"""Grid-only revisions retain published/draft hits and regenerate consistent exports."""

import json
import math
import zipfile
from xml.etree import ElementTree as ET

import pretty_midi
import pytest
from fastapi.testclient import TestClient

from transcription import pipeline, tom_refinement
from transcription.models import Event, Options
from transcription.score import quantize, write_musicxml
from transcription.store import JobStore

EVENTS = [
    {"time": 0.01, "pitch": 45, "strength": 0.5, "velocity": 39},
    {
        "time": 0.81,
        "pitch": 50,
        "strength": 0.8,
        "velocity": 101,
        "tom_source_pitch": 47,
        "tom_match_score": 0.97,
    },
    {"time": 1.81, "pitch": 47, "strength": 0.7, "velocity": 89},
    {"time": 3.11, "pitch": 38, "strength": 0.6, "velocity": 56},
    {"time": 3.99, "pitch": 35, "strength": 0.9, "velocity": 119},
]
METADATA = {"title": "Manual score <&>", "author": "Drummer"}
TOM_REPORT = {
    "enabled": True,
    "status": "completed",
    "low_count": 0,
    "high_count": 1,
    "unresolved_count": 1,
    "hits": [{"time": 0.81, "label": "high", "match_score": 0.97}],
}


def saved_score(directory, bpm=120, meter="4/4", grid="16"):
    generation = directory / "render-published"
    generation.mkdir(parents=True)
    options = Options(bpm=bpm, offset=0, meter=meter, grid=grid, renderer="verovio")
    score_grid = quantize(EVENTS, 4, options)
    write_musicxml(score_grid, METADATA["title"], generation / "score.musicxml", METADATA["author"])
    pipeline.write_midi(score_grid["events"], generation / "score.mid", bpm, meter)
    pipeline.write_midi(EVENTS, generation / "performance.mid", bpm, meter)
    pipeline.save_json(generation / "events.json", EVENTS)
    pipeline.save_json(generation / "tom-refinement.json", TOM_REPORT)
    analysis = {
        "duration": 4,
        "estimated_bpm": 99,
        "estimated_offset": 0.19,
        "tempo_reliable": True,
        "warnings": [],
    }
    review = {
        "grid_key": json.dumps([bpm, 0, meter, score_grid["bar_ticks"], score_grid["step"]]),
        "beats": [{"tick": 0, "reasons": ["off-grid"], "resolved": True}],
    }
    saved = {
        **analysis,
        "generation": generation.name,
        "options": options.model_dump(),
        "grid": score_grid,
        "metadata": METADATA,
        "pages": [],
        "pdf": False,
        "renderer": "verovio",
        "render_warnings": [],
        "event_count": len(EVENTS),
        "tom_refinement": {key: value for key, value in TOM_REPORT.items() if key != "hits"},
        "beat_review": review,
    }
    pipeline.save_json(directory / "result.json", saved)
    pipeline.save_json(directory / "analysis.json", analysis)
    # The raw inference cache differs from the displayed, published tom labels.
    raw = [{**event, "pitch": 47} for event in EVENTS]
    pipeline.save_json(directory / "events.json", raw)
    return saved


def forbidden_models(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A grid-only revision must preserve events without model/audio work")

    for name in ("convert_media", "separate", "transcribe", "estimate_tempo", "select_device"):
        monkeypatch.setattr(pipeline, name, forbidden)
    for name in ("restore_inferred_toms", "refine_toms"):
        monkeypatch.setattr(tom_refinement, name, forbidden)


def shift_request(saved, offset, **changes):
    return {
        "filename": "source.wav",
        "revision": True,
        "preserve_events": True,
        "source_generation": saved["generation"],
        "options": {**saved["options"], "offset": offset},
        "metadata": METADATA,
        "beat_review": saved["beat_review"],
        **changes,
    }


def xml_hits(path, bar_ticks):
    hits = []
    for index, measure in enumerate(ET.parse(path).getroot().findall("part/measure")):
        cursor = index * bar_ticks
        previous = cursor
        for item in measure:
            if item.tag == "backup":
                cursor -= int(item.findtext("duration"))
            if item.tag != "note":
                continue
            chord = item.find("chord") is not None
            tick = previous if chord else cursor
            if item.find("unpitched") is not None:
                hits.append((tick, int(item.find("instrument").get("id").split("I")[1])))
            if not chord:
                previous = cursor
                cursor += int(item.findtext("duration"))
    return sorted(hits)


@pytest.mark.parametrize(
    "bpm,meter,grid,shift",
    [
        (60, "4/4", "16", 0.5),
        (120, "3/4", "8", -0.5),
        (137.3, "6/8", "16", 1),
        (120, "7/8", "8", -1),
        (60, "6/8", "triplet", 0.5),
        (137.3, "4/4", "triplet", -1),
    ],
)
def test_grid_revision_preserves_hits_and_exports(tmp_path, monkeypatch, bpm, meter, grid, shift):
    saved = saved_score(tmp_path, bpm, meter, grid)
    numerator, denominator = map(int, meter.split("/"))
    bar_seconds = numerator * 4 / denominator * 60 / bpm
    offset = shift * 60 / bpm
    if offset > EVENTS[0]["time"]:
        offset -= math.ceil((offset - EVENTS[0]["time"]) / bar_seconds) * bar_seconds
    request = shift_request(saved, offset, events=EVENTS, reestimate_tempo=True)
    pipeline.save_json(tmp_path / "request.json", request)
    (tmp_path / "input").write_bytes(b"unchanged source media")
    forbidden_models(monkeypatch)
    pipeline.run(tmp_path)

    result = json.loads((tmp_path / "result.json").read_text())
    rendered = tmp_path / result["generation"]
    assert result["grid"]["offset"] == round(offset, 4)
    assert result["grid"]["bpm"] == bpm
    assert result["grid"]["dropped_before_offset"] == 0
    assert len(result["grid"]["events"]) == len(EVENTS)
    assert result["metadata"] == METADATA
    assert result["tom_refinement"] == saved["tom_refinement"]
    assert json.loads((rendered / "tom-refinement.json").read_text()) == TOM_REPORT
    assert json.loads((rendered / "events.json").read_text()) == EVENTS
    assert json.loads((tmp_path / "events.json").read_text()) == EVENTS
    assert (tmp_path / "input").read_bytes() == b"unchanged source media"
    assert result["beat_review"]["beats"] == []
    assert json.loads(result["beat_review"]["grid_key"])[:2] == [bpm, round(offset, 4)]
    assert xml_hits(rendered / "score.musicxml", result["grid"]["bar_ticks"]) == sorted(
        (event["tick"], event["pitch"]) for event in result["grid"]["events"]
    )
    root = ET.parse(rendered / "score.musicxml").getroot()
    assert root.findtext("work/work-title") == METADATA["title"]
    score_notes = pretty_midi.PrettyMIDI(str(rendered / "score.mid")).instruments[0].notes
    expected = sorted(
        result["grid"]["events"], key=lambda event: (event["quantized_time"], event["pitch"])
    )
    assert [(note.pitch, note.velocity) for note in score_notes] == [
        (event["pitch"], event["velocity"]) for event in expected
    ]
    assert [note.start for note in score_notes] == pytest.approx(
        [max(0, event["quantized_time"]) for event in expected], abs=0.003
    )
    performance = pretty_midi.PrettyMIDI(str(rendered / "performance.mid")).instruments[0].notes
    assert [note.start for note in performance] == pytest.approx(
        [event["time"] for event in EVENTS], abs=0.003
    )
    with zipfile.ZipFile(rendered / "score-bundle.zip") as bundle:
        assert json.loads(bundle.read("events.json")) == EVENTS
        assert json.loads(bundle.read("analysis.json"))["grid"] == result["grid"]
        assert bundle.read("score.musicxml") == (rendered / "score.musicxml").read_bytes()
        assert bundle.read("score.mid") == (rendered / "score.mid").read_bytes()


def test_omitted_events_use_published_generation_and_resolved_grid(tmp_path, monkeypatch):
    saved = saved_score(tmp_path, bpm=137.3)
    options = {**saved["options"], "bpm": None, "offset": None}
    pipeline.save_json(tmp_path / "request.json", shift_request(saved, 0, options=options))
    forbidden_models(monkeypatch)
    pipeline.run(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["grid"]["bpm"] == 137.3
    assert result["grid"]["offset"] == 0
    assert json.loads((tmp_path / result["generation"] / "events.json").read_text()) == EVENTS
    assert result["beat_review"] == saved["beat_review"]


def test_failed_or_stale_grid_revision_keeps_published_score(tmp_path, monkeypatch):
    saved = saved_score(tmp_path)
    before = (tmp_path / "result.json").read_bytes()
    before_events = (tmp_path / "events.json").read_bytes()
    request = shift_request(saved, -0.25, source_generation="render-stale", events=EVENTS)
    pipeline.save_json(tmp_path / "request.json", request)
    with pytest.raises(ValueError, match="악보가 변경"):
        pipeline.run(tmp_path)
    request["source_generation"] = saved["generation"]
    pipeline.save_json(tmp_path / "request.json", request)

    def fail(*args):
        raise RuntimeError("engraver unavailable")

    forbidden_models(monkeypatch)
    monkeypatch.setattr(pipeline, "render", fail)
    with pytest.raises(RuntimeError, match="engraver unavailable"):
        pipeline.run(tmp_path)
    assert (tmp_path / "result.json").read_bytes() == before
    assert (tmp_path / "events.json").read_bytes() == before_events
    from transcription.engrave import render

    monkeypatch.setattr(pipeline, "render", render)
    pipeline.run(tmp_path)
    assert json.loads((tmp_path / "result.json").read_text())["grid"]["offset"] == -0.25


@pytest.mark.mysql
def test_grid_revision_api_fences_generation_and_owner(
    tmp_path, authenticated_app, authenticated_client
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = client.post("/api/jobs", files={"file": ("source.wav", b"source")}).json()[
            "id"
        ]
        saved = saved_score(tmp_path / identifier)
        app.state.store.update(identifier, "completed")
        url = f"/api/jobs/{identifier}/score"
        body = shift_request(saved, -0.25, client_edit_id="own-grid-edit-0123456789")
        before = app.state.store.get(identifier)
        for invalid_id in ("", "x" * 65):
            assert client.post(url, json={**body, "client_edit_id": invalid_id}).status_code == 422
        assert app.state.store.get(identifier) == before
        body["source_generation"] = "render-stale"
        response = client.post(url, json=body)
        assert response.status_code == 409
        assert "악보가 변경" in response.json()["detail"]
        assert app.state.store.get(identifier) == before

        token, other = app.state.auth.create_session(
            {"sub": "other", "email": "other@example.test", "name": "Other"}
        )
        with TestClient(app, cookies={app.state.auth.settings.cookie_name: token}) as foreign:
            assert foreign.post(url, json=body).status_code == 404
        assert app.state.store.get(identifier) == before
        with pytest.raises(KeyError):
            app.state.store.revise(
                identifier, body["options"], owner_id=other["id"], source_generation="render-stale"
            )
        body.pop("source_generation")
        response = client.post(url, json=body)
        assert response.status_code == 202
        queued = response.json()["request"]
        assert queued["preserve_events"] is True
        assert queued["source_generation"] == saved["generation"]
        assert queued["client_edit_id"] == body["client_edit_id"]
        assert (
            client.get(f"/api/jobs/{identifier}").json()["request"]["client_edit_id"]
            == body["client_edit_id"]
        )
        assert response.json()["result"] == saved


@pytest.mark.mysql
def test_grid_revision_api_publishes_draft_edits_and_recovers_exports(
    tmp_path, monkeypatch, authenticated_app, authenticated_client
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = client.post("/api/jobs", files={"file": ("source.wav", b"source")}).json()[
            "id"
        ]
        store = app.state.store
        saved = saved_score(tmp_path / identifier)
        store.update(identifier, "completed")
        source_manifest = store.artifact_manifest(identifier, "input")
        draft = [Event.model_validate(event).model_dump() for event in EVENTS]
        draft[2].update(pitch=50, velocity=75)
        body = shift_request(saved, -0.25, events=draft)
        body["metadata"] = {"title": "Draft heading", "author": "Editor"}
        response = client.post(f"/api/jobs/{identifier}/score", json=body)
        assert response.status_code == 202
        assert response.json()["request"]["events"] == draft
        assert "client_edit_id" not in response.json()["request"]
        claim = store.claim()
        directory = store.restore(identifier, tmp_path / "attempt")
        forbidden_models(monkeypatch)
        pipeline.run(directory)
        assert store.finish_claim(identifier, claim["claim_token"], directory)
        published = client.get(f"/api/jobs/{identifier}").json()["result"]
        assert published["metadata"] == body["metadata"]
        assert published["grid"]["offset"] == -0.25
        assert published["beat_review"]["beats"] == []
        assert store.artifact_manifest(identifier, "input") == source_manifest
        recovered = JobStore(tmp_path / "empty-cache", db=store.db, objects=store.objects)
        assert recovered.get(identifier)["result"] == published
        events = recovered.file_path(identifier, "events.json")
        assert json.loads(events.read_text()) == draft
        bundle = recovered.file_path(identifier, f"{published['generation']}/score-bundle.zip")
        with zipfile.ZipFile(bundle) as archive:
            assert json.loads(archive.read("events.json")) == draft
            assert json.loads(archive.read("analysis.json"))["grid"] == published["grid"]
        stale = client.post(f"/api/jobs/{identifier}/score", json=body)
        assert stale.status_code == 409
