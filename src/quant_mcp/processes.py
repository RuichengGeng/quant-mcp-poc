"""Small cross-platform helpers for starting and stopping worker processes."""
from __future__ import annotations

import os
import subprocess


def _is_running(process) -> bool:
    poll = getattr(process, "poll", None)
    return poll() is None if poll is not None else process.returncode is None


def process_group_options(enabled: bool = True) -> dict:
    """Return subprocess options that isolate a worker process tree when possible."""
    if not enabled:
        return {}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process(process, *, entire_group: bool = True) -> None:
    """Stop a worker and, where requested, its descendants."""
    if os.name == "nt":
        if not _is_running(process):
            return
        if entire_group:
            # Windows has no stdlib equivalent of killpg. taskkill is included
            # with Windows and can terminate the complete child process tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if _is_running(process):
            process.kill()
        return

    if entire_group:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif _is_running(process):
        process.kill()
