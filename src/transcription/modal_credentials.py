"""Load service credentials without exposing them to jobs, logs or API responses."""

import os
from pathlib import Path


def load_credentials():
    """Support environment injection and mounted Docker/Kubernetes secret files.

    No explicit credentials leaves the SDK's existing local CLI profile available.
    File values are loaded only in the application process; never sent to a GPU.
    """
    values = {}
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        value = os.environ.get(name, "").strip()
        filename = os.environ.get(name + "_FILE", "")
        if filename and not value:
            try:
                with Path(filename).open() as source:
                    value = source.read(4097).strip()
            except (OSError, UnicodeError):
                raise RuntimeError(f"{name}_FILE 인증 파일을 읽을 수 없습니다.") from None
            if not value or len(value) > 4096:
                raise RuntimeError(f"{name}_FILE 인증 파일이 비어 있거나 잘못되었습니다.")
        if value:
            values[name] = value
    if values and len(values) != 2:
        raise RuntimeError("MODAL_TOKEN_ID와 MODAL_TOKEN_SECRET을 함께 설정하세요.")
    os.environ.update(values)
