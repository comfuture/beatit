import os
import re
import subprocess
from pathlib import Path

from .runtime import musescore_path


def render(xml: Path, destination: Path, requested: str = "auto") -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    binary = musescore_path()
    if requested == "musescore" and not binary:
        raise RuntimeError(
            "MuseScore가 없습니다. MUSESCORE_BIN을 설정하거나 자동 렌더러를 선택하세요."
        )
    if binary and requested != "verovio":
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "SKIP_LIBJACK": "1"}
        result = subprocess.run(
            [binary, "-s", "-o", str(destination / "score.svg"), str(xml)],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        pages = sorted(destination.glob("score*.svg"), key=_page_key)
        if result.returncode or not pages:
            raise RuntimeError(
                "MuseScore SVG 변환 실패: " + (result.stderr or result.stdout)[-2000:]
            )
        # PDF is optional; SVG and MusicXML are the required deliverables.
        pdf_result = subprocess.run(
            [binary, "-s", "-o", str(destination / "score.pdf"), str(xml)],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        warning = (
            [] if pdf_result.returncode == 0 else ["PDF 변환에 실패했습니다. SVG를 인쇄하세요."]
        )
        return {"renderer": "musescore", "pages": [p.name for p in pages], "warnings": warning}
    import verovio

    toolkit = verovio.toolkit()
    toolkit.setOptions(
        {
            "inputFrom": "musicxml",
            "pageHeight": 2970,
            "pageWidth": 2100,
            "scale": 40,
            "adjustPageHeight": False,
            "breaks": "auto",
            "svgViewBox": True,
            "svgHtml5": True,
        }
    )
    if not toolkit.loadFile(str(xml)):
        raise RuntimeError("Verovio가 MusicXML을 읽지 못했습니다.")
    pages = []
    for number in range(1, toolkit.getPageCount() + 1):
        page = destination / f"score-{number}.svg"
        page.write_text(toolkit.renderToSVG(number), encoding="utf-8")
        pages.append(page.name)
    if not pages:
        raise RuntimeError("악보 페이지가 생성되지 않았습니다.")
    return {"renderer": "verovio", "pages": pages, "warnings": []}


def _page_key(path: Path):
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else 0
