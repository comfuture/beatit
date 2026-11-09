"""Prepare immutable, versioned weights on a CPU builder or model Volume."""

import json
from pathlib import Path

from .modal_config import CACHE_ENV, MANIFEST, MODEL_DIR, MODEL_FINGERPRINT


def model_manifest():
    path = Path(MODEL_DIR) / MANIFEST
    try:
        value = json.loads(path.read_text())
        if value.get("fingerprint") != MODEL_FINGERPRINT or not value.get("files"):
            return None
        root = Path(CACHE_ENV["XDG_CACHE_HOME"])
        for file in value["files"]:
            cached = root / file["path"]
            if not cached.is_file() or cached.stat().st_size != file["bytes"]:
                return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare_models():
    if manifest := model_manifest():
        return manifest
    from demucs.pretrained import get_model

    from .detection import load_adtof
    from .drumsep import checkpoint_path
    from .rhythm import beat_this_checkpoint

    # ADTOF weights ship in its pinned wheel; verify them along with downloaded models.
    model = get_model("htdemucs")
    del model
    checkpoint_path()
    beat_this_checkpoint()
    model = load_adtof()
    del model
    root = Path(CACHE_ENV["XDG_CACHE_HOME"])
    path = Path(MODEL_DIR) / MANIFEST
    files = [p for p in root.rglob("*") if p.is_file() and p != path and p.suffix != ".tmp"]
    manifest = {
        "fingerprint": MODEL_FINGERPRINT,
        "bytes": sum(p.stat().st_size for p in files if not p.is_symlink()),
        "models": ["htdemucs", "drumsep", "beat-this", "adtof"],
        "files": [
            {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size} for p in files
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest))
    temporary.replace(path)
    return manifest


if __name__ == "__main__":
    print(json.dumps(prepare_models()))
