"""Build a reproducible, credential-free inference source bundle without Git access."""

import argparse
import tarfile
from pathlib import Path


def build(root: Path, destination: Path):
    paths = [root / name for name in ("pyproject.toml", "uv.lock", "README.md", ".python-version")]
    paths += sorted((root / "src/transcription").rglob("*.py"))
    with tarfile.open(destination, "w") as archive:
        for path in paths:
            if path.is_symlink() or "__pycache__" in path.parts:
                continue
            name = path.relative_to(root).as_posix()
            info = archive.gettarinfo(str(path), name)
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as content:
                archive.addfile(info, content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.root, args.output)
