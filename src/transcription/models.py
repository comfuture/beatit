from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def validate_grid(self):
        if self.meter == "7/8" and self.grid == "triplet":
            raise ValueError("7/8 박자에서는 8분 또는 16분음표 격자를 선택하세요.")
        return self


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    time: float = Field(ge=0, le=7200)
    pitch: Literal[35, 38, 47, 42, 46, 49]
    strength: float = Field(default=0.7, ge=0, le=1)
    velocity: int | None = Field(default=None, ge=1, le=127)


class Revision(BaseModel):
    options: Options
    events: list[Event] | None = Field(default=None, max_length=100000)
