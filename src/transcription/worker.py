"""Subprocess entrypoint. All child processes share the cancellable process group."""

import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path


def start_parent_watchdog():
    """Stop orphaned inference when the API process dies without running cleanup.

    Register before importing the inference pipeline. Sending SIGTERM preserves
    the Modal/Colab main-thread cancellation handlers and terminates local model
    subprocesses in this worker's dedicated process group.
    """
    parent = os.environ.get("BEATIT_PARENT_PID")
    if parent is None:
        return  # Standalone operator runs keep their existing lifetime.
    try:
        expected_parent = int(parent)
    except ValueError:
        raise RuntimeError("Invalid Beatit parent process ID") from None
    if expected_parent <= 0:
        raise RuntimeError("Invalid Beatit parent process ID")

    def watch():
        while True:
            if os.getppid() != expected_parent:
                # JobStore starts a new session. The fallback avoids signalling
                # an unrelated shell group if an operator invokes this manually.
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                else:
                    os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.2)

    threading.Thread(target=watch, daemon=True, name="inference-parent-watchdog").start()


if __name__ == "__main__":
    start_parent_watchdog()

    from .execution import run
    from .pipeline import save_json

    directory = Path(sys.argv[1]).resolve()
    try:
        run(directory)
    except Exception as error:
        traceback.print_exc()
        save_json(directory / "error.json", {"message": str(error) or type(error).__name__})
        sys.exit(1)
