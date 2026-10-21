/**
 * Fetch-on-demand hook for per-revision resources (simulation report, build
 * result, file contents...).
 *
 * `identity` names *what* is shown (e.g. a revision id): when it changes the
 * old data is dropped so a different revision's numbers are never displayed
 * under the new selection. `version` names *how fresh* it must be (bumped by
 * revision.updated events): when only the version changes the previous data
 * stays on screen while the refetch runs, so live updates do not flicker.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface Resource<T> {
  data: T | null;
  error: ApiError | Error | null;
  loading: boolean;
  /** true when the server answered 404 - "nothing produced yet", not a failure. */
  notFound: boolean;
  reload: () => void;
}

export function useResource<T>(
  identity: string | null,
  version: string | number,
  fetcher: (signal: AbortSignal) => Promise<T>,
): Resource<T> {
  const [state, setState] = useState<{ identity: string | null; data: T | null; error: ApiError | Error | null; loading: boolean }>(
    { identity: null, data: null, error: null, loading: false },
  );
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (identity === null) {
      setState({ identity: null, data: null, error: null, loading: false });
      return;
    }
    const ctrl = new AbortController();
    setState((prev) =>
      prev.identity === identity
        ? { ...prev, loading: true }
        : { identity, data: null, error: null, loading: true },
    );
    fetcherRef.current(ctrl.signal).then(
      (data) => {
        if (!ctrl.signal.aborted) setState({ identity, data, error: null, loading: false });
      },
      (err: unknown) => {
        if (ctrl.signal.aborted) return;
        const e = err instanceof Error ? err : new Error(String(err));
        // A refresh that fails keeps the data we already had for this identity,
        // except 404 which means the resource is gone/not produced.
        setState((prev) => ({
          identity,
          data: prev.identity === identity && !(e instanceof ApiError && e.isNotFound) ? prev.data : null,
          error: e,
          loading: false,
        }));
      },
    );
    return () => ctrl.abort();
  }, [identity, version, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = state.identity === identity;
  const error = current ? state.error : null;
  return {
    data: current ? state.data : null,
    error,
    loading: identity !== null && (!current || state.loading),
    notFound: error instanceof ApiError && error.isNotFound,
    reload,
  };
}

/** Re-render every `ms` (for live elapsed times computed from server timestamps). */
export function useNow(ms: number, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());
    const h = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(h);
  }, [ms, enabled]);
  return now;
}
