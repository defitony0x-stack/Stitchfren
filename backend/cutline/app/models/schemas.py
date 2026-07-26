"""
Pydantic models for Stitchfren.

These are the shared contracts between app/api/main.py, app/drafting/engine.py,
app/nesting/engine.py, and app/workers/tasks.py. All measurements are in cm.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class PatternStyle(str, Enum):
    bodice_aline = "bodice_aline"
    bodice_straight = "bodice_straight"
    bodice_aline_sleeved = "bodice_aline_sleeved"
    bodice_top = "bodice_top"
    skirt_straight = "skirt_straight"
    skirt_aline = "skirt_aline"
    mens_shirt = "mens_shirt"
    mens_shirt_short_sleeve = "mens_shirt_short_sleeve"


class Measurements(BaseModel):
    """
    Body measurements in cm. Only bust_or_chest and waist are always required,
    since drafting/engine.py only touches the fields relevant to the chosen
    style (e.g. mens_shirt never reads back_length or skirt_length).
    Everything else has a default so a partial measurement set doesn't fail
    validation before we even know which style needs which fields.
    """

    bust_or_chest: float = Field(..., gt=0)
    waist: float = Field(..., gt=0)
    hip: Optional[float] = None
    ease: float = 2.0

    back_length: float = 40.0
    skirt_length: float = 55.0
    shoulder_width: Optional[float] = None

    sleeve_length: float = 60.0
    shirt_length: float = 70.0


class PatternRequest(BaseModel):
    style: PatternStyle
    measurements: Measurements
    fabric_width_cm: float = Field(..., gt=0)
    include_seam_allowance: bool = True
    seam_allowance_cm: float = 1.0
    allow_90_rotation: bool = False


class PiecePlacement(BaseModel):
    label: str
    grid_row: int = 0
    grid_col: int = 0
    x_offset_cm: float
    y_offset_cm: float
    rotated_180: bool = False


class NestingResult(BaseModel):
    placements: List[PiecePlacement]
    fabric_length_used_cm: float
    piece_area_cm2: float
    used_area_cm2: float
    waste_pct: float


class NaiveResult(BaseModel):
    fabric_length_used_cm: float
    used_area_cm2: float
    waste_pct: float


class PatternResponse(BaseModel):
    ok: bool = True
    pattern_svg: str
    layout_svg: str
    nested: NestingResult
    naive: NaiveResult
    fabric_saved_cm: float
    fabric_saved_pct: float
    dxf_url: Optional[str] = None
    result_hash: str
    cutting_sheet: dict
    warnings: List[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    ok: bool = False
    detail: str
