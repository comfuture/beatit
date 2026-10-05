"""Subprocess entrypoint. All child processes share the cancellable process group."""

import sys
import traceback
from pathlib import Path

from .pipeline import run, save_json

if __name__ == "__main__":
    directory = Path(sys.argv[1]).resolve()
    try:
        run(directory)
    except Exception as error:
        traceback.print_exc()
        save_json(directory / "error.json", {"message": str(error) or type(error).__name__})
        sys.exit(1)
