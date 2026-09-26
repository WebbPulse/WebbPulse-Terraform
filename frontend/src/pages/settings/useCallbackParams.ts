/** A GitHub callback's query: read once, then cleared from the address bar. */

import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

/**
 * The query the page was opened with, held from the first render.
 *
 * Held rather than read live, so clearing the address bar with
 * {@link useClearQueryWhen} does not change what the page shows.
 */
export function useInitialParams(): URLSearchParams {
  const location = useLocation();
  const [params] = useState(() => new URLSearchParams(location.search));
  return params;
}

/**
 * Replaces the history entry with the bare path once `done`.
 *
 * The query carries a single use code and state, so it should not linger in the
 * address bar or history after the exchange.
 */
export function useClearQueryWhen(done: boolean): void {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!done || location.search === '') {
      return;
    }
    void navigate(
      { pathname: location.pathname, hash: location.hash },
      { replace: true }
    );
  }, [done, location.hash, location.pathname, location.search, navigate]);
}
