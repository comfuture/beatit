"""Quantize onset events and emit semantic percussion MusicXML, independent of inference."""

import math
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import Options

DIVISIONS = 12
KIT = {
    35: ("Kick", "F", 4, "normal", 2),
    38: ("Snare", "C", 5, "normal", 1),
    47: ("Tom", "D", 5, "normal", 1),
    42: ("Hi-hat", "G", 5, "x", 1),
    49: ("Cymbal", "A", 5, "x", 1),
}


def el(parent, name, value=None, **attrs):
    child = ET.SubElement(parent, name, {key: str(value) for key, value in attrs.items()})
    if value is not None:
        child.text = str(value)
    return child


def quantize(events: list[dict], duration: float, options: Options) -> dict:
    bpm = options.bpm or 120
    offset = options.offset or 0
    numerator, denominator = map(int, options.meter.split("/"))
    bar_ticks = numerator * DIVISIONS * 4 // denominator
    step = {"16": 3, "8": 6, "triplet": 4}[options.grid]
    # A triplet grid over odd eighth-note meters cannot tile a measure.
    if bar_ticks % step:
        raise ValueError("이 박자에서는 셋잇단음표 격자를 사용할 수 없습니다.")
    max_tick = max(1, math.ceil(max(0, duration - offset) * bpm / 60 * DIVISIONS))
    bars = max(1, math.ceil(max_tick / bar_ticks))
    snapped = {}
    errors = []
    early = 0
    for event in events:
        if event["time"] < offset - 0.5 * step / DIVISIONS * 60 / bpm:
            early += 1
            continue
        tick = max(
            0, int(math.floor((event["time"] - offset) * bpm / 60 * DIVISIONS / step + 0.5)) * step
        )
        if tick >= bars * bar_ticks:
            bars = tick // bar_ticks + 1
        key = (tick, event["pitch"])
        candidate = {**event, "tick": tick, "quantized_time": offset + tick / DIVISIONS * 60 / bpm}
        if key not in snapped or candidate["strength"] > snapped[key]["strength"]:
            snapped[key] = candidate
        errors.append(abs(candidate["quantized_time"] - event["time"]))
    return {
        "bpm": bpm,
        "offset": offset,
        "meter": options.meter,
        "grid": options.grid,
        "bar_ticks": bar_ticks,
        "step": step,
        "bars": bars,
        "events": sorted(snapped.values(), key=lambda e: (e["tick"], e["pitch"])),
        "dropped_before_offset": early,
        "merged_events": len(events) - early - len(snapped),
        "mean_quantization_error_ms": round(sum(errors) / max(1, len(errors)) * 1000, 1),
    }


def write_musicxml(grid: dict, title: str, target: Path) -> None:
    root = ET.Element("score-partwise", version="4.0")
    el(el(root, "work"), "work-title", title)
    identification = el(root, "identification")
    el(identification, "creator", "Automatic drum transcription", type="composer")
    el(el(identification, "encoding"), "software", "Drum Score")
    defaults = el(root, "defaults")
    scaling = el(defaults, "scaling")
    el(scaling, "millimeters", 7)
    el(scaling, "tenths", 40)
    layout = el(defaults, "page-layout")
    el(layout, "page-height", 1697.14)
    el(layout, "page-width", 1200)
    margins = el(layout, "page-margins", type="both")
    for side in ("left", "right", "top", "bottom"):
        el(margins, f"{side}-margin", 80)
    part_list = el(root, "part-list")
    part_definition = el(part_list, "score-part", id="P1")
    el(part_definition, "part-name", "Drum set")
    el(part_definition, "part-abbreviation", "Dr.")
    for pitch, (name, _, _, _, _) in KIT.items():
        instrument_id = f"P1-I{pitch}"
        instrument = el(part_definition, "score-instrument", id=instrument_id)
        el(instrument, "instrument-name", name)
    for pitch in KIT:
        instrument_id = f"P1-I{pitch}"
        midi = el(part_definition, "midi-instrument", id=instrument_id)
        el(midi, "midi-channel", 10)
        el(midi, "midi-unpitched", pitch + 1)  # MusicXML is 1-based; GM notes are 0-based.
    part = el(root, "part", id="P1")
    by_bar = defaultdict(list)
    for event in grid["events"]:
        by_bar[event["tick"] // grid["bar_ticks"]].append(event)
    numerator, denominator = grid["meter"].split("/")
    for bar in range(grid["bars"]):
        measure = el(part, "measure", number=bar + 1)
        if bar == 0:
            attributes = el(measure, "attributes")
            el(attributes, "divisions", DIVISIONS)
            time = el(attributes, "time")
            el(time, "beats", numerator)
            el(time, "beat-type", denominator)
            el(el(attributes, "clef"), "sign", "percussion")
            el(el(attributes, "staff-details"), "staff-lines", 5)
            direction = el(measure, "direction", placement="above")
            metronome = el(el(direction, "direction-type"), "metronome")
            el(metronome, "beat-unit", "quarter")
            el(metronome, "per-minute", round(grid["bpm"], 2))
            el(direction, "sound", tempo=round(grid["bpm"], 4))
        for voice in (1, 2):
            if voice == 2:
                el(el(measure, "backup"), "duration", grid["bar_ticks"])
            hits = defaultdict(list)
            for event in by_bar[bar]:
                if KIT[event["pitch"]][4] == voice:
                    hits[event["tick"] % grid["bar_ticks"]].append(event["pitch"])
            _write_voice(measure, hits, voice, grid)
        if bar == grid["bars"] - 1:
            el(el(measure, "barline", location="right"), "bar-style", "light-heavy")
    ET.indent(root, space="  ")
    target.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _write_voice(measure, hits, voice: int, grid: dict):
    total = grid["bar_ticks"]
    if not hits:
        note = el(measure, "note")
        el(note, "rest", measure="yes")
        el(note, "duration", total)
        el(note, "voice", voice)
        return
    tick = 0
    while tick < total:
        pitches = sorted(hits.get(tick, []))
        # Keep rhythmic units within quarter-note beats, including trailing partial beats.
        beat_end = min(total, (tick // DIVISIONS + 1) * DIVISIONS)
        next_hit = min((t for t in hits if t > tick), default=total)
        duration = min(next_hit, beat_end) - tick
        if grid["grid"] == "triplet" and (pitches or duration < DIVISIONS):
            duration = grid["step"]
        for chord_index, pitch in enumerate(pitches or [None]):
            note = el(measure, "note")
            if chord_index:
                el(note, "chord")
            if pitch is None:
                el(note, "rest")
            else:
                _, display_step, octave, head, _ = KIT[pitch]
                unpitched = el(note, "unpitched")
                el(unpitched, "display-step", display_step)
                el(unpitched, "display-octave", octave)
            el(note, "duration", duration)
            if pitch is not None:
                el(note, "instrument", id=f"P1-I{pitch}")
            el(note, "voice", voice)
            is_triplet = grid["grid"] == "triplet" and duration == 4
            note_type = (
                "eighth"
                if is_triplet
                else {3: "16th", 6: "eighth", 9: "eighth", 12: "quarter"}[duration]
            )
            el(note, "type", note_type)
            if duration == 9:
                el(note, "dot")
            if is_triplet:
                modification = el(note, "time-modification")
                el(modification, "actual-notes", 3)
                el(modification, "normal-notes", 2)
                el(modification, "normal-type", "eighth")
            if pitch is not None:
                el(note, "stem", "up" if voice == 1 else "down")
                if head == "x":
                    el(note, "notehead", "x")
            if is_triplet and tick % 12 in (0, 8):
                el(
                    el(note, "notations"),
                    "tuplet",
                    type="start" if tick % 12 == 0 else "stop",
                    number="1",
                )
        tick += duration
