"""Small AAC playback derivatives; lossless processing/download artifacts stay intact."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from .object_storage import file_digest

PREVIEW_PROFILE = "aac-96k-faststart-v1"
PREVIEW_METADATA = "playback-previews.json"
PLAYBACK_PREVIEWS = {
    "audio.wav": "audio-preview.m4a",
    "drums.wav": "drums-preview.m4a",
}
PREVIEW_FILES = set(PLAYBACK_PREVIEWS.values()) | {PREVIEW_METADATA}


def preview_matches(metadata, source, original_sha256, preview_sha256):
    if not isinstance(metadata, dict) or metadata.get("profile") != PREVIEW_PROFILE:
        return False
    sources = metadata.get("sources")
    if not isinstance(sources, dict):
        return False
    entry = sources.get(source)
    if not isinstance(entry, dict) or not isinstance(original_sha256, str) or not original_sha256:
        return False
    return (
        entry.get("original_sha256") == original_sha256
        and entry.get("preview_sha256") == preview_sha256
    )


def prepare_playback_previews(directory: Path):
    """Best effort and idempotent by original/output SHA; never modify originals."""
    sidecar = directory / PREVIEW_METADATA
    try:
        metadata = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or metadata.get("profile") != PREVIEW_PROFILE:
        metadata = {"profile": PREVIEW_PROFILE, "sources": {}}
    if not isinstance(metadata.get("sources"), dict):
        metadata["sources"] = {}
    report = {"created": [], "reused": [], "failed": []}
    for source, preview in PLAYBACK_PREVIEWS.items():
        original = directory / source
        if not original.is_file():
            continue
        destination = directory / preview
        temporary = directory / ("." + uuid4().hex + ".m4a")
        try:
            original_sha256 = file_digest(original)
            if destination.is_file() and preview_matches(
                metadata, source, original_sha256, file_digest(destination)
            ):
                report["reused"].append(preview)
                continue
            metadata["sources"].pop(source, None)
            # Fake/failed inference fixtures are not playable WAVs. Reject them
            # cheaply, preserving normal score publication with raw fallback.
            with original.open("rb") as stream:
                header = stream.read(12)
            if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
                raise ValueError("Source is not a WAV recording")
            executable = shutil.which("ffmpeg")
            if not executable:
                raise RuntimeError("FFmpeg is unavailable")
            result = subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(original),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    "-threads",
                    "1",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                capture_output=True,
                timeout=180,
            )
            if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("AAC encoding failed")
            temporary.replace(destination)
            metadata["sources"][source] = {
                "original_sha256": original_sha256,
                "preview_sha256": file_digest(destination),
                "bytes": destination.stat().st_size,
            }
            report["created"].append(preview)
        except Exception as error:
            metadata["sources"].pop(source, None)
            report["failed"].append(source)
            logging.warning(
                "Playback preview unavailable for %s (%s); using original audio",
                source,
                type(error).__name__,
            )
        finally:
            temporary.unlink(missing_ok=True)
    if any(report.values()) or metadata["sources"]:
        temporary = sidecar.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(metadata, ensure_ascii=False))
            temporary.replace(sidecar)
        except OSError:
            logging.warning("Playback preview metadata could not be saved; using original audio")
        finally:
            temporary.unlink(missing_ok=True)
    return report
