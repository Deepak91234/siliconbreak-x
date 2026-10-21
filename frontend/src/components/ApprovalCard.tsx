/**
 * An approval request (docs/api.md `Approval`). Actions that touch hardware or
 * containers only happen after the user presses Approve here; the card then
 * follows the approval through approved -> executed/failed via events.
 */

import { useState } from "react";
import type { Approval } from "../api/types";
import { errorMessage } from "../api/client";
import { useStore } from "../store/store";
import { approvalStatus } from "../lib/status";
import { compactJson, fmtTime, shortSha } from "../lib/format";
import { toast } from "../lib/toast";
import { Button, Pill, cx } from "./ui";

const KIND_LABEL: Record<string, string> = {
  program_fpga: "Program FPGA",
  start_vivado_container: "Start Vivado container",
  start_docker: "Start Docker",
  stop_xvcd: "Stop xvcd",
};

function DetailRows({ details }: { details: Record<string, unknown> }) {
  const entries = Object.entries(details ?? {});
  if (!entries.length) return null;
  return (
    <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-[11px]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
          <dd className="min-w-0 break-all font-mono text-fg/90">
            {k.endsWith("sha256") && typeof v === "string" ? <span title={v}>{shortSha(v, 20)}</span> : Array.isArray(v) ? v.map(String).join("; ") || "–" : compactJson(v, 200)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function ApprovalCard({ approval, compact = false }: { approval: Approval; compact?: boolean }) {
  const store = useStore();
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const st = approvalStatus(approval.status);
  const pending = approval.status === "pending";
  const result = approval.result as { ok?: boolean; message?: string } | null;

  const decide = async (ok: boolean) => {
    setBusy(ok ? "approve" : "deny");
    try {
      const a = await store.decideApproval(approval.id, ok);
      toast(`${approval.title}: ${a.status}`, a.status === "failed" ? "error" : "ok");
    } catch (e) {
      toast(`Could not ${ok ? "approve" : "deny"}: ${errorMessage(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={cx("rounded-lg border bg-raised/60 p-3", pending ? "border-warn/50 shadow-[0_0_0_1px_rgb(var(--warn)/0.15)]" : "border-line")}>
      <div className="flex items-start gap-2">
        <svg viewBox="0 0 20 20" className={cx("mt-0.5 h-4 w-4 shrink-0", pending ? "text-warn" : "text-muted")} fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
          <path d="M10 2.5l6.5 2.5v4.6c0 4-2.8 6.7-6.5 7.9-3.7-1.2-6.5-3.9-6.5-7.9V5z" strokeLinejoin="round" />
          <path d="M7.2 10l2 2 3.8-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-semibold">{approval.title}</span>
            <Pill tone={st.tone}>{st.label}</Pill>
            <span className="text-[10px] uppercase tracking-wide text-faint">{KIND_LABEL[approval.kind] ?? approval.kind}</span>
          </div>
          {!compact && approval.description && <p className="mt-1 whitespace-pre-wrap text-xs text-muted">{approval.description}</p>}
          {!compact && <DetailRows details={approval.details} />}
          {result && (result.message || result.ok !== undefined) && (
            <div className={cx("mt-2 rounded border px-2 py-1 text-[11px]", result.ok ? "border-ok/30 text-ok" : "border-bad/30 text-bad")}>
              {result.ok ? "Result: ok" : "Result: failed"}{result.message ? ` – ${result.message}` : ""}
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {pending ? (
              <>
                <Button variant="ok" size="sm" busy={busy === "approve"} disabled={busy !== null} onClick={() => void decide(true)}>
                  Approve
                </Button>
                <Button variant="secondary" size="sm" busy={busy === "deny"} disabled={busy !== null} onClick={() => void decide(false)}>
                  Deny
                </Button>
              </>
            ) : null}
            <span className="text-[10px] text-faint">
              requested {fmtTime(approval.created_at)}
              {approval.decided_at ? ` · decided ${fmtTime(approval.decided_at)}` : ""}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
