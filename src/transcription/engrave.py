import json
import os
import platform
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from .runtime import musescore_path


def render(xml: Path, destination: Path, requested: str = "auto") -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    binary = musescore_path()
    if requested == "musescore" and not binary:
        raise RuntimeError(
            "MuseScore가 없습니다. MUSESCORE_BIN을 설정하거나 자동 렌더러를 선택하세요."
        )
    if binary and requested != "verovio":
        env = {**os.environ, "SKIP_LIBJACK": "1"}
        if platform.system() == "Linux":
            env["QT_QPA_PLATFORM"] = "offscreen"
        job = destination / "engrave-job.json"
        job.write_text(
            json.dumps(
                [
                    {
                        "in": str(xml),
                        "out": [str(destination / "score.svg"), str(destination / "score.pdf")],
                    }
                ]
            )
        )
        result = subprocess.run(
            [binary, "-j", str(job)],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (destination / "engraver.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        pages = sorted(destination.glob("score*.svg"), key=_page_key)
        valid_pages = bool(pages)
        for page in pages:
            try:
                root = ET.parse(page).getroot()
                valid_pages = valid_pages and root.tag.endswith("svg") and len(root) > 0
            except ET.ParseError:
                valid_pages = False
        if not valid_pages:
            raise RuntimeError(
                "MuseScore SVG 변환 실패: " + (result.stderr or result.stdout)[-2000:]
            )
        # Some macOS builds abort during shutdown after producing valid outputs.
        # Accept only outputs from this fresh generation after structural validation.
        warning = []
        if result.returncode:
            warning.append(
                f"MuseScore 종료 코드 {result.returncode}; 생성된 SVG 구조는 검증되었습니다. engraver.log를 확인하세요."
            )
        pdf = destination / "score.pdf"
        pdf_valid = False
        if pdf.is_file():
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            try:
                pdf_valid = len(PdfReader(pdf).pages) == len(pages)
            except (PdfReadError, ValueError, OSError):
                pass
        if not pdf_valid:
            pdf.unlink(missing_ok=True)
            warning.append("PDF 변환에 실패했습니다. SVG를 인쇄하세요.")
        return {
            "renderer": "musescore",
            "pages": [p.name for p in pages],
            "pdf": pdf_valid,
            "warnings": warning,
        }
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
    return {"renderer": "verovio", "pages": pages, "pdf": False, "warnings": []}


def _page_key(path: Path):
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else 0
