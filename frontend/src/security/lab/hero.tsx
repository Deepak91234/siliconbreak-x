/** Audit header used by the Dashboard (featured audit) and the Security Lab audit view. */

import { useState, type ReactNode } from "react";
import { errorMessage } from "../../api/client";
import { securityApi, type SecurityAudit } from "../../api/security";
import { IconArrowLeft, IconDownload, IconShield } from "../../components/icons";
import { Button, Pill, Select } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { runStatus } from "../../lib/status";
import { toast } from "../../lib/toast";
import { useStore } from "../../store/store";
import { auditHeadlineTone, providerLabel } from "../derive";
import { SOURCE_KIND_LABEL } from "../SecurityPanel";

/** Download a stored text artifact of the audit (report.md, audit.json) as a file. */
export async function downloadArtifact(audit: SecurityAudit, path: string): Promise<void> {
  try {
    const f = await securityApi.file(audit.id, path);
    const blob = new Blob([f.text], { type: path.endsWith(".json") ? "application/json" : "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `siliconbreak-${audit.id}-${path.split("/").pop()}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    toast(`Could not export ${path}: ${errorMessage(e)}`, "error");
  }
}

export function AuditHero({ audit, siblings, eyebrow, onBack, actions, right }: {
  audit: SecurityAudit;
  siblings?: SecurityAudit[];
  eyebrow?: ReactNode;
  onBack?: () => void;
  actions?: ReactNode;
  right?: ReactNode;
}) {
  const store = useStore();
  const [exporting, setExporting] = useState(false);
  const r = audit.record;
  const c = r.case;
  const st = runStatus(audit.status);
  const f = r.findings[0];
  const inv = r.investigation;
  const title = c?.title ?? r.source?.label ?? audit.id;
  return (
    <section className="sbx-hero rounded-2xl border border-line p-5 shadow-panel">
      {(onBack || eyebrow) && (
        <div className="mb-3 flex items-center gap-3 text-xs">
          {onBack && (
            <button type="button" onClick={onBack} className="inline-flex items-center gap-1.5 font-medium text-accent hover:underline">
              <IconArrowLeft size={14} /> All audits
            </button>
          )}
          {eyebrow && <span className="text-[10.5px] font-semibold uppercase tracking-[0.08em] text-faint">{eyebrow}</span>}
        </div>
      )}
      <div className="flex flex-wrap items-start gap-4">
        <span className="hidden h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-accent/10 text-accent ring-1 ring-inset ring-accent/25 sm:flex">
          <IconShield size={24} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <h2 className="text-2xl font-semibold tracking-tight text-fg">{title}</h2>
            <Pill tone={auditHeadlineTone(audit)} className="!px-2.5 !py-1 !text-xs">{st.label}</Pill>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted">{r.outcome?.summary || c?.security_contract.summary || "Audit running…"}</p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {(c?.cwe ?? []).map((w) => <span key={w} className="rounded-full bg-raised px-2.5 py-1 font-mono text-[11px] text-fg ring-1 ring-inset ring-line">{w}</span>)}
            {f && !(c?.cwe ?? []).includes(f.weakness_class) && <span className="rounded-full bg-info/10 px-2.5 py-1 text-[11px] font-medium text-info ring-1 ring-inset ring-info/25">{f.weakness_class}</span>}
            {c && <span className="rounded-full bg-violet/10 px-2.5 py-1 text-[11px] font-medium text-violet ring-1 ring-inset ring-violet/25">{SOURCE_KIND_LABEL[c.source_kind] ?? c.source_kind}</span>}
            {inv && <span className="rounded-full bg-accent/10 px-2.5 py-1 text-[11px] font-medium text-accent ring-1 ring-inset ring-accent/25">investigator: {providerLabel(inv.provider, inv.model)}</span>}
            {r.fileset?.kind === "repository" && r.fileset.sha && <span className="rounded-full bg-raised px-2.5 py-1 font-mono text-[11px] text-muted ring-1 ring-inset ring-line">{r.fileset.sha.slice(0, 10)}</span>}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <div className="flex flex-wrap items-center justify-end gap-2">
            {siblings && siblings.length > 1 ? (
              <Select value={audit.id} onChange={(id) => store.selectAudit(id, "security")} className="max-w-[15rem]" title="Other audits of this case">
                {siblings.map((a) => <option key={a.id} value={a.id}>{fmtDateTime(a.created_at)} · {runStatus(a.status).label}</option>)}
              </Select>
            ) : <span className="text-xs text-faint">{fmtDateTime(audit.created_at)}</span>}
            {r.evidence && (
              <Button size="sm" variant="secondary" busy={exporting} title="Download report.md from the evidence record"
                      onClick={async () => { setExporting(true); await downloadArtifact(audit, "report.md"); setExporting(false); }}>
                <IconDownload size={14} /> Export report
              </Button>
            )}
          </div>
          {right}
        </div>
      </div>
      {actions && <div className="mt-4 border-t border-line pt-3">{actions}</div>}
    </section>
  );
}
