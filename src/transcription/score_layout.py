"""Locate bars in the single-staff SVG scores produced by our two engravers.

Coordinates are fractions of the page, so the browser can overlay an ordinary
image without executing SVG markup. Reject unfamiliar layouts instead of
guessing which bar is playing.
"""

import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
LINE = re.compile(rf"M\s*({NUMBER})[ ,]+({NUMBER})\s*L\s*({NUMBER})[ ,]+({NUMBER})\s*")


def _numbers(value):
    return [float(v) for v in re.split(r"[\s,]+", value.strip()) if v]


def _staff(lines):
    lines = sorted(lines, key=lambda line: line[1])
    if len(lines) != 5:
        raise ValueError("Expected a five-line percussion staff")
    left, top, right, _ = lines[0]
    bottom = lines[-1][1]
    space = (bottom - top) / 4
    if (
        space <= 0
        or right <= left
        or any(
            abs(x1 - left) > 1
            or abs(x2 - right) > 1
            or abs(y1 - y2) > 1
            or abs(y1 - top - i * space) > 1
            for i, (x1, y1, x2, y2) in enumerate(lines)
        )
    ):
        raise ValueError("Unsupported staff geometry")
    return left, top, right, bottom, space


def _musescore(root):
    # MuseScore emits absolute polylines, not semantic measure groups.
    lines, barlines = [], []
    for element in root.iter():
        if element.get("transform"):
            raise ValueError("Unexpected MuseScore transform")
        kind = element.get("class")
        if kind in {"StaffLines", "BarLine"}:
            points = _numbers(element.get("points", ""))
            if len(points) != 4:
                raise ValueError("Unsupported line")
            (lines if kind == "StaffLines" else barlines).append(points)
    lines.sort(key=lambda line: line[1])
    boxes = []
    for start in range(0, len(lines), 5):
        left, top, right, bottom, space = _staff(lines[start : start + 5])
        ends = sorted(
            x1
            for x1, y1, x2, y2 in barlines
            if abs(x1 - x2) < 1
            and abs(y1 - top) < space / 2
            and abs(y2 - bottom) < space / 2
            and x1 > left + space
        )
        boundaries = []
        for x in ends:
            # The thin and thick strokes of a final/double bar are one boundary.
            if boundaries and x - boundaries[-1] < space * 1.5:
                boundaries[-1] = x
            else:
                boundaries.append(x)
        if not boundaries or abs(boundaries[-1] - right) > space:
            raise ValueError("Missing system end")
        boundaries[-1] = right
        for end in boundaries:
            boxes.append((left, top - 3 * space, end, bottom + 2 * space))
            left = end
    return boxes, _numbers(root.get("viewBox", ""))


def _transform(value, parent):
    # Only axis-aligned transforms occur on Verovio staff ancestors.
    sx, sy, tx, ty = parent
    remaining = value
    for match in re.finditer(r"(translate|scale)\s*\(([^)]+)\)", value):
        kind, args = match.groups()
        values = _numbers(args)
        if len(values) not in (1, 2):
            raise ValueError("Unsupported transform")
        x, y = values[0], values[-1] if kind == "scale" else (values[1] if len(values) == 2 else 0)
        if kind == "translate":
            tx, ty = tx + sx * x, ty + sy * y
        else:
            sx, sy = sx * x, sy * y
        remaining = remaining.replace(match.group(), "", 1)
    if remaining.strip() or sx <= 0 or sy <= 0:
        raise ValueError("Unsupported transform")
    return sx, sy, tx, ty


def _verovio(root):
    canvas = next((e for e in root if e.get("class") == "definition-scale"), root)
    viewport = _numbers(canvas.get("viewBox", ""))
    outer = _numbers(root.get("viewBox", ""))
    if (
        len(viewport) != 4
        or len(outer) != 4
        or not math.isclose(viewport[2] / viewport[3], outer[2] / outer[3], rel_tol=0.001)
    ):
        raise ValueError("Unsupported SVG viewport")
    boxes = []

    def walk(element, transform):
        transform = _transform(element.get("transform", ""), transform)
        if "measure" in element.get("class", "").split():
            staffs = [e for e in element if e.get("class") == "staff"]
            if len(staffs) != 1:
                raise ValueError("Expected a single percussion staff")
            staff = staffs[0]
            sx, sy, tx, ty = _transform(staff.get("transform", ""), transform)
            lines = []
            for e in staff:
                match = LINE.fullmatch(e.get("d", ""))
                if match:
                    if e.get("transform"):
                        raise ValueError("Unexpected staff transform")
                    x1, y1, x2, y2 = map(float, match.groups())
                    lines.append((sx * x1 + tx, sy * y1 + ty, sx * x2 + tx, sy * y2 + ty))
            left, top, right, bottom, space = _staff(lines)
            boxes.append((left, top - 3 * space, right, bottom + 2 * space))
            return
        for child in element:
            if child.tag.rsplit("}", 1)[-1] not in {"defs", "pgHead", "pgFoot"}:
                # Only ancestors of measures affect their coordinates.
                if any("measure" in e.get("class", "").split() for e in child.iter()):
                    walk(child, transform)

    walk(canvas, (1, 1, 0, 0))
    return boxes, viewport


@lru_cache(maxsize=32)
def _layout(files, renderer, expected_bars):
    measures = []
    parser = {"musescore": _musescore, "verovio": _verovio}.get(renderer)
    if not parser:
        raise ValueError("Unknown engraver")
    for page, (filename, _mtime, _size) in enumerate(files, 1):
        boxes, viewport = parser(ET.parse(filename).getroot())
        if len(viewport) != 4 or viewport[2] <= 0 or viewport[3] <= 0:
            raise ValueError("Invalid SVG viewport")
        vx, vy, width, height = viewport
        for left, top, right, bottom in boxes:
            x, y = max(0, (left - vx) / width), max(0, (top - vy) / height)
            w, h = min(1, (right - vx) / width) - x, min(1, (bottom - vy) / height) - y
            if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
                raise ValueError("Invalid measure bounds")
            measures.append(
                {"bar": len(measures) + 1, "page": page, "x": x, "y": y, "width": w, "height": h}
            )
    if not measures or len(measures) != expected_bars:
        raise ValueError("SVG measure count does not match the beat grid")
    return measures


def score_layout(directory: Path, result: dict):
    files = []
    for name in result["pages"]:
        path = directory / name
        stat = path.stat()
        files.append((str(path), stat.st_mtime_ns, stat.st_size))
    try:
        return _layout(tuple(files), result["renderer"], result["grid"]["bars"])
    except (ET.ParseError, ValueError, ZeroDivisionError) as error:
        raise ValueError("Measure positions are unavailable for this score") from error
