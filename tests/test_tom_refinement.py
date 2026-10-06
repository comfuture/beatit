import copy
import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from transcription.models import Options
from transcription.tom_refinement import refine_toms, restore_inferred_toms


def recording(tmp_path, frequencies, *, kick_hits=(), extra_events=()):
    rate = 8000
    audio = np.zeros(round((len(frequencies) + 1) * rate), dtype=np.float32)
    kick = np.zeros_like(audio)
    events = []
    for i, frequency in enumerate(frequencies):
        onset = i + 0.1
        time = np.arange(round(rate * 0.4)) / rate
        wave = 0.5 * np.sin(2 * np.pi * frequency * time) * np.exp(-time / 0.12)
        start = round(onset * rate)
        audio[start : start + len(wave)] += wave
        events.append({"time": onset, "pitch": 47, "strength": 0.8, "velocity": 88})
        if i in kick_hits:
            kick[start : start + len(wave)] += wave
            events.append({"time": onset, "pitch": 35, "strength": 0.9})
    events.extend(extra_events)
    tom_path, kick_path = tmp_path / "stem-toms.flac", tmp_path / "stem-kick.flac"
    sf.write(tom_path, audio, rate)
    sf.write(kick_path, kick, rate)
    return events, tom_path, kick_path


def test_two_resonance_groups_preserve_onsets_velocity_and_manual_edits(tmp_path):
    events, toms, kick = recording(
        tmp_path,
        [120] * 5 + [240] * 5,
        extra_events=[{"time": 11, "pitch": 45, "strength": 0.7}],
    )
    original = copy.deepcopy(events)
    refined, report = refine_toms(events, toms, kick)
    assert report["status"] == "completed"
    assert (report["low_count"], report["high_count"], report["unresolved_count"]) == (5, 5, 0)
    assert [e["pitch"] for e in refined] == [45] * 5 + [50] * 5 + [45]
    assert [(e["time"], e["strength"], e.get("velocity")) for e in refined] == [
        (e["time"], e["strength"], e.get("velocity")) for e in events
    ]
    assert events == original
    assert refined[-1] == events[-1]
    assert restore_inferred_toms(refined) == original
    assert refine_toms(refined, toms, kick) == (refined, report)


def test_one_resonance_group_is_not_forced_into_two_toms(tmp_path):
    events, toms, kick = recording(tmp_path, [120] * 10)
    refined, report = refine_toms(events, toms, kick)
    assert refined == events
    assert report["status"] == "insufficient_evidence"
    assert report["unresolved_count"] == 10


def test_kick_bleed_and_short_tails_remain_generic_toms(tmp_path):
    events, toms, kick = recording(tmp_path, [120] * 6 + [240] * 5, kick_hits=(0,))
    # This near-following hit truncates the preceding hit's resonance windows.
    events.append({"time": 1.14, "pitch": 47, "strength": 0.4})
    refined, report = refine_toms(events, toms, kick)
    assert report["status"] == "completed"
    assert refined[0]["pitch"] == 47
    assert report["hits"][0]["reason"] == "kick_bleed"
    truncated = next(h for h in report["hits"] if h["time"] == 1.1)
    assert truncated["reason"] == "short_or_silent_tail"
    assert refined[1] == events[1]  # kick event is unchanged
    assert len(refined) == len(events)


def test_missing_stems_keeps_events_and_reports_reason(tmp_path):
    events = [{"time": 0.1, "pitch": 47, "strength": 0.8}]
    refined, report = refine_toms(events, tmp_path / "missing", tmp_path / "missing-kick")
    assert refined == events
    assert report["status"] == "missing_stems"
    assert report["unresolved_count"] == 1


def test_manual_tom_truncates_preceding_tail_and_overlapping_toms_are_not_split(tmp_path):
    events, toms, kick = recording(
        tmp_path,
        [120] * 6 + [240] * 5,
        extra_events=[
            {"time": 0.18, "pitch": 50, "strength": 0.7},
            {"time": 1.1, "pitch": 45, "strength": 0.7},
        ],
    )
    refined, report = refine_toms(events, toms, kick)
    assert report["status"] == "completed"
    assert [e["pitch"] for e in refined[:2]] == [47, 47]
    assert [h["reason"] for h in report["hits"][:2]] == [
        "short_or_silent_tail",
        "overlapping_toms",
    ]
    assert refined[-2:] == events[-2:]


def test_cached_revision_can_enable_and_disable_refinement_without_inference(tmp_path, monkeypatch):
    from transcription import pipeline

    events, _, _ = recording(tmp_path, [120] * 5 + [240] * 5)
    analysis = {
        "duration": 11,
        "estimated_bpm": 120,
        "estimated_offset": 0,
        "tempo_reliable": True,
        "warnings": [],
    }
    (tmp_path / "events.json").write_text(json.dumps(events))
    (tmp_path / "analysis.json").write_text(json.dumps(analysis))
    request = {
        "revision": True,
        "filename": "Synthetic.wav",
        "options": Options(tom_refinement=True, renderer="verovio").model_dump(),
    }
    (tmp_path / "request.json").write_text(json.dumps(request))

    def forbidden(*args, **kwargs):
        raise AssertionError("A cached score revision must not rerun inference")

    for name in ("convert_media", "separate", "transcribe", "estimate_tempo"):
        monkeypatch.setattr(pipeline, name, forbidden)
    pipeline.run(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    generation = tmp_path / result["generation"]
    inferred = json.loads((generation / "events.json").read_text())
    assert [e["pitch"] for e in inferred] == [45] * 5 + [50] * 5
    assert json.loads((tmp_path / "events.json").read_text()) == events
    assert (generation / "tom-refinement.json").is_file()

    # Submitting the displayed events and disabling refinement must restore the raw labels.
    request["events"] = inferred
    request["options"]["tom_refinement"] = False
    (tmp_path / "request.json").write_text(json.dumps(request))
    pipeline.run(tmp_path)
    result = json.loads((tmp_path / "result.json").read_text())
    generation = tmp_path / result["generation"]
    assert json.loads((generation / "events.json").read_text()) == events
    assert not (generation / "tom-refinement.json").exists()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_retry_initial_refinement_resumes_from_onset_and_tempo_cache(tmp_path, monkeypatch, status):
    from transcription import pipeline, tom_refinement
    from transcription.app import create_app

    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        options = Options(tom_refinement=True, renderer="verovio", device="cpu").model_dump()
        identifier = client.post(
            "/api/jobs",
            files={"file": ("Synthetic.wav", b"audio")},
            data={"options": json.dumps(options)},
        ).json()["id"]
        directory = tmp_path / identifier
        events, _, _ = recording(directory, [120] * 5 + [240] * 5)
        tempo = {
            "estimated_bpm": 120,
            "estimated_offset": 0,
            "tempo_reliable": True,
            "warnings": [],
        }
        monkeypatch.setattr(pipeline, "convert_media", lambda *args: 11)
        monkeypatch.setattr(
            pipeline, "separate", lambda *args: (directory / "drums.wav", "cpu", [])
        )
        monkeypatch.setattr(pipeline, "transcribe", lambda *args: (events, "cpu", []))
        monkeypatch.setattr(pipeline, "estimate_tempo", lambda *args: (tempo, []))

        def interrupted(*args):
            raise RuntimeError("Interrupted during refinement")

        monkeypatch.setattr(tom_refinement, "refine_toms", interrupted)
        with pytest.raises(RuntimeError, match="Interrupted"):
            pipeline.run(directory)
        assert json.loads((directory / "progress.json").read_text())["stage"] == "refining"
        cached_analysis = (directory / "analysis.json").read_bytes()
        cached_events = (directory / "events.json").read_bytes()
        app.state.store.update(identifier, status)
        assert client.post(f"/api/jobs/{identifier}/retry").status_code == 202
        request = json.loads((directory / "request.json").read_text())
        assert request["revision"] and not request.get("reestimate_tempo")

        def forbidden(*args, **kwargs):
            raise AssertionError("A refining retry must reuse all completed inference")

        for name in ("convert_media", "separate", "transcribe", "estimate_tempo"):
            monkeypatch.setattr(pipeline, name, forbidden)
        monkeypatch.setattr(tom_refinement, "refine_toms", refine_toms)
        pipeline.run(directory)
        result = json.loads((directory / "result.json").read_text())
        assert result["tom_refinement"]["status"] == "completed"
        assert result["tom_refinement"]["low_count"] == result["tom_refinement"]["high_count"] == 5
        assert (directory / "analysis.json").read_bytes() == cached_analysis
        assert (directory / "events.json").read_bytes() == cached_events
