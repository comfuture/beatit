"""Serve existing private playback URLs when the application origin is direct."""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .auth import require_user

SOURCES = {"audio.wav", "drums.wav"} | {
    f"stem-{name}.flac" for name in ("kick", "snare", "toms", "hh", "cymbals")
}


def install_origin_media(app, store):
    @app.api_route("/media/{identifier}/{source}", methods=["GET", "HEAD"])
    def playback(identifier: str, source: str, request: Request, user=Depends(require_user)):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-origin media requests are disabled")
        if source not in SOURCES or request.url.query:
            raise HTTPException(404, "오디오가 없습니다.")
        try:
            path, manifest, content_type = store.playback_file(
                identifier, source, owner_id=user["id"]
            )
        except KeyError:
            raise HTTPException(404, "오디오가 없습니다.") from None
        return FileResponse(
            path,
            media_type=content_type,
            headers={
                "Cache-Control": "private, no-store",
                "Vary": "Cookie",
                "ETag": '"' + manifest["sha256"] + '"',
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Beatit-Media": "origin",
            },
        )
