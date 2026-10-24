"""Import and index a public GitHub repository at a pinned commit (read-only).

Safety:
  * only https://github.com/<owner>/<repo> URLs; ref/subdir validated; no credentials
    (GIT_TERMINAL_PROMPT=0, askpass disabled), so private repositories simply fail
  * the ref is resolved to a 40-hex SHA first (`git ls-remote`), and everything after
    that uses the SHA; HEAD is re-checked after checkout
  * partial, shallow fetch (`--depth 1 --filter=blob:none`) plus a sparse checkout of
    RTL files only (optionally inside one subdirectory), so large repositories stay small
  * hooks disabled, symlinks checked out as plain files and skipped by the indexer,
    submodules and LFS never fetched, nothing from the repository is executed
  * file count / byte caps; files over the per-file cap are listed but not read

The index is deterministic: modules, includes, approximate instantiations and a
keyword-based security-relevance score used to pick files for a budgeted audit.
A complete index does not mean the repository was audited; audits record exactly
which files they read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from backend.core.process import ensure_within, run_command
from backend.storage import store

GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){0,15}$")
RTL_EXT = (".v", ".sv", ".vh", ".svh")
MAX_FILES = 5000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false",
           "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1"}

# keyword -> weight for the security-relevance score (identifiers and paths)
KEYWORDS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"debug|dbg|jtag|dmi|dft|scan_?mode|test_?mode", re.I), 3.0),
    (re.compile(r"lock|locked|unlock", re.I), 3.0),
    (re.compile(r"secret|key|otp|fuse|password|token|seed", re.I), 3.0),
    (re.compile(r"priv|auth|permission|access|allow|grant|deny|pmp|mpu", re.I), 2.0),
    (re.compile(r"lc_ctrl|lifecycle|life_cycle|lc_state|lc_tx", re.I), 2.0),
    (re.compile(r"zeroiz|wipe|scrub|scrambl|sanitiz|clear_secret|erase", re.I), 2.0),
    (re.compile(r"bypass|override|backdoor|escalat", re.I), 3.0),
    (re.compile(r"csr|regfile|reg_top", re.I), 0.5),
]

_MODULE_RE = re.compile(r"^\s*module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", re.M)
_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)", re.M)
_INCLUDE_RE = re.compile(r'^\s*`include\s+"([^"]+)"', re.M)
_INST_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:#\s*\([^;]*?\))?\s+([A-Za-z_]\w*)\s*\(", re.M | re.S)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


class RepoError(Exception):
    pass


def parse_url(url: str) -> tuple[str, str]:
    m = GITHUB_RE.match((url or "").strip())
    if not m:
        raise RepoError("only public https://github.com/<owner>/<repo> URLs are supported")
    owner, name = m.group(1), m.group(2)
    if owner in (".", "..") or name in (".", ".."):
        raise RepoError("invalid repository name")
    return owner, name


def validate_subdir(subdir: str | None) -> str:
    sd = (subdir or "").strip().strip("/")
    if not sd:
        return ""
    if not SUBDIR_RE.match(sd) or any(p in (".", "..") for p in sd.split("/")):
        raise RepoError("invalid subdirectory")
    return sd


async def resolve_ref(url: str, ref: str | None) -> tuple[str, str]:
    """-> (sha, ref label)."""
    r = (ref or "").strip()
    if r and SHA_RE.match(r):
        return r, r
    if r and (not REF_RE.match(r) or ".." in r or r.startswith("-")):
        raise RepoError("invalid ref")
    want = r or "HEAD"
    proc = await run_command(["git", "ls-remote", "--", url, want] + ([f"refs/heads/{r}", f"refs/tags/{r}", f"refs/tags/{r}^{{}}"] if r else []),
                             timeout_s=60, env=GIT_ENV)
    if not proc.ok:
        raise RepoError(f"could not reach the repository (exit {proc.exit_code}): " + " ".join(proc.output[-3:])[:300])
    refs = {}
    for line in proc.output:
        parts = line.split()
        if len(parts) == 2 and SHA_RE.match(parts[0]):
            refs[parts[1]] = parts[0]
    for key in ([f"refs/tags/{r}^{{}}", f"refs/tags/{r}", f"refs/heads/{r}", r] if r else ["HEAD"]):
        if key in refs:
            return refs[key], want
    raise RepoError(f"ref {want!r} not found")


def repos_dir() -> Path:
    return store.security_dir() / "repos"


def repo_id_for(owner: str, name: str, sha: str, subdir: str) -> str:
    h = hashlib.sha256(f"{owner}/{name}@{sha}:{subdir}".encode()).hexdigest()[:10]
    return f"repo_{h}"


def score_text(path: str, text: str) -> tuple[float, dict[str, int]]:
    idents = _IDENT_RE.findall(text)
    hits: dict[str, int] = {}
    score = 0.0
    for pat, w in KEYWORDS:
        n = sum(1 for t in idents if pat.search(t))
        n_path = 1 if pat.search(path) else 0
        if n or n_path:
            hits[pat.pattern.split("|")[0]] = n + n_path
            score += w * (min(n, 25) ** 0.5) + 2 * w * n_path
    return round(score, 2), hits


async def import_repo(url: str, ref: str | None = None, subdir: str | None = None, *, on_line=None) -> dict:
    owner, name = parse_url(url)
    sd = validate_subdir(subdir)
    clean_url = f"https://github.com/{owner}/{name}.git"
    sha, ref_label = await resolve_ref(clean_url, ref)
    rid = repo_id_for(owner, name, sha, sd)
    root = repos_dir() / rid
    idx_path = root / "index.json"
    if idx_path.is_file():
        return json.loads(idx_path.read_text())
    if root.exists():
        shutil.rmtree(root)
    work = root / "src"
    work.mkdir(parents=True)
    commands: list[str] = []

    async def git(*args: str, timeout: float = 120) -> list[str]:
        argv = ["git", "-C", str(work), *args]
        commands.append("git " + " ".join(args))
        proc = await run_command(argv, timeout_s=timeout, env=GIT_ENV, on_line=on_line)
        if not proc.ok:
            raise RepoError(f"git {args[0]} failed (exit {proc.exit_code}): " + " ".join(proc.output[-4:])[:400])
        return proc.output

    t0 = time.monotonic()
    try:
        await git("init", "-q")
        await git("config", "core.hooksPath", "/dev/null")
        await git("config", "core.symlinks", "false")
        await git("config", "submodule.recurse", "false")
        await git("config", "advice.detachedHead", "false")
        await git("remote", "add", "origin", clean_url)
        await git("sparse-checkout", "init", "--no-cone")
        prefix = f"/{sd}/**/" if sd else ""
        await git("sparse-checkout", "set", "--no-cone", *[f"{prefix}*{ext}" for ext in RTL_EXT])
        await git("fetch", "-q", "--depth", "1", "--filter=blob:none", "--no-tags", "origin", sha, timeout=600)
        await git("checkout", "-q", "FETCH_HEAD", timeout=900)
        head = (await git("rev-parse", "HEAD"))[0].strip()
        if head != sha:
            raise RepoError(f"checked-out commit {head} does not match the pinned {sha}")
        links = set()
        for line in await git("ls-files", "-s"):
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[0] == "120000":
                links.add(parts[3])
    except RepoError:
        shutil.rmtree(root, ignore_errors=True)
        raise

    files, modules, total, truncated = [], {}, 0, False
    for p in sorted(work.rglob("*")):
        rel = p.relative_to(work).as_posix()
        if rel.startswith(".git/") or not p.is_file() or p.suffix not in RTL_EXT:
            continue
        if sd and not rel.startswith(sd + "/"):
            continue
        if rel in links or p.is_symlink():
            continue
        ensure_within(p, work)
        size = p.stat().st_size
        if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
            truncated = True
            break
        entry = {"path": rel, "bytes": size, "lines": 0, "modules": [], "packages": [], "includes": [],
                 "instances": [], "score": 0.0, "keywords": {}, "skipped": None}
        if size > MAX_FILE_BYTES:
            entry["skipped"] = f"larger than {MAX_FILE_BYTES // 1024} KiB"
        else:
            text = p.read_text(errors="replace")
            total += size
            entry["lines"] = text.count("\n") + 1
            entry["sha256"] = hashlib.sha256(text.encode()).hexdigest()
            entry["modules"] = _MODULE_RE.findall(text)
            entry["packages"] = _PACKAGE_RE.findall(text)
            entry["includes"] = _INCLUDE_RE.findall(text)
            entry["score"], entry["keywords"] = score_text(rel, text)
            entry["_insts"] = list({m.group(1) for m in _INST_RE.finditer(text)})
            for m in entry["modules"]:
                modules.setdefault(m, rel)
        files.append(entry)
    for e in files:
        e["instances"] = sorted(i for i in e.pop("_insts", []) if i in modules and i not in e["modules"])
    idx = {
        "repo_id": rid, "url": f"https://github.com/{owner}/{name}", "owner": owner, "name": name,
        "ref": ref_label, "sha": sha, "subdir": sd, "imported_at": time.time(),
        "duration_s": round(time.monotonic() - t0, 1), "files": files, "modules": modules,
        "totals": {"files": len(files), "bytes": total, "modules": len(modules),
                   "lines": sum(f["lines"] for f in files)},
        "truncated": truncated, "commands": commands,
        "limits": {"max_files": MAX_FILES, "max_total_bytes": MAX_TOTAL_BYTES, "max_file_bytes": MAX_FILE_BYTES},
    }
    idx_path.write_text(json.dumps(idx))
    return idx


def load_index(repo_id: str) -> dict | None:
    if not re.fullmatch(r"repo_[0-9a-f]{10}", repo_id or ""):
        return None
    p = repos_dir() / repo_id / "index.json"
    return json.loads(p.read_text()) if p.is_file() else None


def list_repos() -> list[dict]:
    out = []
    d = repos_dir()
    if d.is_dir():
        for p in sorted(d.glob("repo_*/index.json")):
            idx = json.loads(p.read_text())
            out.append({k: idx[k] for k in ("repo_id", "url", "owner", "name", "ref", "sha", "subdir", "imported_at",
                                            "totals", "truncated")})
    return sorted(out, key=lambda r: -r["imported_at"])


def read_file(repo_id: str, rel: str) -> str:
    idx = load_index(repo_id)
    if idx is None:
        raise RepoError("unknown repository")
    entry = next((f for f in idx["files"] if f["path"] == rel), None)
    if entry is None or entry.get("skipped"):
        raise RepoError("file is not in the index")
    work = repos_dir() / repo_id / "src"
    return ensure_within(work / rel, work).read_text(errors="replace")


def select_for_audit(idx: dict, requested: list[str] | None, budget_chars: int = 110_000, max_files: int = 12) -> list[str]:
    """Explicit selection (validated against the index), else the highest-scoring files within budget."""
    by_path = {f["path"]: f for f in idx["files"] if not f.get("skipped")}
    if requested:
        sel = [p for p in requested if p in by_path][:max_files]
        if not sel:
            raise RepoError("none of the requested files are in the index")
        return sel
    ranked = sorted(by_path.values(), key=lambda f: (-f["score"], f["bytes"]))
    out, used = [], 0
    for f in ranked:
        if f["score"] <= 0:
            break
        est = f["bytes"] + 8 * f["lines"]
        if used + est > budget_chars:
            continue
        out.append(f["path"])
        used += est
        if len(out) >= max_files:
            break
    return out


def permalink(idx: dict, rel: str, a: int | None = None, b: int | None = None) -> str:
    url = f"{idx['url']}/blob/{idx['sha']}/{rel}"
    return url + (f"#L{a}-L{b}" if a and b else "")
