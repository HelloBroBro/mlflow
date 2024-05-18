import { useEffect, useRef } from 'react';
import { RUNS_SEARCH_MAX_RESULTS } from '../../../actions';
import { isArray, isEqual } from 'lodash';
import type { ExperimentQueryParamsSearchFacets } from './useExperimentPageSearchFacets';
import type { ExperimentRunsSelectorResult } from '../utils/experimentRuns.selector';
import { RUNS_AUTO_REFRESH_INTERVAL, createSearchRunsParams } from '../utils/experimentPage.fetch-utils';
import type { FetchRunsHookFunction } from './useExperimentRuns';

/**
 * Enables auto-refreshing runs on the experiment page.
 * The hook will schedule a new runs fetch every `RUNS_AUTO_REFRESH_INTERVAL` milliseconds and will be postponed
 * if user is currently loading runs or changes the search facets.
 */
export const useExperimentRunsAutoRefresh = ({
  experimentIds,
  lastFetchedTime,
  fetchRuns,
  searchFacets,
  enabled,
  cachedPinnedRuns,
  runsData,
  isLoadingRuns,
}: {
  cachedPinnedRuns: React.MutableRefObject<string[]>;
  lastFetchedTime: React.MutableRefObject<number | null>;
  enabled: boolean;
  experimentIds: string[];
  fetchRuns: FetchRunsHookFunction;
  searchFacets: ExperimentQueryParamsSearchFacets | null;
  runsData: ExperimentRunsSelectorResult;
  isLoadingRuns: boolean;
}) => {
  const refreshTimeoutRef = useRef<number | undefined>(undefined);

  const isLoadingImmediate = useRef(isLoadingRuns);
  const autoRefreshEnabledRef = useRef(enabled);
  const currentResults = useRef(runsData.runInfos);

  currentResults.current = runsData.runInfos;
  isLoadingImmediate.current = isLoadingRuns;
  autoRefreshEnabledRef.current = enabled;

  useEffect(() => {
    // Each time the parameters change, clear the timeout and try to schedule a new one
    window.clearTimeout(refreshTimeoutRef.current);

    // If auto refresh has been disabled or user is currently loading runs, do not schedule a new refresh
    if (!enabled || isLoadingRuns) {
      return;
    }

    const scheduleRefresh = async () => {
      const hasBeenInitialized = Boolean(lastFetchedTime.current);
      const timePassed = lastFetchedTime.current ? Date.now() - lastFetchedTime.current : 0;
      if (searchFacets && hasBeenInitialized && timePassed >= RUNS_AUTO_REFRESH_INTERVAL) {
        const requestedPageCount = Math.ceil(currentResults.current.length / RUNS_SEARCH_MAX_RESULTS);

        const requestParams = {
          ...createSearchRunsParams(
            experimentIds,
            { ...searchFacets, runsPinned: cachedPinnedRuns.current },
            Date.now(),
          ),
          requestedFacets: searchFacets,
          maxResults: requestedPageCount * RUNS_SEARCH_MAX_RESULTS,
        };

        await fetchRuns(requestParams, {
          isAutoRefreshing: true,
          discardResultsFn: (lastRequestedParams) => {
            const existingPageCount = Math.ceil(currentResults.current.length / RUNS_SEARCH_MAX_RESULTS);

            // At this moment, check if the results from auto-refresh should be considered. If the following
            // conditions are met, the results from auto-refresh will be discarded.
            if (
              // Skip if auto-refresh has been disabled before the results response came back
              !autoRefreshEnabledRef.current ||
              // Skip if user has loaded more runs since the last request
              existingPageCount !== requestedPageCount ||
              // Skip if the requested facets have changed since the last request
              !isEqual(lastRequestedParams.requestedFacets, requestParams.requestedFacets)
            ) {
              return true;
            }

            // Otherwise, return "false" and consider the results from auto-refresh as valid
            return false;
          },
        });
      }

      // Clear the timeout before scheduling a new one
      window.clearTimeout(refreshTimeoutRef.current);

      // If auto refresh has been disabled during last fetch, do not schedule a new one
      if (!autoRefreshEnabledRef.current) {
        return;
      }
      refreshTimeoutRef.current = window.setTimeout(scheduleRefresh, RUNS_AUTO_REFRESH_INTERVAL);
    };
    scheduleRefresh();
    return () => {
      clearTimeout(refreshTimeoutRef.current);
    };
  }, [experimentIds, fetchRuns, searchFacets, enabled, cachedPinnedRuns, lastFetchedTime, isLoadingRuns]);
};
