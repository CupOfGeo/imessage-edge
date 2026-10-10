"""The FDA launcher must hand its child only what was baked in at build
time: no caller arguments, no caller environment."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER_SRC = Path(__file__).parent.parent / "launcher" / "imessage-edged.c"


@pytest.mark.skipif(shutil.which("cc") is None, reason="needs a C compiler")
def test_launcher_ignores_caller_args_and_env(tmp_path: Path) -> None:
    # Stands in for the venv python: reports what it was given.
    child = tmp_path / "fake-python"
    child.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
        "sys.exit(7)\n"
    )
    child.chmod(0o755)
    launcher = tmp_path / "imessage-edged"
    subprocess.run(
        [
            "cc",
            "-o",
            str(launcher),
            f'-DEDGE_PYTHON="{child}"',
            '-DEDGE_CONFIG="/baked/config.env"',
            str(LAUNCHER_SRC),
        ],
        check=True,
    )

    r = subprocess.run(
        [str(launcher), "-c", "print('pwned')"],
        env={
            "HOME": "/attacker/home",
            "PYTHONPATH": "/attacker",
            "IMESSAGE_EDGE_CONFIG": "/attacker/config.env",
        },
        capture_output=True,
        text=True,
    )

    assert r.returncode == 7  # child's exit status propagates
    seen = json.loads(r.stdout)
    assert seen["argv"] == ["-I", "-m", "imessage_edge"]
    env = seen["env"]
    assert env["IMESSAGE_EDGE_CONFIG"] == "/baked/config.env"
    assert env["HOME"] == pwd.getpwuid(os.getuid()).pw_dir
    assert "PYTHONPATH" not in env
