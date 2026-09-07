from pathlib import Path

from aftergraph_work_intelligence.production_drift import (
    evaluate_snapshot,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "ops" / "production-runtime-policy.json"


def canonical_snapshot() -> dict:
    return {
        "backend": {
            "active": True,
            "user": "work-intelligence",
            "group": "work-intelligence",
            "environment_files": ["/etc/aftergraph/work-intelligence.env"],
            "dropins": [],
        },
        "frontend": {
            "active": True,
            "user": "work-intelligence-web",
            "group": "work-intelligence-web",
            "environment_files": ["/etc/aftergraph/work-intelligence-web.env"],
            "dropins": [],
        },
        "listeners": ["172.17.0.1:8090", "0.0.0.0:3001"],
        "cloudflared": {
            "processes": [
                "cloudflared --no-autoupdate tunnel --loglevel info run"
            ],
            "containers": [
                {
                    "name": "renos-control-tunnel-1",
                    "image": "cloudflare/cloudflared:latest",
                    "command": "cloudflared --no-autoupdate tunnel run",
                    "networks": ["renos-control-edge", "renos-control-internal"],
                    "running": True,
                }
            ],
        },
        "checkout": {
            "clean": True,
            "deployed_sha": "a" * 40,
            "latest_main_sha": "b" * 40,
        },
    }


def test_canonical_snapshot_passes_even_when_main_has_advanced() -> None:
    policy = load_policy(POLICY)
    result = evaluate_snapshot(canonical_snapshot(), policy)
    assert result["status"] == "PASS"
    assert result["checkout"]["deployed_sha"] != result["checkout"]["latest_main_sha"]
    assert all(check["ok"] for check in result["checks"])


def test_quick_tunnel_is_rejected() -> None:
    snapshot = canonical_snapshot()
    snapshot["cloudflared"]["processes"].append(
        "cloudflared tunnel --url http://127.0.0.1:8090"
    )
    snapshot["cloudflared"]["containers"].append(
        {
            "name": "wi-quick-tunnel",
            "image": "cloudflare/cloudflared:latest",
            "command": "cloudflared tunnel --url http://127.0.0.1:8090",
            "networks": ["host"],
            "running": True,
        }
    )
    result = evaluate_snapshot(snapshot, load_policy(POLICY))
    assert result["status"] == "FAIL"
    failed = {c["id"] for c in result["checks"] if not c["ok"]}
    assert "cloudflared.no_quick_tunnels" in failed
    assert "cloudflared.canonical_container" in failed


def test_dirty_checkout_and_backend_dropin_fail_closed() -> None:
    snapshot = canonical_snapshot()
    snapshot["checkout"]["clean"] = False
    snapshot["backend"]["dropins"] = ["/etc/systemd/system/work-intelligence.service.d/rogue.conf"]
    result = evaluate_snapshot(snapshot, load_policy(POLICY))
    failed = {c["id"] for c in result["checks"] if not c["ok"]}
    assert result["status"] == "FAIL"
    assert "checkout.clean" in failed
    assert "backend.no_dropins" in failed


def test_listener_and_identity_drift_are_rejected() -> None:
    snapshot = canonical_snapshot()
    snapshot["listeners"] = ["0.0.0.0:8090", "0.0.0.0:3001"]
    snapshot["frontend"]["user"] = "root"
    result = evaluate_snapshot(snapshot, load_policy(POLICY))
    failed = {c["id"] for c in result["checks"] if not c["ok"]}
    assert result["status"] == "FAIL"
    assert "backend.listener" in failed
    assert "frontend.identity" in failed
