"""Security case manifests (security_cases/<case_id>/case.json).

A case bundles everything the trusted verifier needs to judge a design:
the editable RTL, a written security and functional contract, a trusted
testbench, an optional formal property wrapper, and (hidden) ground truth and a
human reference fix used only to validate the harness.

What an agent may see is `public_view()`; trusted files and ground truth never
leave the case directory except through the verifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from backend.core.config import REPO_ROOT

CASES_DIR = REPO_ROOT / "security_cases"
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
TEST_ID_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,47}$")
PROP_ID_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,31}$")

SourceKind = Literal["demo", "research_injected", "historical", "benchmark_adapted"]


class ContractProperty(BaseModel):
    id: str
    text: str

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not PROP_ID_RE.match(v):
            raise ValueError(f"invalid property id {v!r}")
        return v


class SecurityContract(BaseModel):
    summary: str
    properties: list[ContractProperty]


class TestSpec(BaseModel):
    id: str
    kind: Literal["security", "functional"]
    properties: list[str] = Field(default_factory=list)
    title: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not TEST_ID_RE.match(v):
            raise ValueError(f"invalid test id {v!r}")
        return v


class VerificationSpec(BaseModel):
    testbench: str
    tb_top: str = "tb"
    timeout_s: float = 120
    tests: list[TestSpec]


class FormalSpec(BaseModel):
    wrapper: str
    top: str
    properties: list[str] = Field(default_factory=list)
    max_steps: int = 20


class GroundTruth(BaseModel):
    file: str
    lines: tuple[int, int]
    properties: list[str]
    weakness: str = ""
    summary: str = ""


class ReferenceFix(BaseModel):
    files: dict[str, str]
    author: str = ""


class BoardSpec(BaseModel):
    wrapper: str
    mapping: list[dict] = Field(default_factory=list)


class PortSpec(BaseModel):
    port: str
    dir: Literal["input", "output"]
    width: int = 1
    text: str = ""


class CaseManifest(BaseModel):
    case_id: str
    title: str
    source_kind: SourceKind
    provenance: str
    cwe: list[str] = Field(default_factory=list)
    repository: Optional[str] = None
    commit: Optional[str] = None
    license: str = ""
    top_module: str
    editable: list[str]
    description: str = ""
    interface: list[PortSpec] = Field(default_factory=list)
    security_contract: SecurityContract
    functional_contract: list[ContractProperty] = Field(default_factory=list)
    verification: VerificationSpec
    formal: Optional[FormalSpec] = None
    ground_truth: Optional[GroundTruth] = None
    reference_fix: Optional[ReferenceFix] = None
    board: Optional[BoardSpec] = None

    # set by the loader
    dir: Path = Field(default=Path("."), exclude=True)

    @field_validator("case_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not CASE_ID_RE.match(v):
            raise ValueError(f"invalid case id {v!r}")
        return v

    # ------------------------------------------------------------------ paths
    def path(self, rel: str) -> Path:
        p = (self.dir / rel).resolve()
        if self.dir.resolve() not in p.parents:
            raise ValueError(f"{rel!r} escapes the case directory")
        return p

    def trusted_files(self) -> list[str]:
        files = [self.verification.testbench]
        if self.formal:
            files.append(self.formal.wrapper)
        if self.board:
            files.append(self.board.wrapper)
        return files

    def trusted_hashes(self) -> dict[str, str]:
        return {rel: sha256_file(self.path(rel)) for rel in self.trusted_files()}

    def test(self, test_id: str) -> TestSpec | None:
        return next((t for t in self.verification.tests if t.id == test_id), None)

    def property_ids(self) -> list[str]:
        return [p.id for p in self.security_contract.properties]

    # ------------------------------------------------------------- disclosure
    def public_view(self) -> dict:
        """What the UI, investigators and repair agents may see (no ground truth,
        no reference fix, no trusted testbench source)."""
        return {
            "case_id": self.case_id,
            "title": self.title,
            "source_kind": self.source_kind,
            "provenance": self.provenance,
            "cwe": self.cwe,
            "repository": self.repository,
            "commit": self.commit,
            "license": self.license,
            "top_module": self.top_module,
            "editable": self.editable,
            "description": self.description,
            "interface": [p.model_dump() for p in self.interface],
            "security_contract": self.security_contract.model_dump(),
            "functional_contract": [p.model_dump() for p in self.functional_contract],
            "tests": [t.model_dump() for t in self.verification.tests],
            "formal": {"available": self.formal is not None,
                       "properties": self.formal.properties if self.formal else []},
            "has_reference_fix": self.reference_fix is not None,
            "board": self.board.model_dump() if self.board else None,
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load(path: Path) -> CaseManifest:
    data = json.loads(path.read_text())
    case = CaseManifest.model_validate(data)
    case.dir = path.parent.resolve()
    if case.case_id != path.parent.name:
        raise ValueError(f"{path}: case_id {case.case_id!r} does not match its directory")
    for rel in [*case.editable, *case.trusted_files()]:
        if not case.path(rel).is_file():
            raise ValueError(f"{path}: missing file {rel}")
    if case.reference_fix:
        for target, src in case.reference_fix.files.items():
            if target not in case.editable:
                raise ValueError(f"{path}: reference fix targets non-editable file {target}")
            if not case.path(src).is_file():
                raise ValueError(f"{path}: missing reference file {src}")
    prop_ids = set(case.property_ids()) | {p.id for p in case.functional_contract}
    for t in case.verification.tests:
        unknown = set(t.properties) - prop_ids
        if unknown:
            raise ValueError(f"{path}: test {t.id} names unknown properties {sorted(unknown)}")
    return case


@lru_cache(maxsize=1)
def _all_cases() -> dict[str, CaseManifest]:
    out: dict[str, CaseManifest] = {}
    if CASES_DIR.is_dir():
        for p in sorted(CASES_DIR.glob("*/case.json")):
            case = _load(p)
            out[case.case_id] = case
    return out


def list_cases() -> list[CaseManifest]:
    return list(_all_cases().values())


def get_case(case_id: str) -> CaseManifest | None:
    if not CASE_ID_RE.match(case_id or ""):
        return None
    return _all_cases().get(case_id)


def reload_cases() -> None:
    _all_cases.cache_clear()
