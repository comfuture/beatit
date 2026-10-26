"""Bounded, allowlisted transfer of inference files across the Colab boundary."""

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MEDIA = {"audio.wav", "drums.wav"} | {
    f"stem-{name}.flac" for name in ("kick", "snare", "toms", "hh", "cymbals")
}
ROOT_OUTPUTS = MEDIA | {"analysis.json", "events.json", "result.json"}
SCORE_FILES = {
    "score.musicxml",
    "score.mid",
    "performance.mid",
    "score.pdf",
    "score-bundle.zip",
    "analysis.json",
    "events.json",
    "engraver.log",
    "tom-refinement.json",
}
MAX_RESULT_BYTES = 4 * 1024**3


def valid_identifier(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{32}", value) is not None


def save_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def pack_input(directory: Path, destination: Path):
    request = json.loads((directory / "request.json").read_text())
    names = {"request.json", "input"}
    if request.get("revision"):
        names |= MEDIA | {"analysis.json", "events.json", "result.json"}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                archive.write(path, name)


def unpack_input(archive_path: Path, directory: Path):
    allowed = ROOT_OUTPUTS | {"request.json", "input"}
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > len(allowed) or sum(i.file_size for i in infos) > MAX_RESULT_BYTES:
            raise ValueError("Input archive exceeds its limits")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names) or not {"request.json", "input"} <= set(names):
            raise ValueError("Input archive is incomplete or has duplicate files")
        for info in infos:
            if info.filename not in allowed or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Unexpected input archive entry")
            with archive.open(info) as source, (directory / info.filename).open("wb") as target:
                shutil.copyfileobj(source, target)


def pack_output(directory: Path, destination: Path):
    result = json.loads((directory / "result.json").read_text())
    generation = result["generation"]
    if not re.fullmatch(r"render-[0-9]+", generation):
        raise ValueError("Invalid render generation")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(ROOT_OUTPUTS):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                archive.write(path, name)
        for path in sorted((directory / generation).iterdir()):
            if path.is_file() and not path.is_symlink():
                archive.write(path, generation + "/" + path.name)


def import_output(archive_path: Path, directory: Path, provenance: dict):
    """Validate everything before replacing media; publish the result pointer last."""
    if not directory.is_dir():
        raise FileNotFoundError("The job was deleted")
    with tempfile.TemporaryDirectory(prefix=".colab-import-", dir=directory) as scratch:
        staged = Path(scratch)
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > 1000 or len(names) != len(set(names)):
                raise ValueError("Unexpected number of result files")
            if sum(info.file_size for info in infos) > MAX_RESULT_BYTES:
                raise ValueError("Result archive exceeds its size limit")
            if not {"result.json", "events.json", "analysis.json", *MEDIA} <= set(names):
                raise ValueError("Incomplete Colab result")
            if archive.getinfo("result.json").file_size > 32 * 1024**2:
                raise ValueError("Result metadata exceeds its size limit")
            result = json.loads(archive.read("result.json"))
            generation = result.get("generation", "")
            pages = result.get("pages", [])
            if not re.fullmatch(r"render-[0-9]+", generation) or not pages:
                raise ValueError("Invalid result generation")
            if any(not re.fullmatch(r"score-[0-9]+\.svg", p) for p in pages):
                raise ValueError("Invalid score page name")
            allowed = ROOT_OUTPUTS | {generation + "/" + name for name in SCORE_FILES | set(pages)}
            required = {generation + "/" + name for name in ["score.musicxml", "score.mid", *pages]}
            if not required <= set(names):
                raise ValueError("Missing score artifacts")
            for info in infos:
                if info.filename not in allowed or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Unexpected result archive entry")
                if (
                    info.filename.endswith((".json", ".musicxml", ".svg", ".mid"))
                    and info.file_size > 32 * 1024**2
                ):
                    raise ValueError("Score metadata exceeds its size limit")
                target = staged / info.filename
                target.parent.mkdir(exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            for name in ["score.musicxml", *pages]:
                root = ElementTree.parse(staged / generation / name).getroot()
                if not root.tag.endswith("svg" if name.endswith(".svg") else "score-partwise"):
                    raise ValueError("Invalid score XML")
            for name in ("score.mid", "performance.mid"):
                if (staged / generation / name).read_bytes()[:4] != b"MThd":
                    raise ValueError("Invalid MIDI artifact")
            with zipfile.ZipFile(staged / generation / "score-bundle.zip") as bundle:
                if (
                    len(bundle.infolist()) > 1000
                    or sum(i.file_size for i in bundle.infolist()) > 256 * 1024**2
                ):
                    raise ValueError("Score bundle exceeds its size limit")
                if any(n not in SCORE_FILES | set(pages) for n in bundle.namelist()):
                    raise ValueError("Unexpected score bundle entry")
                if bundle.testzip():
                    raise ValueError("Corrupt score bundle")
            analysis = json.loads((staged / "analysis.json").read_text())
            if analysis.get("transcription_device") != "cuda":
                raise ValueError("Colab did not use CUDA")
        result["execution_backend"] = "colab"
        result["execution_runtime"] = provenance
        analysis.update(execution_backend="colab", execution_runtime=provenance)
        save_json(staged / "analysis.json", analysis)
        save_json(staged / "result.json", result)
        if (directory / generation).exists():
            raise ValueError("Render generation already exists")
        os.replace(staged / generation, directory / generation)
        for name in sorted(ROOT_OUTPUTS - {"result.json"}):
            if (staged / name).exists():
                os.replace(staged / name, directory / name)
        os.replace(staged / "result.json", directory / "result.json")
    return result
