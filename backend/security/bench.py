"""SiliconBench: measured, provenance-labelled runs over the security cases.

Arms (each recorded per case, never fabricated):
  harness              verifier on the vulnerable source and on the hidden reference fix,
                       plus the formal check on both. Validates the case itself: a case
                       counts only if the vulnerable source fails and the reference passes.
  detect:<provider>    one investigation (rules | bitdeer | anthropic). Correct when a
                       finding overlaps the hidden ground-truth lines AND names one of
                       its properties. Model arms without a key are NOT_RUN.
  repair:baseline:<p>  one model patch from the contract + source only, no verifier
                       evidence, no retries (same model as the assisted arm)
  repair:assisted:<p>  model patch with the verifier's failing evidence and up to N
                       retries fed by retest results
  repair:factory       Factory Droid with the same evidence

Metrics are computed from stored records with explicit numerators, denominators
and not-run counts. Demo cases are labelled as such; nothing here is a
historical-vulnerability metric unless a case's source_kind is "historical".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from pathlib import Path

from backend.core.events import bus
from backend.security import formal as formal_mod
from backend.security import investigate as inv
from backend.security import repair as rep
from backend.security.cases import CaseManifest, get_case, list_cases
from backend.security.verify import verify_sources
from backend.storage import store
from backend.storage.db import now

log = logging.getLogger("siliconbreak.bench")

# Free arms run by default; paid arms must be requested explicitly and their worst-case
# cost confirmed (see estimate()); the credit guard still applies to every call.
DEFAULT_ARMS = ["harness", "detect:rules"]
PAID_ARMS = ["detect:bitdeer", "detect:anthropic", "repair:baseline:auto", "repair:assisted:auto", "repair:factory"]
ALL_ARMS = DEFAULT_ARMS + PAID_ARMS
ARM_LABELS = {
    "harness": "Harness validity (vulnerable fails, reference passes)",
    "detect:rules": "Detection: offline static rules (not AI)",
    "detect:bitdeer": "Detection: Bitdeer",
    "detect:anthropic": "Detection: Claude",
    "repair:baseline:auto": "Repair: model, single shot without tools",
    "repair:assisted:auto": "Repair: same model with verifier loop",
    "repair:factory": "Repair: Factory Droid with verifier evidence",
}

_tasks: dict[str, asyncio.Task] = {}


def detection_correct(case: CaseManifest, findings: list[dict]) -> bool | None:
    gt = case.ground_truth
    if gt is None:
        return None
    lo, hi = gt.lines
    for f in findings:
        if f.get("file") == gt.file and f["line_start"] <= hi and f["line_end"] >= lo \
                and set(f.get("violated_properties", [])) & set(gt.properties):
            return True
    return False


async def _harness(case: CaseManifest, d: Path) -> dict:
    vul = await verify_sources(case, {r: case.path(r) for r in case.editable}, d / "harness_vulnerable", label="vulnerable")
    out = {"vulnerable_status": vul.status, "failed_properties": vul.failed_properties,
           "vulnerable_fails": vul.security_status == "failed" if vul.status in ("passed", "failed") else None,
           "reference_status": "not_run", "reference_passes": None, "formal_vulnerable": None, "formal_reference": None}
    fv = await formal_mod.check(case, [case.path(r) for r in case.editable], d / "formal_vulnerable", label="vulnerable")
    out["formal_vulnerable"] = fv.status
    if case.reference_fix:
        cands = {r: case.path(case.reference_fix.files.get(r, r)) for r in case.editable}
        ref = await verify_sources(case, cands, d / "harness_reference", label="reference")
        out["reference_status"] = ref.status
        out["reference_passes"] = ref.status == "passed"
        fr = await formal_mod.check(case, list(cands.values()), d / "formal_reference", label="reference")
        out["formal_reference"] = fr.status
    return out


async def _detect(case: CaseManifest, provider: str, d: Path) -> dict:
    name, prov, note = inv.pick_provider(provider)
    if name != provider:
        return {"status": "not_run", "provider": provider, "model": None, "is_ai": provider != "rules", "correct": None,
                "findings": 0, "reason": note or f"{provider} is not configured"}
    files = {r: case.path(r).read_text() for r in case.editable}
    rec = await inv.investigate(case.security_contract.model_dump(), [p.model_dump() for p in case.interface], files,
                                d / f"detect_{provider}", provider_override=(name, prov, note))
    if rec["status"] != "ok":
        return {"status": "error", "provider": provider, "model": rec.get("model"), "is_ai": prov is not None,
                "correct": None, "findings": 0, "reason": (rec.get("error") or {}).get("message")}
    return {"status": "ran", "provider": provider, "model": rec.get("model"), "is_ai": prov is not None,
            "correct": detection_correct(case, rec["findings"]), "findings": len(rec["findings"]),
            "duration_s": rec.get("duration_s"), "usage": rec.get("usage")}


async def _repair(case: CaseManifest, arm: str, d: Path, harness: dict, max_attempts: int) -> dict:
    if not harness.get("vulnerable_fails"):
        return {"status": "not_run", "verdict": None, "reason": "the vulnerable source does not fail its security tests"}
    srcs = {r: case.path(r) for r in case.editable}
    rep_path = d / "harness_vulnerable" / "report.json"
    report = json.loads(rep_path.read_text()) if rep_path.is_file() else None
    if arm == "repair:factory":
        st = rep.droid_status()
        if st["state"] != "configured":
            return {"status": "not_run", "verdict": None,
                    "reason": st["reason"] or "Factory Droid is not configured (set FACTORY_API_KEY)"}
        attempts, agent = max_attempts, "factory"
        prov = None
    else:
        name, prov, note = inv.pick_provider("auto")
        if prov is None:
            return {"status": "not_run", "verdict": None, "reason": note or "no model provider configured"}
        agent = "model"
        attempts = 1 if ":baseline:" in arm else max_attempts
    baseline = ":baseline:" in arm
    evidence = "No verifier evidence is provided in this arm." if baseline else rep.failing_evidence_text(case, report, None)
    tag = arm.replace(":", "_")
    verdict, tries, model = None, 0, None
    for n in range(1, attempts + 1):
        tries = n
        ws = d / tag / f"attempt-{n}" / "workspace"
        task = rep.task_markdown(case, evidence, None)
        before = rep.make_workspace(case, srcs, ws, task)
        if agent == "factory":
            async def _noop(kind: str, item: dict) -> None:
                return None
            arec = await rep.repair_with_droid(case, ws, on_event=_noop)
        else:
            arec = await rep.repair_with_model(case, ws, (name, prov), task_md=task)
            model = arec.get("model")
        ch = rep.collect_changes(case, ws, before, srcs)
        if arec["status"] != "completed" or not ch["changed_files"]:
            verdict = "no_patch"
            if arec["status"] in ("error", "unavailable"):
                return {"status": "error", "verdict": verdict, "reason": arec.get("reason"), "attempts": tries, "model": model}
            continue
        if ch["scope_violations"]:
            verdict = "rejected_scope"
            continue
        v = await verify_sources(case, ch["candidates"], d / tag / f"attempt-{n}" / "verify", label=f"{tag}-{n}")
        verdict = "verified" if v.status == "passed" else "failed"
        if verdict == "verified":
            break
        evidence = rep.failing_evidence_text(case, v.model_dump(), None) if not baseline else evidence
    return {"status": "ran", "verdict": verdict, "attempts": tries, "model": model}


def compute_metrics(cases: list[dict], arms: list[str]) -> dict:
    m: dict[str, dict] = {}
    hv = [c for c in cases if c["harness"]]
    valid = [c for c in hv if c["harness"].get("vulnerable_fails") and c["harness"].get("reference_passes")]
    if "harness" in arms:
        m["harness"] = {"label": ARM_LABELS["harness"], "numerator": len(valid), "denominator": len(hv), "not_run": len(cases) - len(hv)}
        m["reproduction"] = {"label": "Reproduction: vulnerable source fails an independent security test",
                             "numerator": sum(1 for c in hv if c["harness"].get("vulnerable_fails")), "denominator": len(hv),
                             "not_run": len(cases) - len(hv)}
        fv = [c for c in hv if c["harness"].get("formal_vulnerable") not in (None, "not_run")]
        m["formal"] = {"label": "Formal: counterexample on vulnerable and proof on reference",
                       "numerator": sum(1 for c in fv if c["harness"]["formal_vulnerable"] == "counterexample"
                                        and c["harness"].get("formal_reference") == "proven"),
                       "denominator": len(fv), "not_run": len(cases) - len(fv)}
    for arm in arms:
        if arm.startswith("detect:"):
            rows = [c["detection"].get(arm) for c in valid]
            ran = [r for r in rows if r and r["status"] == "ran"]
            m[arm] = {"label": ARM_LABELS.get(arm, arm), "numerator": sum(1 for r in ran if r["correct"]),
                      "denominator": len(ran), "not_run": len(rows) - len(ran)}
        elif arm.startswith("repair:"):
            rows = [c["repair"].get(arm) for c in valid]
            ran = [r for r in rows if r and r["status"] == "ran"]
            m[arm] = {"label": ARM_LABELS.get(arm, arm), "numerator": sum(1 for r in ran if r["verdict"] == "verified"),
                      "denominator": len(ran), "not_run": len(rows) - len(ran)}
    return m


async def run_bench(bench_id: str) -> None:
    b = store.get_bench_run(bench_id)
    cfg = b["config"]
    root = Path(b["dir"])
    results: list[dict] = []
    try:
        for cid in cfg["case_ids"]:
            case = get_case(cid)
            if case is None:
                continue
            t0 = time.monotonic()
            d = root / cid
            d.mkdir(parents=True, exist_ok=True)
            row = {"case_id": cid, "title": case.title, "source_kind": case.source_kind, "cwe": case.cwe,
                   "provenance": case.provenance, "repository": case.repository, "commit": case.commit,
                   "harness": None, "detection": {}, "repair": {}, "duration_s": 0.0}
            harness = await _harness(case, d) if "harness" in cfg["arms"] or any(a.startswith("repair:") for a in cfg["arms"]) else {}
            row["harness"] = harness or None
            for arm in cfg["arms"]:
                if arm.startswith("detect:"):
                    row["detection"][arm] = await _detect(case, arm.split(":", 1)[1], d)
                elif arm.startswith("repair:"):
                    row["repair"][arm] = await _repair(case, arm, d, harness, int(cfg.get("max_attempts", 3)))
            row["duration_s"] = round(time.monotonic() - t0, 2)
            results.append(row)
            partial = {"cases": results, "metrics": compute_metrics(results, cfg["arms"]), "tool_versions": {}, "commands": []}
            store.update_bench_run(bench_id, result=partial)
            await bus.emit("bench.updated", bench=store.get_bench_run(bench_id))
        from backend.security.verify import verilator_version

        final = {"cases": results, "metrics": compute_metrics(results, cfg["arms"]),
                 "tool_versions": {"verilator": await verilator_version(), "yosys": await formal_mod.yosys_version()},
                 "commands": ["POST /api/bench/runs " + json.dumps(cfg)]}
        store.update_bench_run(bench_id, status="finished", result=final, finished_at=now())
        (root / "bench.json").write_text(json.dumps(store.get_bench_run(bench_id), indent=2, default=str))
    except Exception as e:  # never leave a bench run "running"
        log.exception("bench %s failed", bench_id)
        store.update_bench_run(bench_id, status="failed", finished_at=now(),
                               result={"cases": results, "metrics": compute_metrics(results, cfg["arms"]),
                                       "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-3000:],
                                       "tool_versions": {}, "commands": []})
    await bus.emit("bench.updated", bench=store.get_bench_run(bench_id))


class ConfirmationRequired(Exception):
    def __init__(self, estimate: dict):
        super().__init__("paid arms selected: confirm the worst-case cost")
        self.estimate = estimate


def _resolve(case_ids: list[str] | None, arms: list[str] | None, max_attempts: int) -> tuple[list[str], list[str], int]:
    known = [c.case_id for c in list_cases()]
    ids = [c for c in (case_ids or known) if c in known]
    if not ids:
        raise ValueError("no known cases selected")
    return ids, [a for a in (arms or DEFAULT_ARMS) if a in ARM_LABELS], max(1, min(max_attempts, 5))


def estimate(case_ids: list[str] | None, arms: list[str] | None, max_attempts: int = 3) -> dict:
    """Worst-case spend of a bench run, per provider (every call at max tokens, every attempt used)."""
    from backend.core import budget
    from backend.core.config import get_settings

    s = get_settings()
    ids, arms, attempts = _resolve(case_ids, arms, max_attempts)
    usd = {"anthropic": 0.0, "bitdeer": 0.0}
    calls = {"anthropic": 0, "bitdeer": 0}
    sessions = 0
    repair_provider = "bitdeer" if s.bitdeer_api_key else ("anthropic" if s.anthropic_api_key else None)
    for cid in ids:
        case = get_case(cid)
        chars = sum(len(case.path(r).read_text()) for r in case.editable) + 9000  # + contract, task, schema
        for arm in arms:
            if arm.startswith("detect:") and arm != "detect:rules":
                prov = arm.split(":", 1)[1]
                model = s.llm_model if prov == "anthropic" else s.bitdeer_model
                usd[prov] += budget.estimate_call(prov, model, chars, 16000)
                calls[prov] += 1
            elif arm in ("repair:baseline:auto", "repair:assisted:auto") and repair_provider:
                n = 1 if "baseline" in arm else attempts
                model = s.llm_model if repair_provider == "anthropic" else s.bitdeer_model
                usd[repair_provider] += n * budget.estimate_call(repair_provider, model, chars, 16000)
                calls[repair_provider] += n
            elif arm == "repair:factory":
                sessions += min(attempts, s.droid_max_attempts)
    return {"case_ids": ids, "arms": arms, "max_attempts": attempts,
            "usd": {k: round(v, 4) for k, v in usd.items()}, "usd_total": round(sum(usd.values()), 4),
            "calls": calls, "droid_sessions": sessions,
            "paid": any(a in PAID_ARMS for a in arms), "live_enabled": budget.live_enabled(),
            "note": "worst case: every call at max tokens and every repair attempt used; Bitdeer prices are high placeholders"}


def start(case_ids: list[str] | None, arms: list[str] | None, max_attempts: int = 3, *,
          confirm_max_usd: float | None = None, confirm_droid_sessions: int | None = None) -> dict:
    est = estimate(case_ids, arms, max_attempts)
    ids, arms, attempts = est["case_ids"], est["arms"], est["max_attempts"]
    if est["paid"]:
        if (confirm_max_usd is None or confirm_max_usd + 1e-9 < est["usd_total"]
                or (est["droid_sessions"] and (confirm_droid_sessions or 0) < est["droid_sessions"])):
            raise ConfirmationRequired(est)
    b = store.create_bench_run({"case_ids": ids, "arms": arms, "max_attempts": attempts, "estimate": est})
    _tasks[b["id"]] = asyncio.create_task(run_bench(b["id"]))
    _tasks[b["id"]].add_done_callback(lambda _t, k=b["id"]: _tasks.pop(k, None))
    return b
