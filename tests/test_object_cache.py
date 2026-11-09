"""Artifact transfers do not serialize independent playback/cache requests."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from transcription.store import JobStore


class BlockingObjects:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.blocked = set()
        self.started = threading.Event()
        self.release = threading.Event()

    def get(self, key, path):
        self.calls.append(key)
        if key in self.blocked:
            self.started.set()
            if not self.release.wait(3):
                raise AssertionError("Test did not release its slow object transfer")
        path.write_bytes(self.values[key])


def object_manifest(objects, name, content):
    objects.values[name] = content
    return {
        "path": name,
        "object_key": name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def cache_store(tmp_path, objects):
    # The cache protocol needs no database and never opens a DB connection.
    return JobStore(tmp_path, db=SimpleNamespace(pool=None), objects=objects)


def test_slow_download_does_not_block_warm_playback_or_another_cold_artifact(tmp_path):
    objects = BlockingObjects()
    store = cache_store(tmp_path, objects)
    audio = object_manifest(objects, "audio.wav", b"already cached playback")
    drums = object_manifest(objects, "drums.wav", b"cold stem")
    score = object_manifest(objects, "score.svg", b"another cold artifact")
    cached_audio = store._cached_object(audio)
    objects.blocked.add("drums.wav")
    with ThreadPoolExecutor(max_workers=3) as executor:
        slow = executor.submit(store._cached_object, drums)
        assert objects.started.wait(1)
        try:
            warm = executor.submit(store._cached_object, audio)
            other = executor.submit(store._cached_object, score)
            assert warm.result(timeout=1) == cached_audio
            assert other.result(timeout=1).read_bytes() == b"another cold artifact"
            # Cache hydration must not reserve inference/cancellation state.
            assert store.lock.acquire(blocking=False)
            store.lock.release()
            assert objects.calls.count("audio.wav") == 1
            assert not slow.done()
        finally:
            objects.release.set()
        assert slow.result(timeout=1).read_bytes() == b"cold stem"


def test_same_cold_object_is_downloaded_only_once_for_concurrent_readers(tmp_path):
    objects = BlockingObjects()
    store = cache_store(tmp_path, objects)
    audio = object_manifest(objects, "audio.wav", b"one transfer shared by both readers")
    objects.blocked.add("audio.wav")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(store._cached_object, audio)
        assert objects.started.wait(1)
        second = executor.submit(store._cached_object, audio)
        objects.release.set()
        assert first.result(timeout=1) == second.result(timeout=1)
    assert objects.calls == ["audio.wav"]
    assert not list(store.root.rglob("*.tmp"))


def test_failed_integrity_check_never_publishes_partial_cached_media(tmp_path):
    objects = BlockingObjects()
    store = cache_store(tmp_path, objects)
    audio = object_manifest(objects, "audio.wav", b"expected bytes")
    objects.values["audio.wav"] = b"different data"
    with pytest.raises(OSError, match="integrity"):
        store._cached_object(audio)
    assert not list(store.root.rglob("audio.wav"))
    assert not list(store.root.rglob("*.tmp"))
