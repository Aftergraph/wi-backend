"""Minimal runnable check for the autonomy gate's signal derivation.

The gate must NEVER trust caller-provided flags -- it derives them from the
changed-file list itself. These asserts fail if that independence breaks.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_SPEC = importlib.util.spec_from_file_location(
    "autonomy_gate", Path(__file__).resolve().parent.parent / "scripts" / "autonomy-gate.py"
)
autonomy_gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(autonomy_gate)  # type: ignore[union-attr]
derive_signals = autonomy_gate.derive_signals


def test_detects_auth_secret_from_filename():
    signals = derive_signals(["src/api/auth.py", "README.md"])
    assert signals["auth_or_secret_touched"] is True
    assert signals["proxy_or_ssl_touched"] is False


def test_detects_proxy_ssl_from_filename():
    signals = derive_signals(["infra/caddy/Caddyfile"])
    assert signals["proxy_or_ssl_touched"] is True
    assert signals["auth_or_secret_touched"] is False


def test_detects_critical_from_security_path():
    signals = derive_signals(["security/policies/access.yaml"])
    assert signals["critical_file_touched"] is True


def test_benign_files_trigger_nothing():
    signals = derive_signals(["src/utils/format.ts", "docs/README.md", "package.json"])
    assert signals == {
        "auth_or_secret_touched": False,
        "proxy_or_ssl_touched": False,
        "critical_file_touched": False,
        "changed_files": ["src/utils/format.ts", "docs/README.md", "package.json"],
    }


if __name__ == "__main__":
    for fn in [test_detects_auth_secret_from_filename, test_detects_proxy_ssl_from_filename,
               test_detects_critical_from_security_path, test_benign_files_trigger_nothing]:
        fn()
    print("autonomy-gate signal checks: 4 passed")