import importlib.util
import os
import platform
import shutil
from pathlib import Path


def musescore_path() -> str | None:
    configured = os.environ.get("MUSESCORE_BIN")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    for name in ("mscore", "musescore4", "musescore", "MuseScore4"):
        if found := shutil.which(name):
            return found
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for version in (4, 3):
            candidate = base / f"MuseScore {version}.app/Contents/MacOS/mscore"
            if candidate.is_file():
                return str(candidate)
    return None


def capabilities() -> dict:
    import torch

    cuda = torch.cuda.is_available()
    mps = torch.backends.mps.is_available()
    selected = "cuda" if cuda else "mps" if mps else "cpu"
    return {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "torch": torch.__version__,
        "devices": ["cpu"] + (["mps"] if mps else []) + (["cuda"] if cuda else []),
        "default_device": selected,
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "musescore": bool(musescore_path()),
        "verovio": importlib.util.find_spec("verovio") is not None,
        "mlx_installed": importlib.util.find_spec("mlx") is not None,
        "mlx_supported": False,
        "classes": {
            35: "Kick",
            38: "Snare",
            47: "Tom",
            45: "Low tom",
            50: "High tom",
            42: "Hi-hat",
            46: "Open hi-hat",
            49: "Cymbal",
        },
    }


def select_device(requested: str) -> str:
    available = capabilities()
    if requested == "auto":
        return available["default_device"]
    if requested not in available["devices"]:
        raise ValueError(f"{requested.upper()} 장치를 사용할 수 없습니다. CPU를 선택하세요.")
    return requested
