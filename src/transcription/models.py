from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bpm: float | None = Field(default=None, ge=30, le=300)
    # Negative values start bar 1 before the audio, so pickup hits stay on the grid.
    offset: float | None = Field(default=None, ge=-10, le=120)
    meter: Literal["4/4", "3/4", "6/8", "7/8"] = "4/4"
    grid: Literal["16", "8", "triplet"] = "16"
    device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    renderer: Literal["auto", "musescore", "verovio"] = "auto"
    source: Literal["mix", "drums"] = "mix"
    sensitivity: float = Field(default=1, ge=0.5, le=1.5)
    tom_refinement: bool = True

    @model_validator(mode="after")
    def validate_grid(self):
        if self.meter == "7/8" and self.grid == "triplet":
            raise ValueError("7/8 박자에서는 8분 또는 16분음표 격자를 선택하세요.")
        return self


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    time: float = Field(ge=0, le=7200)
    pitch: Literal[35, 38, 45, 47, 50, 42, 46, 49]
    strength: float = Field(default=0.7, ge=0, le=1)
    velocity: int | None = Field(default=None, ge=1, le=127)
    tom_source_pitch: Literal[47] | None = None
    tom_match_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_tom_provenance(self):
        if self.tom_source_pitch is not None and self.pitch not in (45, 47, 50):
            raise ValueError("자동 탐 보정 태그는 탐 이벤트에만 사용할 수 있습니다.")
        return self


class ScoreMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(max_length=256)
    author: str = Field(default="", max_length=256)

    @field_validator("title", "author")
    @classmethod
    def single_line_xml_text(cls, value: str) -> str:
        if any(
            ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
            for char in value
        ):
            raise ValueError("제목과 저자는 줄바꿈이나 제어 문자 없이 입력하세요.")
        return value

    @classmethod
    def for_filename(cls, filename: str):
        return cls(title=Path(filename).stem, author="Automatic drum transcription")


class BeatReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tick: int = Field(ge=0, le=1000000)
    reasons: list[
        Literal[
            "too-many-hands",
            "hat-cymbal-sequence",
            "conflicting-hats",
            "duplicate-hit",
            "off-grid",
        ]
    ] = Field(min_length=1, max_length=5)
    resolved: bool = False


class BeatReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grid_key: str = Field(max_length=256)
    beats: list[BeatReviewItem] = Field(default_factory=list, max_length=100000)


class Revision(BaseModel):
    options: Options
    events: list[Event] | None = Field(default=None, max_length=100000)
    metadata: ScoreMetadata | None = None
    beat_review: BeatReview | None = None
