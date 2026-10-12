from functools import lru_cache


@lru_cache(maxsize=24)
def waveform_peaks(path: str, modified: int, size: int) -> dict:
    """Read bounded blocks, retaining transients without decoding a whole song in the browser.

    Modification time and file size invalidate cached peaks when an audio artifact changes.
    """
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as audio:
        if not audio.frames:
            raise ValueError("Empty audio")
        bins = min(1200, audio.frames)
        boundaries = np.linspace(0, audio.frames, bins + 1, dtype=np.int64)
        peaks = []
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            remaining = int(end - start)
            peak = 0.0
            while remaining:
                block = audio.read(min(65536, remaining), dtype="float32", always_2d=True)
                if not len(block):
                    raise ValueError("Incomplete audio")
                peak = max(peak, float(np.max(np.abs(block))))
                remaining -= len(block)
            peaks.append(round(min(1.0, peak), 4))
        return {"duration": audio.frames / audio.samplerate, "peaks": peaks}
