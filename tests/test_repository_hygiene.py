from pathlib import Path
import subprocess


def test_runtime_venv_symlink_is_ignored() -> None:
    patterns = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".venv" in patterns


def test_generated_egg_info_is_not_tracked() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "src/*.egg-info/*"],
        text=True,
    ).splitlines()

    assert tracked == []
