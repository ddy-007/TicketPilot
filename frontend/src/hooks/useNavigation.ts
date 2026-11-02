import { useCallback, useEffect, useRef, useState } from 'react';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Extract a valid run ID from the current URL, or return empty string. */
function locationRun(): string {
  const value = new URL(location.href).searchParams.get('run');
  return value && uuid.test(value) ? value : '';
}

/**
 * Manages URL-based navigation (history.pushState / popstate).
 * Returns runId, a navigate callback, and a resetView helper to clear dependent state.
 */
export function useNavigation(onReset: (id: string) => void) {
  const [runId, setRunId] = useState(locationRun);
  const [routeRevision, setRouteRevision] = useState(0);
  const viewVersion = useRef(0);

  const resetView = useCallback((id: string) => {
    viewVersion.current++;
    setRouteRevision(n => n + 1);
    setRunId(id);
    onReset(id);
  }, [onReset]);

  const navigate = useCallback((id: string) => {
    const url = new URL(location.href);
    url.searchParams.delete('ticket'); url.searchParams.delete('request');
    id ? url.searchParams.set('run', id) : url.searchParams.delete('run');
    history.pushState({}, '', url.pathname + url.search);
    resetView(id);
  }, [resetView]);

  useEffect(() => {
    const changed = () => resetView(locationRun());
    window.addEventListener('popstate', changed);
    return () => window.removeEventListener('popstate', changed);
  }, [resetView]);

  return { runId, routeRevision, viewVersion, navigate, resetView } as const;
}
