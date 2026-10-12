import json
import zipfile
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from transcription import pipeline
from transcription.app import create_app
from transcription.models import Options, ScoreMetadata
from transcription.score import quantize, update_musicxml_metadata, write_musicxml


def saved_score(directory):
    generation = directory / "render-original"
    generation.mkdir(parents=True)
    options = Options(bpm=120, offset=0, renderer="verovio")
    events = [{"time": 0, "pitch": 35, "strength": 0.8}]
    grid = quantize(events, 4, options)
    write_musicxml(grid, "Original", generation / "score.musicxml")
    for name in ("score.mid", "performance.mid", "events.json", "tom-refinement.json"):
        (generation / name).write_bytes(b"preserve " + name.encode())
    result = {
        "generation": generation.name,
        "options": options.model_dump(),
        "grid": grid,
        "pages": ["score-1.svg"],
        "warnings": ["Review tempo", "Old renderer warning"],
        "render_warnings": ["Old renderer warning"],
        "duration": 4,
        "event_count": 1,
    }
    pipeline.save_json(directory / "result.json", result)
    return result


@pytest.mark.parametrize("author", ["김드럼 & Friends <편곡>", ""])
def test_metadata_changes_only_heading_and_escapes_xml(tmp_path, author):
    saved_score(tmp_path)
    source = tmp_path / "render-original" / "score.musicxml"
    target = tmp_path / "updated.musicxml"
    metadata = ScoreMetadata(title='새 악보 <Live> & "연습"', author=author)
    update_musicxml_metadata(source, target, metadata)
    before, after = ET.parse(source).getroot(), ET.parse(target).getroot()
    assert after.findtext("work/work-title") == metadata.title
    assert (after.findtext("identification/creator") or "") == author
    assert ET.tostring(before.find("part")) == ET.tostring(after.find("part"))
    assert before.findtext("work/work-title") == "Original"


def test_metadata_regeneration_preserves_rhythm_and_publishes_bundle(tmp_path, monkeypatch):
    original = saved_score(tmp_path)
    metadata = {"title": "새 악보 & Live", "author": "홍길동"}
    pipeline.save_json(
        tmp_path / "request.json",
        {
            "metadata_only": True,
            "source_generation": original["generation"],
            "metadata": metadata,
        },
    )

    def render(xml, directory, renderer):
        root = ET.parse(xml).getroot()
        assert root.findtext("work/work-title") == metadata["title"]
        assert root.findtext("identification/creator") == metadata["author"]
        (directory / "score-1.svg").write_text("updated SVG")
        (directory / "score.pdf").write_bytes(b"updated PDF")
        return {"pages": ["score-1.svg"], "pdf": True, "renderer": renderer, "warnings": []}

    monkeypatch.setattr(pipeline, "render", render)
    # No source audio, analysis, or root events exist: only the saved score is needed.
    pipeline.run(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    generation = tmp_path / result["generation"]
    assert result["metadata"] == metadata
    assert result["grid"] == original["grid"]
    assert result["options"] == original["options"]
    assert result["metadata_source_generation"] == original["generation"]
    assert result["warnings"] == ["Review tempo"]
    with zipfile.ZipFile(generation / "score-bundle.zip") as bundle:
        assert json.loads(bundle.read("analysis.json"))["metadata"] == metadata
        for name in ("score.mid", "performance.mid", "events.json", "tom-refinement.json"):
            assert bundle.read(name) == (tmp_path / original["generation"] / name).read_bytes()
        assert bundle.read("score.pdf") == b"updated PDF"
        assert bundle.read("score-1.svg") == b"updated SVG"


def test_failed_metadata_render_keeps_published_result_then_can_retry(tmp_path, monkeypatch):
    saved_score(tmp_path)
    before = (tmp_path / "result.json").read_bytes()
    pipeline.save_json(
        tmp_path / "request.json",
        {
            "metadata_only": True,
            "source_generation": "render-original",
            "metadata": {"title": "New", "author": ""},
        },
    )

    def fail(*args):
        raise RuntimeError("engraving failed")

    monkeypatch.setattr(pipeline, "render", fail)
    with pytest.raises(RuntimeError, match="engraving failed"):
        pipeline.run(tmp_path)
    assert (tmp_path / "result.json").read_bytes() == before
    from transcription.engrave import render

    monkeypatch.setattr(pipeline, "render", render)
    pipeline.run(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["metadata"] == {"title": "New", "author": ""}
    svg = (tmp_path / result["generation"] / result["pages"][0]).read_text()
    assert "New" in svg and "Automatic drum transcription" not in svg


def test_metadata_api_validates_queues_and_retains_saved_heading_on_rhythm_edits(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        job = client.post("/api/jobs", files={"file": ("session.wav", b"audio")}).json()
        identifier = job["id"]
        url = f"/api/jobs/{identifier}/score-metadata"
        metadata = {"title": "  New title  ", "author": "Author"}
        assert client.post(url, json=metadata).status_code == 409
        saved = saved_score(tmp_path / identifier)
        app.state.store.update(identifier, "completed")
        for invalid in (
            {"title": "x" * 257},
            {"title": "a\x00b"},
            {"title": "a\nb"},
            {"title": "x", "author": "x" * 257},
        ):
            assert client.post(url, json=invalid).status_code == 422
        response = client.post(url, json=metadata)
        assert response.status_code == 202
        request = response.json()["request"]
        assert request["metadata_only"] is True
        assert request["source_generation"] == saved["generation"]
        assert request["metadata"] == {"title": "New title", "author": "Author"}
        assert response.json()["filename"] == "session.wav"
        assert response.json()["result"] == saved
        assert client.post(url, json=metadata).status_code == 409
        app.state.store.update(identifier, "failed")
        assert client.post(f"/api/jobs/{identifier}/retry").json()["request"] == request
        saved["metadata"] = request["metadata"]
        pipeline.save_json(tmp_path / identifier / "result.json", saved)
        app.state.store.update(identifier, "completed")
        revised = client.post(f"/api/jobs/{identifier}/score", json={"options": {"bpm": 126}})
        assert revised.status_code == 202
        assert revised.json()["request"]["metadata"] == saved["metadata"]
        assert "metadata_only" not in revised.json()["request"]


def test_new_score_uses_metadata_instead_of_filename(tmp_path):
    result = pipeline.make_score(
        tmp_path,
        {
            "filename": "session.wav",
            "options": Options(bpm=120, offset=0, renderer="verovio").model_dump(),
            "metadata": {"title": "My score", "author": "My author"},
        },
        {"duration": 2, "estimated_bpm": 120, "tempo_reliable": True},
        [{"time": 0, "pitch": 35, "strength": 0.8}],
        pipeline.Progress(tmp_path),
    )
    assert result["metadata"] == {"title": "My score", "author": "My author"}
    svg = (tmp_path / result["generation"] / result["pages"][0]).read_text()
    assert "My score" in svg and "My author" in svg
