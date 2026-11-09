import json
import zipfile

import pytest

from transcription import pipeline
from transcription.models import Options

pytestmark = pytest.mark.mysql


REVIEW = {
    "grid_key": '[120,0,"4/4",48,3]',
    "beats": [{"tick": 0, "reasons": ["too-many-hands"], "resolved": True}],
}


def test_review_survives_revision_worker_metadata_and_reload(
    tmp_path,
    authenticated_app,
    authenticated_client,
):
    app = authenticated_app(tmp_path)
    with authenticated_client(app) as client:
        identifier = client.post("/api/jobs", files={"file": ("song.wav", b"x")}).json()["id"]
        directory = tmp_path / identifier
        options = Options(bpm=120, offset=0, renderer="verovio").model_dump()
        events = [{"time": 0, "pitch": pitch, "strength": 0.8} for pitch in (38, 42, 49)]
        analysis = {"duration": 2, "estimated_bpm": 120, "tempo_reliable": True}
        pipeline.save_json(directory / "analysis.json", analysis)
        pipeline.save_json(directory / "events.json", events)
        pipeline.make_score(
            directory,
            {"filename": "song.wav", "options": options},
            analysis,
            events,
            pipeline.Progress(directory),
        )
        app.state.store.update(identifier, "completed")
        response = client.post(
            f"/api/jobs/{identifier}/score",
            json={"options": options, "beat_review": REVIEW},
        )
        assert response.status_code == 202
        assert response.json()["request"]["beat_review"] == REVIEW
        assert response.json()["result"]["beat_review"] is None
        pipeline.run(directory)
        app.state.store.update(identifier, "completed")
        saved = client.get(f"/api/jobs/{identifier}").json()["result"]
        assert saved["beat_review"] == REVIEW
        assert json.loads((directory / saved["generation"] / "events.json").read_text()) == events
        with zipfile.ZipFile(directory / saved["generation"] / "score-bundle.zip") as bundle:
            assert json.loads(bundle.read("analysis.json"))["beat_review"] == REVIEW
        response = client.post(
            f"/api/jobs/{identifier}/score-metadata",
            json={"title": "Reviewed", "author": ""},
        )
        assert response.status_code == 202
        pipeline.run(directory)
        app.state.store.update(identifier, "completed")
        assert client.get(f"/api/jobs/{identifier}").json()["result"]["beat_review"] == REVIEW
        # Older clients do not erase confirmations when they revise without this field.
        response = client.post(f"/api/jobs/{identifier}/score", json={"options": options})
        assert response.json()["request"]["beat_review"] == REVIEW


@pytest.mark.parametrize(
    "beat",
    [
        {"tick": -1, "reasons": ["too-many-hands"]},
        {"tick": 0, "reasons": ["unknown-reason"]},
        {"tick": 0, "reasons": []},
    ],
)
def test_review_api_rejects_invalid_markers(
    tmp_path,
    beat,
    authenticated_app,
    authenticated_client,
):
    with authenticated_client(authenticated_app(tmp_path)) as client:
        identifier = client.post("/api/jobs", files={"file": ("song.wav", b"x")}).json()["id"]
        response = client.post(
            f"/api/jobs/{identifier}/score",
            json={
                "options": {},
                "beat_review": {"grid_key": REVIEW["grid_key"], "beats": [beat]},
            },
        )
        assert response.status_code == 422
