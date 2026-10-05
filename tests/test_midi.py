import pretty_midi

from transcription.pipeline import write_midi


def test_midi_preserves_drum_mapping_meter_and_quantized_onset(tmp_path):
    events = [{"time": 0.11, "quantized_time": 0.125, "pitch": 38, "strength": 0.8}]
    path = tmp_path / "drums.mid"
    write_midi(events, path, 120, "7/8")
    midi = pretty_midi.PrettyMIDI(str(path))
    assert midi.instruments[0].is_drum
    note = midi.instruments[0].notes[0]
    assert note.pitch == 38
    assert abs(note.start - 0.125) < 0.002
    assert midi.time_signature_changes[0].numerator == 7
    assert midi.time_signature_changes[0].denominator == 8
