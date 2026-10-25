/**
 * Import a public GitHub repository at a pinned commit and pick files to audit.
 * The index (modules, includes, security-relevance score) comes from
 * POST /api/security/repos; nothing in the repository is executed.
 */

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type RepoIndex, type RepoSummary } from "../api/security";
import { Button, ErrorNote, Pill, Select, cx } from "../components/ui";
import { toast } from "../lib/toast";
import { useStore } from "../store/store";

const EXAMPLE = { url: "https://github.com/lowRISC/opentitan", ref: "master", subdir: "hw/ip/rv_dm/rtl" };

export function RepoImport() {
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [subdir, setSubdir] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [idx, setIdx] = useState<RepoIndex | null>(null);
  const [known, setKnown] = useState<RepoSummary[]>([]);

  useEffect(() => { securityApi.repos().then(setKnown).catch(() => setKnown([])); }, [idx?.repo_id]);

  const doImport = async () => {
    setBusy(true);
    setErr(null);
    try {
      setIdx(await securityApi.importRepo({ url: url.trim(), ref: ref.trim() || undefined, subdir: subdir.trim() || undefined }));
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const open = async (id: string) => {
    try {
      setIdx(await securityApi.repo(id));
    } catch (e) {
      setErr(errorMessage(e));
    }
  };

  return (
    <section className="rounded-xl border border-line bg-panel p-4 shadow-panel" aria-label="Import a repository">
      <div className="flex items-start justify-between gap-2">
        <div>
          <span className="text-sm font-medium text-fg">Audit a public repository</span>
          <p className="mt-0.5 text-xs text-muted">Pinned to a commit SHA; only RTL files are fetched; nothing is executed. Findings stay
            suspected because no trusted tests exist for arbitrary repositories.</p>
        </div>
        <button type="button" className="shrink-0 text-2xs text-accent hover:underline"
                onClick={() => { setUrl(EXAMPLE.url); setRef(EXAMPLE.ref); setSubdir(EXAMPLE.subdir); }}>
          Example: OpenTitan rv_dm
        </button>
      </div>
      <div className="mt-2.5 grid gap-2 sm:grid-cols-[2fr_1fr_1.4fr_auto]">
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repo" aria-label="Repository URL"
               className="h-8 rounded-md bg-sunken px-2.5 text-sm ring-1 ring-line placeholder:text-faint focus:outline-none focus:ring-accent/60" />
        <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="ref or SHA (default HEAD)" aria-label="Git ref"
               className="h-8 rounded-md bg-sunken px-2.5 text-sm ring-1 ring-line placeholder:text-faint focus:outline-none focus:ring-accent/60" />
        <input value={subdir} onChange={(e) => setSubdir(e.target.value)} placeholder="subdirectory (optional)" aria-label="Subdirectory"
               className="h-8 rounded-md bg-sunken px-2.5 text-sm ring-1 ring-line placeholder:text-faint focus:outline-none focus:ring-accent/60" />
        <Button size="md" variant="secondary" busy={busy} disabled={!url.trim()} onClick={doImport}>Import</Button>
      </div>
      {known.length > 0 && !idx && (
        <div className="mt-2 flex items-center gap-2 text-xs text-muted">
          <span>Imported before:</span>
          <Select value="" onChange={(v) => v && open(v)} className="max-w-[24rem]">
            <option value="">choose…</option>
            {known.map((r) => <option key={r.repo_id} value={r.repo_id}>{r.owner}/{r.name}@{r.sha.slice(0, 7)}{r.subdir ? ` ${r.subdir}` : ""}</option>)}
          </Select>
        </div>
      )}
      {err && <ErrorNote error={err} className="mt-2" />}
      {idx && <RepoFiles idx={idx} onClose={() => setIdx(null)} />}
    </section>
  );
}

function RepoFiles({ idx, onClose }: { idx: RepoIndex; onClose: () => void }) {
  const store = useStore();
  const [q, setQ] = useState("");
  const ranked = useMemo(() => [...idx.files].sort((a, b) => b.score - a.score), [idx]);
  const [sel, setSel] = useState<Set<string>>(() => new Set(ranked.filter((f) => f.score > 0 && !f.skipped).slice(0, 4).map((f) => f.path)));
  const [busy, setBusy] = useState(false);
  const shown = ranked.filter((f) => !q || f.path.toLowerCase().includes(q.toLowerCase())
    || f.modules.some((m) => m.toLowerCase().includes(q.toLowerCase())));
  const toggle = (p: string) => setSel((s) => { const n = new Set(s); n.has(p) ? n.delete(p) : n.add(p); return n; });
  const audit = async () => {
    setBusy(true);
    try {
      await store.startRepoAudit(idx.repo_id, [...sel]);
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-3 rounded-md border border-line">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <a href={`${idx.url}/tree/${idx.sha}${idx.subdir ? `/${idx.subdir}` : ""}`} target="_blank" rel="noreferrer"
           className="font-medium text-fg hover:text-accent">{idx.owner}/{idx.name}</a>
        <span className="font-mono text-2xs text-faint" title={idx.sha}>@{idx.sha.slice(0, 12)}</span>
        {idx.subdir && <span className="font-mono text-2xs text-faint">{idx.subdir}</span>}
        <span className="text-faint">· {idx.totals.files} RTL files · {idx.totals.modules} modules · {idx.totals.lines} lines indexed</span>
        {idx.truncated && <Pill tone="warn">index truncated at the size cap</Pill>}
        <button type="button" onClick={onClose} className="ml-auto text-faint hover:text-fg">close</button>
      </div>
      <div className="flex items-center gap-2 px-3 py-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by path or module" aria-label="Filter files"
               className="h-7 min-w-0 flex-1 rounded bg-sunken px-2 text-xs ring-1 ring-line placeholder:text-faint focus:outline-none" />
        <Button size="sm" variant="primary" busy={busy} disabled={sel.size === 0 || sel.size > 12} onClick={audit}>
          Audit {sel.size} selected file{sel.size === 1 ? "" : "s"}
        </Button>
      </div>
      <ul className="max-h-72 overflow-y-auto border-t border-line text-xs">
        {shown.map((f) => (
          <li key={f.path} className={cx("flex items-center gap-2 px-3 py-1.5", sel.has(f.path) && "bg-accent/5")}>
            <input type="checkbox" checked={sel.has(f.path)} disabled={!!f.skipped} onChange={() => toggle(f.path)}
                   aria-label={`Select ${f.path}`} className="accent-[rgb(var(--accent))]" />
            <span className="w-12 shrink-0 text-right font-mono text-2xs tabular-nums text-faint" title="security-relevance score">{f.score.toFixed(1)}</span>
            <span className="min-w-0 flex-1 truncate font-mono" title={f.path}>{idx.subdir ? f.path.slice(idx.subdir.length + 1) : f.path}</span>
            <span className="hidden truncate text-2xs text-faint sm:inline">{f.modules.slice(0, 2).join(", ")}</span>
            <span className="w-14 shrink-0 text-right text-2xs text-faint">{f.skipped ? "too big" : `${f.lines} ln`}</span>
          </li>
        ))}
      </ul>
      <p className="border-t border-line px-3 py-1.5 text-2xs text-faint">
        Score = weighted security keywords (debug, lock, key/secret, privilege, life cycle, zeroize, bypass) in identifiers and
        paths; it only orders files for review. At most 12 files per audit.
      </p>
    </div>
  );
}
