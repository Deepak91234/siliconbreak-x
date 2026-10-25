/** Report export for the Security Lab audit header. */

import { errorMessage } from "../../api/client";
import { securityApi, type SecurityAudit } from "../../api/security";
import { toast } from "../../lib/toast";

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
