import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI backend (docs/api.md). Everything the dashboard shows comes from it.
// CIRCUITLOOP_BACKEND overrides it (e.g. a backend on a private port for testing).
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const BACKEND = env.CIRCUITLOOP_BACKEND || "http://127.0.0.1:8000";

/**
 * When the backend is down Vite's proxy would answer with an empty 500, which is
 * indistinguishable from a real server error. Answer 502 with a JSON `detail`
 * instead so the client can show "backend unreachable" and keep retrying.
 */
function backendDownAs502(proxy: Parameters<NonNullable<ProxyOptions["configure"]>>[0]): void {
  proxy.on("error", (err, _req, res) => {
    const r = res as unknown as {
      headersSent?: boolean;
      writableEnded?: boolean;
      writeHead?: (status: number, headers: Record<string, string>) => { end: (body: string) => void };
    };
    if (r && typeof r.writeHead === "function" && !r.headersSent && !r.writableEnded) {
      r.writeHead(502, { "Content-Type": "application/json" }).end(
        JSON.stringify({ detail: `CircuitLoop backend not reachable at ${BACKEND} (${err.message})`, backend_down: true }),
      );
    }
  });
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: BACKEND, changeOrigin: false, configure: backendDownAs502 },
      "/ws": { target: BACKEND, ws: true, changeOrigin: false },
    },
  },
  preview: {
    port: 4173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: BACKEND, configure: backendDownAs502 },
      "/ws": { target: BACKEND, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 900,
  },
});
