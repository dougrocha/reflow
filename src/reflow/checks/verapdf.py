"""`reflow audit --verapdf`: veraPDF's PDF/UA-1 validation, merged into the audit report.

veraPDF is a separate Java tool (https://verapdf.org). The audit runs it when it is on PATH
and reads its XML report: whether the file is compliant, and each failed rule with its clause
of ISO 14289-1 and the number of failed checks. A missing tool or a report it could not make
is a `skip`, so the audit still comes back.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

VeraStatus = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class VeraRule:
    clause: str        # "7.21.7"
    test: str          # "1"
    description: str
    failed_checks: int

    def to_dict(self) -> dict:
        return {"clause": self.clause, "test": self.test, "description": self.description,
                "failed_checks": self.failed_checks}


@dataclass
class VeraResult:
    status: VeraStatus
    detail: str
    rules: list[VeraRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail,
                "rules": [r.to_dict() for r in self.rules]}


def run_verapdf(pdf: Path, *, password: str = "") -> VeraResult:
    """Validate `pdf` against PDF/UA-1. Never raises."""
    exe = shutil.which("verapdf")
    if not exe:
        return VeraResult("skip", "verapdf not found on PATH")
    if password:
        # veraPDF takes a password only on its command line, where the process list shows it.
        return VeraResult("skip", "the password is not passed to veraPDF")
    try:
        out = subprocess.run(
            [exe, "--format", "xml", "--flavour", "ua1", "--maxfailuresdisplayed", "1",
             str(pdf)],
            capture_output=True, text=True, check=False).stdout
    except OSError as e:
        return VeraResult("skip", f"could not run verapdf: {e}")
    return parse_report(out)


def parse_report(xml: str) -> VeraResult:
    """The result of one job in veraPDF's XML report."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return VeraResult("skip", f"unreadable veraPDF report: {e}")
    report = root.find("./jobs/job/validationReport")
    if report is None:
        error = root.findtext("./jobs/job/taskException/exceptionMessage") or "no validation report"
        return VeraResult("skip", f"veraPDF could not validate: {error.strip()}")
    rules = [VeraRule(r.get("clause", ""), r.get("testNumber", ""),
                      " ".join((r.findtext("description") or "").split()),
                      int(r.get("failedChecks", "0")))
             for r in report.iterfind("./details/rule") if r.get("status") == "failed"]
    profile = report.get("profileName", "PDF/UA-1")
    if report.get("isCompliant") == "true":
        return VeraResult("pass", f"compliant with the {profile}")
    details = report.find("./details")
    total = len(rules) + int(details.get("passedRules", "0") if details is not None else 0)
    return VeraResult("fail", f"{len(rules)} of {total} rules of the {profile} failed", rules)
