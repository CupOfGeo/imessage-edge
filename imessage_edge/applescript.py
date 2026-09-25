"""AppleScript helper.

Generic wrapper around macOS ``osascript``. Kept self-contained so the
daemon has no dependencies beyond the standard library here.
"""

from __future__ import annotations

import subprocess

from imessage_edge.logging import log


def escape_applescript_string(value: str) -> str:
    """Escape a Python string for safe inclusion in an AppleScript literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def run_applescript(script: str, timeout: int = 60) -> str:
    """Run an AppleScript via ``osascript`` and return stripped stdout.

    Raises ``RuntimeError`` if osascript exits non-zero, times out, or
    is not installed (non-macOS).
    """
    log.debug("applescript.run", timeout=timeout)
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "osascript not found — AppleScript is only available on macOS"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"AppleScript timed out after {timeout}s") from exc

    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()
