/**
 * RTL of a revision: file list (GET /api/revisions/{id}.files), highlighted
 * source (GET .../files/{name}) and diffs against the parent or any other
 * revision (GET .../diff?against=). Everything shown is the stored revision.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { Revision, RevisionFile, RevisionSummary } from "../api/types";
import type { ProjectDetail } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { fmtBytes, fmtDateTime, shortSha } from "../lib/format";
import { highlightLines, languageFor } from "../lib/highlight";
import { revisionKind, revisionStatus } from "../lib/status";
import { useResource, type Resource } from "../lib/useResource";
import { Card, Collapsible, CopyButton, Empty, ErrorNote, Loading, Pill, Segmented, Select, cx } from "../components/ui";
import { DiffStatsBadge, DiffView, type DiffMode } from "./DiffView";

const ROLE_ORDER: Record<string, number> = { core: 0, wrapper: 1, xdc: 2, library: 3 };

function sortFiles(files: RevisionFile[]): RevisionFile[] {
  return files.slice().sort((a, b) => (ROLE_ORDER[a.role] ?? 9) - (ROLE_ORDER[b.role] ?? 9) || a.name.localeCompare(b.name));
}

function download(name: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function CodeViewer({ revisionId, file, version }: { revisionId: string; file: RevisionFile; version: number }) {
  const res = useResource(`${revisionId}/${file.name}`, version, (signal) => api.revisionFile(revisionId, file.name, signal));
  const lines = useMemo(() => (res.data ? highlightLines(res.data.content, languageFor(file.name)) : []), [res.data, file.name]);
  const raw = useMemo(() => (res.data ? res.data.content.split("\n") : []), [res.data]);
  const modules = useMemo(() => raw.flatMap((l, i) => {
    const m = /^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(l);
    return m ? [{ name: m[1], line: i }] : [];
  }), [raw]);
  const [query, setQuery] = useState("");
  const [cur, setCur] = useState(0);
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? raw.flatMap((l, i) => (l.toLowerCase().includes(q) ? [i] : [])) : [];
  }, [raw, query]);
  const box = useRef<HTMLDivElement>(null);
  const jump = (line: number) => box.current?.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: "center" });
  useEffect(() => {
    setCur(0);
    if (matches.length) jump(matches[0]);
  }, [matches]);
  const step = (d: number) => {
    if (!matches.length) return;
    const n = (cur + d + matches.length) % matches.length;
    setCur(n);
    jump(matches[n]);
  };
  if (res.loading && !res.data) return <Loading label={`Loading ${file.name}…`} />;
  if (res.notFound) return <Empty title={`${file.name} is not available`}>The server has no text content for this file (it may not have been written yet).</Empty>;
  if (res.error && !res.data) return <ErrorNote error={res.error} onRetry={res.reload} />;
  if (!res.data) return null;
  const matchSet = new Set(matches);
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-sunken">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-muted">
        <span className="font-mono text-fg">{file.name}</span>
        <Pill tone="muted">{file.role}</Pill>
        <span>{lines.length} lines · {fmtBytes(file.size)}</span>
        {modules.length > 0 && (
          <span className="flex items-center gap-1">
            {modules.map((m) => (
              <button key={m.line} type="button" onClick={() => jump(m.line)} className="rounded bg-raised px-1.5 font-mono text-fg hover:text-accent"
                      title={`Go to module ${m.name} (line ${m.line + 1})`}>{m.name}</button>
            ))}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" aria-label="Search in file"
                 onKeyDown={(e) => { if (e.key === "Enter") step(e.shiftKey ? -1 : 1); }}
                 className="w-32 rounded border border-line bg-panel px-1.5 py-0.5 text-fg outline-none focus:border-accent" />
          {query && <span className="tabular-nums">{matches.length ? `${cur + 1}/${matches.length}` : "0"}</span>}
          {matches.length > 1 && (
            <>
              <button type="button" onClick={() => step(-1)} className="px-1 hover:text-fg" aria-label="Previous match">↑</button>
              <button type="button" onClick={() => step(1)} className="px-1 hover:text-fg" aria-label="Next match">↓</button>
            </>
          )}
        </span>
        <span className="font-mono" title={file.sha256}>sha256 {shortSha(file.sha256)}</span>
        <CopyButton text={res.data.content} label="copy" />
        <button type="button" onClick={() => download(file.name, res.data!.content)} className="hover:text-fg">download</button>
      </div>
      <div ref={box} className="max-h-[calc(100vh-15rem)] overflow-auto">
        <table className="border-collapse font-mono text-[12px] leading-[1.6]">
          <tbody>
            {lines.map((html, i) => (
              <tr key={i} data-line={i}
                  className={cx(matchSet.has(i) ? (matches[cur] === i ? "bg-warn/20" : "bg-warn/10") : "hover:bg-raised/40")}>
                <td className="w-12 select-none border-r border-line/60 pr-2 text-right align-top text-faint">{i + 1}</td>
                <td className="whitespace-pre pl-3 pr-6">
                  <span className="hljs" dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DiffPanel({ revision, revisions, version }: { revision: Revision; revisions: RevisionSummary[]; version: number }) {
  const store = useStore();
  const compareAgainst = useAppState((s) => s.selection.compareAgainst);
  const [against, setAgainst] = useState<string>(compareAgainst ?? "");
  const [mode, setMode] = useState<DiffMode>("unified");
  useEffect(() => {
    if (compareAgainst) setAgainst(compareAgainst);
  }, [compareAgainst]);

  const res = useResource(`${revision.id}:diff:${against}`, version, (signal) => api.revisionDiff(revision.id, against || null, signal));
  const others = revisions.filter((r) => r.id !== revision.id);
  const parent = revisions.find((r) => r.id === revision.parent_id);
  const againstRev = revisions.find((r) => r.id === (res.data?.against ?? against));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Compare r{revision.number} against</span>
        <Select value={against} onChange={(v) => { setAgainst(v); if (!v) store.clearCompare(); }}>
          <option value="">parent{parent ? ` (r${parent.number})` : " (none)"}</option>
          {others.map((r) => (
            <option key={r.id} value={r.id}>
              r{r.number} · {revisionKind(r.kind).label}
            </option>
          ))}
        </Select>
        <Segmented value={mode} onChange={setMode} options={[{ value: "unified", label: "Unified" }, { value: "split", label: "Side by side" }]} />
        {res.loading && <span className="text-faint">loading…</span>}
      </div>
      {res.error && !res.data && <ErrorNote error={res.error} onRetry={res.reload} />}
      {res.data && res.data.against === null && (
        <Empty title="Nothing to compare">This is the first revision (no parent). Pick another revision above to compare against.</Empty>
      )}
      {res.data && res.data.against !== null && (
        <>
          <div className="text-[11px] text-muted">
            Diff from {againstRev ? `r${againstRev.number}` : res.data.against} to r{revision.number} (computed by the server)
          </div>
          {res.data.files.map((f) => (
            <Collapsible key={f.name} defaultOpen={!!f.diff} title={<span className="font-mono">{f.name}</span>} right={<DiffStatsBadge diff={f.diff} />}>
              <div className="-m-3 overflow-hidden rounded-b-md bg-sunken">
                <DiffView diff={f.diff} mode={mode} lang={languageFor(f.name)} />
              </div>
            </Collapsible>
          ))}
          <Collapsible title={<span className="font-mono">spec.json</span>} defaultOpen={!!res.data.spec_diff} right={<DiffStatsBadge diff={res.data.spec_diff} />}>
            <div className="-m-3 overflow-hidden rounded-b-md bg-sunken">
              <DiffView diff={res.data.spec_diff} mode={mode} lang="json" />
            </div>
          </Collapsible>
        </>
      )}
    </div>
  );
}

function RevisionInfo({ rev }: { rev: Revision }) {
  const kind = revisionKind(rev.kind);
  const st = revisionStatus(rev.status);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-sm font-semibold">r{rev.number}</span>
        <Pill tone={kind.tone}>{kind.label}</Pill>
        <Pill tone={st.tone}>{st.label}</Pill>
      </div>
      <dl className="space-y-1 text-[11px]">
        <div><dt className="inline text-muted">generator </dt><dd className="inline font-mono">{rev.generator || "–"}</dd></div>
        <div><dt className="inline text-muted">interpreter </dt><dd className="inline font-mono">{rev.interpreter || "–"}</dd></div>
        <div><dt className="inline text-muted">created </dt><dd className="inline">{fmtDateTime(rev.created_at)}</dd></div>
        <div><dt className="inline text-muted">spec </dt><dd className="inline">{rev.spec?.title ?? "–"} ({rev.spec?.behavior?.family ?? "?"})</dd></div>
      </dl>
      {rev.user_request && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">Request</div>
          <p className="mt-0.5 text-xs italic text-muted">&ldquo;{rev.user_request}&rdquo;</p>
        </div>
      )}
      {rev.change_summary?.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">Changes</div>
          <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-xs">
            {rev.change_summary.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
      {rev.explanation && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">Generator notes</div>
          <p className="mt-0.5 whitespace-pre-wrap text-xs text-muted">{rev.explanation}</p>
        </div>
      )}
      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">RTL safety check</div>
        {rev.safety_issues?.length ? (
          <ul className="mt-1 space-y-1 text-xs">
            {rev.safety_issues.map((s, i) => (
              <li key={i} className={cx("rounded border px-2 py-1", s.severity === "error" ? "border-bad/30 text-bad" : "border-warn/30 text-warn")}>
                <span className="font-mono">{String(s.code ?? "")}</span> {String(s.message ?? "")}
                {s.line != null && <span className="text-faint"> (line {String(s.line)})</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-0.5 text-xs text-muted">No issues reported.</p>
        )}
      </div>
    </div>
  );
}

export function useRevision(revisionId: string | null): Resource<Revision> & { version: number } {
  const version = useAppState((s) => (revisionId ? s.revisionVersions[revisionId] ?? 0 : 0));
  const res = useResource(revisionId, version, (signal) => api.revision(revisionId!, signal));
  return { ...res, version };
}

export function VerilogTab({ revisionId, detail }: { revisionId: string | null; detail: ProjectDetail | undefined }) {
  const compareAgainst = useAppState((s) => s.selection.compareAgainst);
  const rev = useRevision(revisionId);
  const [mode, setMode] = useState<"code" | "diff">(compareAgainst ? "diff" : "code");
  const [fileName, setFileName] = useState<string | null>(null);
  useEffect(() => {
    if (compareAgainst) setMode("diff");
  }, [compareAgainst]);

  const files = useMemo(() => (rev.data ? sortFiles(rev.data.files ?? []) : []), [rev.data]);
  const file = files.find((f) => f.name === fileName) ?? files[0] ?? null;

  if (!revisionId) {
    return <Empty title="No revision yet">Verilog appears here once the agent generates the first revision of a design.</Empty>;
  }
  if (rev.loading && !rev.data) return <Loading label="Loading revision…" />;
  if (rev.error && !rev.data) return <ErrorNote error={rev.error} onRetry={rev.reload} />;
  if (!rev.data) return null;
  const r = rev.data;

  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_17rem]">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented value={mode} onChange={setMode} size="md" options={[{ value: "code", label: "Source" }, { value: "diff", label: "Diff" }]} />
          {mode === "code" && files.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {files.map((f) => (
                <button
                  key={f.name}
                  type="button"
                  onClick={() => setFileName(f.name)}
                  className={cx(
                    "rounded-md px-2 py-1 font-mono text-[11px] ring-1 ring-inset transition-colors",
                    file?.name === f.name ? "bg-accent/15 text-fg ring-accent/40" : "text-muted ring-line hover:text-fg",
                  )}
                  title={`${f.role} · ${fmtBytes(f.size)}`}
                >
                  {f.name}
                  <span className="ml-1 text-faint">{f.role}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        {mode === "code" ? (
          file ? (
            <CodeViewer revisionId={r.id} file={file} version={rev.version} />
          ) : (
            <Empty title="No RTL files for this revision">The revision exists but its Verilog has not been written yet{r.status === "draft" ? " (draft)" : ""}.</Empty>
          )
        ) : (
          <DiffPanel revision={r} revisions={detail?.revisions ?? []} version={rev.version} />
        )}
      </div>
      <Card title="Revision" className="h-fit">
        <RevisionInfo rev={r} />
      </Card>
    </div>
  );
}
