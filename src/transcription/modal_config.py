"""Names and model contract; importing this module never contacts Modal."""

import hashlib
import os

APP_NAME = os.environ.get("BEATIT_MODAL_APP", "beatit")
ENVIRONMENT = os.environ.get("MODAL_ENVIRONMENT", "main")
MODEL_VOLUME = APP_NAME + "-models"
JOB_VOLUME = APP_NAME + "-jobs"
PROGRESS_DICT = APP_NAME + "-progress"
MODEL_DIR = "/models"
JOB_DIR = "/jobs"
GPU = os.environ.get("BEATIT_MODAL_GPU", "T4")
MODEL_STORAGE = os.environ.get("BEATIT_MODAL_MODEL_STORAGE", "volume")
MODEL_CONTRACT = (
    "demucs==4.1.0:htdemucs;"
    "drumsep:da6dc5af5458a296e04cbf52b97babced90eb908:"
    "1f8e636fb674b88a52c8399fde9a4ebe2b72b065ca07eed4e03ab1c9f0bfb2e0;"
    "beat-this:8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331;"
    "adtof:85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"
)
MODEL_FINGERPRINT = hashlib.sha256(MODEL_CONTRACT.encode()).hexdigest()
IMAGE_NAME = os.environ.get(
    "BEATIT_MODAL_IMAGE",
    f"{APP_NAME}-processing:py312-cu128-{MODEL_STORAGE}-{MODEL_FINGERPRINT[:12]}-v1",
)
MANIFEST = MODEL_FINGERPRINT + "/manifest.json"
CACHE_ENV = {
    "XDG_CACHE_HOME": MODEL_DIR + "/" + MODEL_FINGERPRINT,
    "HF_HOME": MODEL_DIR + "/" + MODEL_FINGERPRINT + "/huggingface",
    "TORCH_HOME": MODEL_DIR + "/" + MODEL_FINGERPRINT + "/torch",
}
