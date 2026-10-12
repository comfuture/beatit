import json

import pytest
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.score_layout import score_layout


def musescore_page():
    # Shuffled draw order, unequal bar widths, and a two-stroke final barline.
    staff = "".join(
        f'<polyline class="StaffLines" points="100,{y} 900,{y}"/>'
        for y in (200, 220, 240, 260, 280)
    )
    bars = "".join(
        f'<polyline class="BarLine" points="{x},198 {x},282"/>' for x in (897, 350, 882, 650)
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1400">{bars}{staff}</svg>'


def test_musescore_uses_actual_boundaries_across_pages(tmp_path):
    for name in ("score-1.svg", "score-2.svg"):
        (tmp_path / name).write_text(musescore_page())
    measures = score_layout(
        tmp_path,
        {
            "renderer": "musescore",
            "pages": ["score-1.svg", "score-2.svg"],
            "grid": {"bars": 6},
        },
    )
    assert [m["page"] for m in measures] == [1, 1, 1, 2, 2, 2]
    assert [m["bar"] for m in measures] == list(range(1, 7))
    assert measures[0]["x"] == 0.1
    assert measures[0]["width"] == pytest.approx(0.25)
    assert measures[1]["width"] == pytest.approx(0.3)
    assert measures[2]["x"] + measures[2]["width"] == pytest.approx(0.9)


def test_verovio_nested_viewport_and_page_margin(tmp_path):
    staff = "".join(f'<path d="M100 {y} L400 {y}"/>' for y in range(200, 281, 20))
    (tmp_path / "score.svg").write_text(f"""<svg viewBox="0 0 200 280">
      <svg class="definition-scale" viewBox="0 0 1000 1400">
        <g class="page-margin" transform="translate(50, 60)">
          <g class="system"><g class="measure"><g class="staff">{staff}</g></g></g>
        </g>
      </svg></svg>""")
    measure = score_layout(
        tmp_path,
        {
            "renderer": "verovio",
            "pages": ["score.svg"],
            "grid": {"bars": 1},
        },
    )[0]
    assert measure["x"] == pytest.approx(0.15)
    assert measure["y"] == pytest.approx(200 / 1400)
    assert measure["width"] == pytest.approx(0.3)
    assert measure["height"] == pytest.approx(180 / 1400)


@pytest.mark.parametrize("svg,bars", [(musescore_page(), 4), ("<svg/>", 3), ("<svg", 3)])
def test_unrecognized_or_incomplete_layout_never_guesses_bars(tmp_path, svg, bars):
    (tmp_path / "score.svg").write_text(svg)
    with pytest.raises(ValueError):
        score_layout(
            tmp_path,
            {
                "renderer": "musescore",
                "pages": ["score.svg"],
                "grid": {"bars": bars},
            },
        )


def test_layout_api_pins_generation_and_refreshes_changed_files(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        identifier = client.post("/api/jobs", files={"file": ("song.wav", b"x")}).json()["id"]
        url = f"/api/jobs/{identifier}/score-layout"
        assert client.get(url, params={"generation": "render-1"}).status_code == 404
        directory = tmp_path / identifier
        render = directory / "render-1"
        render.mkdir()
        (render / "score.svg").write_text(musescore_page())
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "generation": "render-1",
                    "renderer": "musescore",
                    "pages": ["score.svg"],
                    "grid": {"bars": 3},
                }
            )
        )
        assert client.get(url, params={"generation": "render-0"}).status_code == 409
        response = client.get(url, params={"generation": "render-1"})
        assert response.status_code == 200
        assert len(response.json()["measures"]) == 3
        (render / "score.svg").write_text("<svg/>")
        assert client.get(url, params={"generation": "render-1"}).json()["measures"] == []
