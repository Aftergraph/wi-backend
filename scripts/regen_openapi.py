"""Regenerate openapi.json from the FastAPI app (single source of truth).

Usage: python scripts/regen_openapi.py [--check]
  --check: exit 1 if the checked-in openapi.json differs (for CI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aftergraph_work_intelligence.api import create_app

SPEC_PATH = ROOT / "openapi.json"


def build_spec() -> dict:
    app = create_app(db_path=":memory:")
    return app.openapi()


def main() -> int:
    spec = build_spec()
    rendered = json.dumps(spec, indent=2) + "\n"
    if "--check" in sys.argv[1:]:
        current = SPEC_PATH.read_text() if SPEC_PATH.exists() else ""
        if current != rendered:
            print("openapi.json is stale — run: python scripts/regen_openapi.py")
            return 1
        print("openapi.json is current.")
        return 0
    SPEC_PATH.write_text(rendered)
    print(f"wrote {SPEC_PATH} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
