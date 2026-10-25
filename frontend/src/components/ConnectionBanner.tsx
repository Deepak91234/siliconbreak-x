/**
 * Tells the user when what they see may be stale: the backend is unreachable
 * (REST failing) or the live event stream is down (reconnecting with backoff).
 */

import { useEffect, useState } from "react";
import { useAppState, useStore } from "../store/store";
import { Button, Spinner } from "./ui";

export function ConnectionBanner() {
  const store = useStore();
  const backend = useAppState((s) => s.backend);
  const socket = useAppState((s) => s.socket);
  // Do not flash the banner during the first connection attempt.
  const [grace, setGrace] = useState(true);
  useEffect(() => {
    const h = setTimeout(() => setGrace(false), 2500);
    return () => clearTimeout(h);
  }, []);

  if (backend.reachable === false) {
    return (
      <div className="flex flex-wrap items-center gap-3 border-b border-bad/30 bg-bad/10 px-4 py-2 text-xs text-bad">
        <Spinner className="h-3.5 w-3.5" />
        <span className="font-semibold">SiliconBreak X backend not reachable.</span>
        <span className="min-w-0 flex-1 truncate text-bad/80">
          {backend.error ?? "No response from the API."} Start it with <code className="font-mono">scripts/dev.sh</code> (FastAPI on 127.0.0.1:8000); retrying automatically.
        </span>
        <Button size="sm" variant="danger" onClick={() => store.retryNow()}>
          Retry now
        </Button>
      </div>
    );
  }
  if (!grace && backend.reachable && socket.status !== "open") {
    return (
      <div className="flex flex-wrap items-center gap-3 border-b border-warn/30 bg-warn/10 px-4 py-1.5 text-xs text-warn">
        <Spinner className="h-3.5 w-3.5" />
        <span>
          Live updates disconnected
          {socket.retryInMs != null ? ` – reconnecting in ${Math.ceil(socket.retryInMs / 1000)} s` : " – reconnecting"}
          {socket.lastError ? ` (${socket.lastError})` : ""}. The view is refreshed from the API every 10 s meanwhile.
        </span>
        <Button size="sm" variant="warn" onClick={() => store.retryNow()}>
          Reconnect
        </Button>
      </div>
    );
  }
  return null;
}
