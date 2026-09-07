"""Credential hygiene: no live-looking secret literals in production paths.

Exit rule from the credential-exposure incident (issue #24): secret values
must come from the environment, never from string literals in tracked code.
This test scans src/ and scripts/ for assignments that look like real
credential values. Only file:line locations are reported — never values.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "scripts")

ASSIGN = re.compile(
    r"""(?i)\b[A-Z_]*(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|API_KEY)\b\s*=\s*['"]([^'"]+)['"]"""
)

PLACEHOLDER_HINTS = (
    "test", "example", "placeholder", "changeme", "replace", "***",
    "<", "your", "here", "dummy", "fake", "sample", "none", "env",
)


def _looks_live(value: str) -> bool:
    lowered = value.lower()
    if len(value) < 16:
        return False
    return not any(hint in lowered for hint in PLACEHOLDER_HINTS)


def test_no_live_secret_literals_in_production_paths():
    violations = []
    for dirname in SCAN_DIRS:
        root = REPO / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                match = ASSIGN.search(line)
                if match and _looks_live(match.group(1)):
                    violations.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not violations, f"live-looking secret literals found at: {violations}"
