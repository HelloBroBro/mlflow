import contextlib
import inspect
import logging
import uuid
import warnings
from copy import deepcopy

from packaging.version import Version

import mlflow
from mlflow.entities import RunTag
from mlflow.exceptions import MlflowException
from mlflow.langchain.runnables import get_runnable_steps
from mlflow.ml_package_versions import _ML_PACKAGE_VERSIONS
from mlflow.tracking.context import registry as context_registry
from mlflow.utils.autologging_utils import (
    ExceptionSafeAbstractClass,
    disable_autologging,
    get_autologging_config,
)
from mlflow.utils.autologging_utils.safety import _resolve_extra_tags

MIN_REQ_VERSION = Version(_ML_PACKAGE_VERSIONS["langchain"]["autologging"]["minimum"])
MAX_REQ_VERSION = Version(_ML_PACKAGE_VERSIONS["langchain"]["autologging"]["maximum"])

_logger = logging.getLogger(__name__)

UNSUPPORT_LOG_MODEL_MESSAGE = (
    "MLflow autologging does not support logging models containing BaseRetriever because "
    "logging the model requires `loader_fn` and `persist_dir`. Please log the model manually "
    "using `mlflow.langchain.log_model(model, artifact_path, loader_fn=..., persist_dir=...)`"
)
INFERENCE_FILE_NAME = "inference_inputs_outputs.json"


def _get_input_data_from_function(func_name, model, args, kwargs):
    func_param_name_mapping = {
        "__call__": "inputs",
        "invoke": "input",
        "batch": "inputs",
        "get_relevant_documents": "query",
    }
    input_example_exc = None
    if param_name := func_param_name_mapping.get(func_name):
        inference_func = getattr(model, func_name)
        # A guard to make sure `param_name` is the first argument of inference function
        if next(iter(inspect.signature(inference_func).parameters.keys())) != param_name:
            input_example_exc = MlflowException(
                "Inference function signature changes, please contact MLflow team to "
                "fix langchain autologging.",
            )
        else:
            return args[0] if len(args) > 0 else kwargs.get(param_name)
    else:
        input_example_exc = MlflowException(
            f"Unsupported inference function. Only support {list(func_param_name_mapping.keys())}."
        )
    _logger.warning(
        f"Failed to gather input example of model {model.__class__.__name__} "
        f"due to {input_example_exc}."
    )


def _convert_data_to_dict(data, key):
    if isinstance(data, dict):
        return {f"{key}-{k}": v for k, v in data.items()}
    if isinstance(data, list):
        return {key: data}
    if isinstance(data, str):
        return {key: [data]}
    raise MlflowException("Unsupported data type.")


def _combine_input_and_output(input, output, session_id, func_name):
    """
    Combine input and output into a single dictionary
    """
    if func_name == "get_relevant_documents" and output is not None:
        output = [{"page_content": doc.page_content, "metadata": doc.metadata} for doc in output]
        # to make sure output is inside a single row when converted into pandas DataFrame
        output = [output]
    result = {"session_id": [session_id]}
    if input:
        result.update(_convert_data_to_dict(input, "input"))
    if output:
        result.update(_convert_data_to_dict(output, "output"))
    return result


def _update_langchain_model_config(model):
    try:
        from langchain_core.pydantic_v1 import Extra
    except ImportError as e:
        warnings.warn(
            "MLflow langchain autologging might log model several "
            "times due to the pydantic.config.Extra import error. "
            f"Error: {e}"
        )
        return False
    else:
        # Langchain models are Pydantic models, and the value for extra is
        # ignored, we need to set it to allow so as to set attributes on
        # the model to keep track of logging status
        if hasattr(model, "__config__"):
            model.__config__.extra = Extra.allow
        return True


def _inject_callbacks(original_callbacks, new_callbacks):
    """
    Inject list of callbacks into the original callbacks.
    For RunnableConfig, the callbacks is defined as List[BaseCallbackHandler] or BaseCallbackManager
    https://github.com/langchain-ai/langchain/blob/ed980601e1c630f996aabf85df5cb26178e53099/libs/core/langchain_core/callbacks/base.py#L636
    """
    from langchain_core.callbacks.base import BaseCallbackManager

    if isinstance(original_callbacks, BaseCallbackManager):
        for callback in new_callbacks:
            original_callbacks.add_handler(callback)
        return original_callbacks
    if not isinstance(original_callbacks, list):
        original_callbacks = [original_callbacks]
    return original_callbacks + new_callbacks


def _inject_callbacks_for_runnable(mlflow_callbacks, args, kwargs):
    """
    `config` is the second positional argument of runnable.invoke and runnable.batch functions
    https://github.com/langchain-ai/langchain/blob/7d444724d7582386de347fb928619c2243bd0e55/libs/core/langchain_core/runnables/base.py#L468
    https://github.com/langchain-ai/langchain/blob/ed980601e1c630f996aabf85df5cb26178e53099/libs/core/langchain_core/runnables/base.py#L600-L607

    """
    from langchain.schema.runnable.config import RunnableConfig

    in_args = False
    if len(args) >= 2:
        config = args[1]
        in_args = True
    else:
        config = kwargs.get("config")
    if config is None:
        config = RunnableConfig(callbacks=mlflow_callbacks)
    else:
        # for `invoke`, config type is RunnableConfig
        # for `batch`, config type is Union[RunnableConfig, List[RunnableConfig]]
        if isinstance(config, list):
            for c in config:
                c["callbacks"] = _inject_callbacks(c.get("callbacks") or [], mlflow_callbacks)
        else:
            config["callbacks"] = _inject_callbacks(config.get("callbacks") or [], mlflow_callbacks)
    if in_args:
        args = (args[0], config) + args[2:]
    else:
        kwargs["config"] = config
    return args, kwargs


def _inject_mlflow_callbacks(func_name, mlflow_callbacks, args, kwargs):
    """
    Inject list of callbacks into the function named `func_name` of the model.
    """
    if func_name in ["invoke", "batch"]:
        return _inject_callbacks_for_runnable(mlflow_callbacks, args, kwargs)

    if func_name == "__call__":
        # `callbacks` is the third positional argument of chain.__call__ function
        # https://github.com/langchain-ai/langchain/blob/7d444724d7582386de347fb928619c2243bd0e55/libs/langchain/langchain/chains/base.py#L320
        if len(args) >= 3:
            callbacks_arg = _inject_callbacks(args[2] or [], mlflow_callbacks)
            args = args[:2] + (callbacks_arg,) + args[3:]
        else:
            kwargs["callbacks"] = _inject_callbacks(kwargs.get("callbacks") or [], mlflow_callbacks)
        return args, kwargs

    # callbacks is only available as kwargs in get_relevant_documents function
    # https://github.com/langchain-ai/langchain/blob/7d444724d7582386de347fb928619c2243bd0e55/libs/core/langchain_core/retrievers.py#L173
    if func_name == "get_relevant_documents":
        kwargs["callbacks"] = _inject_callbacks(kwargs.get("callbacks") or [], mlflow_callbacks)
        return args, kwargs


def _runnable_with_retriever(model):
    from langchain.schema import BaseRetriever

    with contextlib.suppress(ImportError):
        from langchain.schema.runnable import RunnableBranch, RunnableParallel, RunnableSequence
        from langchain.schema.runnable.passthrough import RunnableAssign

        if isinstance(model, RunnableBranch):
            return any(_runnable_with_retriever(runnable) for _, runnable in model.branches)

        if isinstance(model, RunnableParallel):
            return any(
                _runnable_with_retriever(runnable)
                for runnable in get_runnable_steps(model).values()
            )

        if isinstance(model, RunnableSequence):
            return any(_runnable_with_retriever(runnable) for runnable in get_runnable_steps(model))

        if isinstance(model, RunnableAssign):
            return _runnable_with_retriever(model.mapper)

    return isinstance(model, BaseRetriever)


def _chain_with_retriever(model):
    with contextlib.suppress(ImportError):
        from langchain.chains import RetrievalQA

        return isinstance(model, RetrievalQA)
    return False


def patched_inference(func_name, original, self, *args, **kwargs):
    """
    A patched implementation of langchain models inference process which enables logging the
    following parameters, metrics and artifacts:

    - model
    - metrics
    - data

    We patch either `invoke` or `__call__` function for different models
    based on their usage.
    """

    import langchain
    from langchain_community.callbacks import MlflowCallbackHandler

    class _MlflowLangchainCallback(MlflowCallbackHandler, metaclass=ExceptionSafeAbstractClass):
        """
        Callback for auto-logging metrics and parameters.
        We need to inherit ExceptionSafeAbstractClass to avoid invalid new
        input arguments added to original function call.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

    _lc_version = Version(langchain.__version__)
    if not MIN_REQ_VERSION <= _lc_version <= MAX_REQ_VERSION:
        warnings.warn(
            "Autologging is known to be compatible with langchain versions between "
            f"{MIN_REQ_VERSION} and {MAX_REQ_VERSION} and may not succeed with packages "
            "outside this range."
        )

    run_id = getattr(self, "run_id", None)
    active_run = mlflow.active_run()
    if run_id is None:
        # only log the tags once
        extra_tags = get_autologging_config(mlflow.langchain.FLAVOR_NAME, "extra_tags", None)
        # include run context tags
        resolved_tags = context_registry.resolve_tags(extra_tags)
        tags = _resolve_extra_tags(mlflow.langchain.FLAVOR_NAME, resolved_tags)
        if active_run:
            run_id = active_run.info.run_id
            mlflow.MlflowClient().log_batch(
                run_id=run_id,
                tags=[RunTag(key, str(value)) for key, value in tags.items()],
            )
    else:
        tags = None
    # Use session_id-inference_id as artifact directory where mlflow
    # callback logs artifacts into, to avoid overriding artifacts
    session_id = getattr(self, "session_id", uuid.uuid4().hex)
    inference_id = getattr(self, "inference_id", 0)
    mlflow_callback = _MlflowLangchainCallback(
        tracking_uri=mlflow.get_tracking_uri(),
        run_id=run_id,
        artifacts_dir=f"artifacts-{session_id}-{inference_id}",
        tags=tags,
    )
    args, kwargs = _inject_mlflow_callbacks(func_name, [mlflow_callback], args, kwargs)
    with disable_autologging():
        result = original(self, *args, **kwargs)

    mlflow_callback.flush_tracker()

    log_models = get_autologging_config(mlflow.langchain.FLAVOR_NAME, "log_models", False)
    log_input_examples = get_autologging_config(
        mlflow.langchain.FLAVOR_NAME, "log_input_examples", False
    )
    log_model_signatures = get_autologging_config(
        mlflow.langchain.FLAVOR_NAME, "log_model_signatures", False
    )
    input_example = None
    if log_models and not hasattr(self, "model_logged"):
        if (
            (func_name == "get_relevant_documents")
            or _runnable_with_retriever(self)
            or _chain_with_retriever(self)
        ):
            _logger.info(UNSUPPORT_LOG_MODEL_MESSAGE)
        else:
            # warn user in case we did't capture some cases where retriever is used
            warnings.warn(UNSUPPORT_LOG_MODEL_MESSAGE)
            if log_input_examples:
                input_example = deepcopy(
                    _get_input_data_from_function(func_name, self, args, kwargs)
                )
                if not log_model_signatures:
                    _logger.info(
                        "Signature is automatically generated for logged model if "
                        "input_example is provided. To disable log_model_signatures, "
                        "please also disable log_input_examples."
                    )

            registered_model_name = get_autologging_config(
                mlflow.langchain.FLAVOR_NAME, "registered_model_name", None
            )
            try:
                with disable_autologging():
                    mlflow.langchain.log_model(
                        self,
                        "model",
                        input_example=input_example,
                        registered_model_name=registered_model_name,
                        run_id=mlflow_callback.mlflg.run_id,
                    )
            except Exception as e:
                _logger.warning(f"Failed to log model due to error {e}.")
            if _update_langchain_model_config(self):
                self.model_logged = True

    # Even if the model is not logged, we keep a single run per model
    if _update_langchain_model_config(self):
        if not hasattr(self, "run_id"):
            self.run_id = mlflow_callback.mlflg.run_id
        if not hasattr(self, "session_id"):
            self.session_id = session_id
        self.inference_id = inference_id + 1

    log_inputs_outputs = get_autologging_config(
        mlflow.langchain.FLAVOR_NAME, "log_inputs_outputs", False
    )
    if log_inputs_outputs:
        if input_example is None:
            input_data = deepcopy(_get_input_data_from_function(func_name, self, args, kwargs))
            if input_data is None:
                _logger.info("Input data gathering failed, only log inference results.")
        else:
            input_data = input_example
        try:
            data_dict = _combine_input_and_output(input_data, result, self.session_id, func_name)
            mlflow.log_table(data_dict, INFERENCE_FILE_NAME, run_id=mlflow_callback.mlflg.run_id)
        except Exception as e:
            _logger.warning(
                f"Failed to log inputs and outputs into `{INFERENCE_FILE_NAME}` "
                f"file due to error {e}."
            )

    # Terminate the run if it is not managed by the user
    if active_run is None or active_run.info.run_id != mlflow_callback.mlflg.run_id:
        mlflow.MlflowClient().set_terminated(mlflow_callback.mlflg.run_id)

    return result
