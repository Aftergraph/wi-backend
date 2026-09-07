from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "production-runtime-policy.json"
)


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        policy = json.load(handle)
    if policy.get("schema_version") != "aftergraph.production-runtime-policy/1.0":
        raise ValueError("unsupported production runtime policy schema")
    return policy


def _run(args: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout


def _paths(value: str) -> list[str]:
    return re.findall(r"/[^\s]+", value or "")


def _service_snapshot(service: str, errors: list[str]) -> dict[str, Any]:
    rc, output = _run(
        [
            "systemctl",
            "show",
            service,
            "--property=ActiveState,User,Group,EnvironmentFiles,DropInPaths",
            "--no-pager",
        ]
    )
    if rc != 0:
        errors.append(f"systemctl:{service}:rc={rc}")
    props: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return {
        "active": props.get("ActiveState") == "active",
        "user": props.get("User", ""),
        "group": props.get("Group", ""),
        "environment_files": _paths(props.get("EnvironmentFiles", "")),
        "dropins": _paths(props.get("DropInPaths", "")),
    }


def _listener_snapshot(errors: list[str]) -> list[str]:
    rc, output = _run(["ss", "-H", "-ltn"])
    if rc != 0:
        errors.append(f"ss:rc={rc}")
        return []
    listeners: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            listeners.append(fields[3])
    return sorted(set(listeners))


def _cloudflared_processes(errors: list[str]) -> list[dict[str, Any]]:
    rc, output = _run(["ps", "-eo", "pid=,args="])
    if rc != 0:
        errors.append(f"ps:rc={rc}")
        return []
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or "cloudflared" not in stripped:
            continue
        pid_text, _, args = stripped.partition(" ")
        if pid_text == str(os.getpid()):
            continue
        if "production_drift" in args:
            continue
        records.append(
            {
                "pid": int(pid_text) if pid_text.isdigit() else None,
                "quick_tunnel": "--url" in args,
            }
        )
    return records


def _cloudflared_containers(errors: list[str]) -> list[dict[str, Any]]:
    rc, output = _run(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
    )
    if rc != 0:
        errors.append(f"docker:ps:rc={rc}")
        return []
    containers: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            errors.append("docker:ps:invalid_json")
            continue
        image = str(item.get("Image", ""))
        command = str(item.get("Command", ""))
        name = str(item.get("Names", ""))
        if "cloudflared" not in image.lower() and "cloudflared" not in command.lower():
            continue
        networks = [part.strip() for part in str(item.get("Networks", "")).split(",") if part.strip()]
        containers.append(
            {
                "name": name,
                "image": image,
                "networks": sorted(networks),
                "running": str(item.get("State", "")).lower() == "running",
                "quick_tunnel": "--url" in command,
            }
        )
    return containers


def _checkout_snapshot(
    repo_dir: str | Path,
    errors: list[str],
    expected_sha: str | None,
) -> dict[str, Any]:
    repo = str(repo_dir)
    rc_status, status = _run(["git", "-C", repo, "status", "--porcelain"])
    rc_head, head = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    rc_main, main = _run(["git", "-C", repo, "rev-parse", "origin/main"])
    if rc_status != 0:
        errors.append(f"git:status:rc={rc_status}")
    if rc_head != 0:
        errors.append(f"git:head:rc={rc_head}")
    if rc_main != 0:
        errors.append(f"git:origin_main:rc={rc_main}")
    return {
        "clean": rc_status == 0 and status.strip() == "",
        "deployed_sha": head.strip() if rc_head == 0 else None,
        "latest_main_sha": main.strip() if rc_main == 0 else None,
        "expected_sha": expected_sha,
    }


def collect_snapshot(
    repo_dir: str | Path = "/opt/work-intelligence",
    expected_sha: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    return {
        "collector_errors": errors,
        "backend": _service_snapshot("work-intelligence", errors),
        "frontend": _service_snapshot("work-intelligence-web", errors),
        "listeners": _listener_snapshot(errors),
        "cloudflared": {
            "processes": _cloudflared_processes(errors),
            "containers": _cloudflared_containers(errors),
        },
        "checkout": _checkout_snapshot(repo_dir, errors, expected_sha),
    }


def _add_check(
    checks: list[dict[str, Any]],
    check_id: str,
    ok: bool,
    expected: Any,
    observed: Any,
) -> None:
    checks.append(
        {
            "id": check_id,
            "ok": bool(ok),
            "expected": expected,
            "observed": observed,
        }
    )


def _quick_tunnel(record: Any) -> bool:
    if isinstance(record, str):
        return "--url" in record
    if isinstance(record, dict):
        if "quick_tunnel" in record:
            return bool(record["quick_tunnel"])
        return "--url" in str(record.get("command", ""))
    return False


def _service_checks(
    checks: list[dict[str, Any]],
    name: str,
    observed: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    _add_check(checks, f"{name}.active", observed.get("active") is True, True, observed.get("active"))
    identity = [observed.get("user"), observed.get("group")]
    wanted_identity = [expected["user"], expected["group"]]
    _add_check(checks, f"{name}.identity", identity == wanted_identity, wanted_identity, identity)
    env_files = observed.get("environment_files", [])
    _add_check(checks, f"{name}.env", env_files == [expected["environment_file"]], [expected["environment_file"]], env_files)
    if not expected.get("allow_dropins", False):
        dropins = observed.get("dropins", [])
        _add_check(checks, f"{name}.no_dropins", dropins == [], [], dropins)


def _listener_checks(
    checks: list[dict[str, Any]],
    listeners: list[str],
    name: str,
    expected_listener: str,
) -> None:
    port = expected_listener.rsplit(":", 1)[-1]
    observed = sorted(item for item in listeners if item.endswith(f":{port}"))
    _add_check(
        checks,
        f"{name}.listener",
        observed == [expected_listener],
        [expected_listener],
        observed,
    )


def _cloudflared_checks(
    checks: list[dict[str, Any]],
    observed: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    processes = observed.get("processes", [])
    containers = observed.get("containers", [])
    quick_count = sum(_quick_tunnel(item) for item in processes) + sum(
        _quick_tunnel(item) for item in containers
    )
    if not expected.get("allow_quick_tunnels", False):
        _add_check(
            checks,
            "cloudflared.no_quick_tunnels",
            quick_count == 0,
            0,
            quick_count,
        )
    canonical_name = expected["canonical_container"]
    canonical = [item for item in containers if item.get("name") == canonical_name]
    unexpected = sorted(
        item.get("name", "")
        for item in containers
        if item.get("name") != canonical_name
    )
    canonical_ok = False
    canonical_observed: dict[str, Any] | None = canonical[0] if len(canonical) == 1 else None
    if canonical_observed is not None:
        required_networks = set(expected.get("required_networks", []))
        observed_networks = set(canonical_observed.get("networks", []))
        canonical_ok = (
            canonical_observed.get("running") is True
            and not _quick_tunnel(canonical_observed)
            and required_networks.issubset(observed_networks)
            and not unexpected
        )
    _add_check(
        checks,
        "cloudflared.canonical_container",
        canonical_ok,
        {
            "name": canonical_name,
            "required_networks": sorted(expected.get("required_networks", [])),
            "running": True,
            "unexpected_containers": [],
        },
        {
            "canonical": canonical_observed,
            "unexpected_containers": unexpected,
        },
    )


def _origin_check(checks: list[dict[str, Any]], backend: dict[str, Any]) -> None:
    parsed = urlparse(backend["canonical_origin"])
    origin_listener = f"{parsed.hostname}:{parsed.port}"
    _add_check(
        checks,
        "policy.backend_origin",
        origin_listener == backend["listener"],
        backend["listener"],
        origin_listener,
    )


def evaluate_snapshot(
    snapshot: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    collector_errors = snapshot.get("collector_errors", [])
    _add_check(
        checks,
        "collector.complete",
        collector_errors == [],
        [],
        collector_errors,
    )
    _service_checks(checks, "backend", snapshot["backend"], policy["backend"])
    _service_checks(checks, "frontend", snapshot["frontend"], policy["frontend"])
    _listener_checks(
        checks,
        snapshot.get("listeners", []),
        "backend",
        policy["backend"]["listener"],
    )
    _listener_checks(
        checks,
        snapshot.get("listeners", []),
        "frontend",
        policy["frontend"]["listener"],
    )
    _origin_check(checks, policy["backend"])
    _cloudflared_checks(checks, snapshot["cloudflared"], policy["cloudflared"])
    checkout = snapshot["checkout"]
    if policy.get("checkout", {}).get("require_clean", True):
        _add_check(
            checks,
            "checkout.clean",
            checkout.get("clean") is True,
            True,
            checkout.get("clean"),
        )
    expected_sha = checkout.get("expected_sha")
    if expected_sha:
        _add_check(
            checks,
            "checkout.expected_sha",
            checkout.get("deployed_sha") == expected_sha,
            expected_sha,
            checkout.get("deployed_sha"),
        )
    status = "PASS" if all(check["ok"] for check in checks) else "FAIL"
    return {
        "schema_version": "aftergraph.production-drift-evidence/1.0",
        "policy_version": policy["schema_version"],
        "status": status,
        "checks": checks,
        "checkout": {
            "clean": checkout.get("clean"),
            "deployed_sha": checkout.get("deployed_sha"),
            "latest_main_sha": checkout.get("latest_main_sha"),
            "expected_sha": expected_sha,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Work Intelligence production drift check")
    parser.add_argument("--repo-dir", default="/opt/work-intelligence")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    parser.add_argument("--expected-sha", default=None)
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    snapshot = collect_snapshot(args.repo_dir, args.expected_sha)
    result = evaluate_snapshot(snapshot, policy)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
