from xml.etree import ElementTree as ET

import pytest

from transcription.models import Options
from transcription.score import quantize, write_musicxml


@pytest.mark.parametrize(
    "meter,grid_name",
    [("4/4", "16"), ("3/4", "8"), ("6/8", "16"), ("7/8", "8"), ("4/4", "triplet")],
)
def test_every_voice_fills_measure_and_chords_do_not_advance_time(tmp_path, meter, grid_name):
    events = [
        {"time": 0, "pitch": 35, "strength": 0.9},
        {"time": 0, "pitch": 42, "strength": 0.7},
        {"time": 0, "pitch": 49, "strength": 0.8},
        {"time": 0.5, "pitch": 38, "strength": 0.8},
        {"time": 1.375, "pitch": 47, "strength": 0.8},
    ]
    options = Options(bpm=120, offset=0, meter=meter, grid=grid_name)
    grid = quantize(events, 8.01, options)
    path = tmp_path / "score.musicxml"
    write_musicxml(grid, "Test <&> title", path)
    root = ET.parse(path).getroot()
    assert root.findtext("work/work-title") == "Test <&> title"
    assert root.findtext("part/measure/attributes/clef/sign") == "percussion"
    for measure in root.findall("part/measure"):
        for voice in ("1", "2"):
            notes = [n for n in measure.findall("note") if n.findtext("voice") == voice]
            assert (
                sum(int(n.findtext("duration")) for n in notes if n.find("chord") is None)
                == grid["bar_ticks"]
            )
        assert int(measure.findtext("backup/duration")) == grid["bar_ticks"]
    instruments = root.findall("part-list/score-part/midi-instrument")
    assert {int(i.findtext("midi-unpitched")) for i in instruments} == {36, 39, 43, 48, 50}
    assert all(i.findtext("midi-channel") == "10" for i in instruments)
    assert len(root.findall(".//unpitched")) == len(grid["events"])


def test_snapping_merges_duplicates_but_preserves_different_instruments():
    grid = quantize(
        [
            {"time": 0.01, "pitch": 35, "strength": 0.3},
            {"time": 0.02, "pitch": 35, "strength": 0.9},
            {"time": 0.02, "pitch": 42, "strength": 0.5},
        ],
        2,
        Options(bpm=120),
    )
    assert len(grid["events"]) == 2
    assert grid["merged_events"] == 1
    assert grid["events"][0]["strength"] == 0.9


def test_offset_does_not_silently_shift_early_events():
    grid = quantize([{"time": 0.1, "pitch": 35, "strength": 0.8}], 4, Options(bpm=120, offset=1))
    assert grid["dropped_before_offset"] == 1
    assert grid["events"] == []


def test_rejects_triplets_that_do_not_tile_meter():
    with pytest.raises(ValueError):
        quantize([], 4, Options(meter="7/8", grid="triplet"))


def test_verovio_engraves_real_musicxml_to_vector_pages(tmp_path):
    from transcription.engrave import render

    grid = quantize([{"time": 0, "pitch": 35, "strength": 0.8}], 40, Options(bpm=120))
    xml = tmp_path / "score.musicxml"
    write_musicxml(grid, "Percussion test", xml)
    result = render(xml, tmp_path / "pages", "verovio")
    assert result["pages"]
    for page in result["pages"]:
        root = ET.parse(tmp_path / "pages" / page).getroot()
        assert root.tag.endswith("svg")
        assert len(root.findall(".//{http://www.w3.org/2000/svg}path")) > 5
