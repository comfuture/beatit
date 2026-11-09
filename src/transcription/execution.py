"""Engine selection shared by the API and cancellable job worker."""

import os
from typing import Literal

Engine = Literal["modal", "local", "colab"]
ENGINES = ("modal", "local", "colab")


def default_engine() -> Engine:
    engine = os.environ.get("DRUM_EXECUTION_BACKEND", "local")
    if engine not in ENGINES:
        raise ValueError(f"Unknown processing engine: {engine}")
    return engine


def execution_capabilities(local: dict) -> dict:
    selected = default_engine()
    result = {
        **local,
        "execution_backend": selected,
        "execution_label": "로컬 엔진",
    }
    if selected == "colab":
        from .colab_worker import execution_capabilities as colab_capabilities

        result.update(colab_capabilities(local))
    elif selected == "modal":
        from .modal_worker import runtime_status

        result.update(
            execution_label="Modal GPU",
            devices=["cuda"],
            default_device="cuda",
            ffmpeg=True,
            musescore=False,
            verovio=True,
            remote_runtime=runtime_status(),
        )
    return result


def run(directory):
    import json

    from .pipeline import run as local_run

    request = json.loads((directory / "request.json").read_text())
    engine = default_engine()
    needs_inference = not request.get("revision") and not request.get("metadata_only")
    if (needs_inference or request.get("reestimate_tempo")) and engine != "local":
        if engine == "modal":
            from .modal_worker import run as remote_run
        elif engine == "colab":
            from .colab_worker import run as remote_run
        else:
            raise ValueError(f"Unknown processing engine: {engine}")
        remote_run(directory)
    else:
        local_run(directory)
