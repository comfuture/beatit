"""Subprocess entrypoint. All child processes share the cancellable process group."""

import json
import os
import sys
import traceback
from pathlib import Path

from .pipeline import run, save_json

if __name__ == "__main__":
    directory = Path(sys.argv[1]).resolve()
    try:
        request = json.loads((directory / "request.json").read_text())
        remote = os.environ.get("DRUM_EXECUTION_BACKEND", "local") == "colab"
        needs_inference = not request.get("revision") and not request.get("metadata_only")
        if remote and (needs_inference or request.get("reestimate_tempo")):
            from .colab_worker import run as remote_run

            remote_run(directory)
        else:
            run(directory)
    except Exception as error:
        traceback.print_exc()
        save_json(directory / "error.json", {"message": str(error) or type(error).__name__})
        sys.exit(1)
