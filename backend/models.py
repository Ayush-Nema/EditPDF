from pydantic import BaseModel


class UploadResponse(BaseModel):
    doc_id: str
    page_count: int


class TextSpan(BaseModel):
    index: int
    text: str
    bbox: list[float]  # [x0, y0, x1, y1]
    font: str
    size: float
    color: str  # hex "#rrggbb"
    flags: int  # bold/italic/etc bitmask


class PageTextResponse(BaseModel):
    page_num: int
    width: float
    height: float
    spans: list[TextSpan]


class EditRequest(BaseModel):
    span_index: int
    new_text: str
    font: str | None = None
    size: float | None = None
    color: str | None = None  # hex "#rrggbb"


class AddTextRequest(BaseModel):
    x: float
    y: float
    text: str
    font: str = "helv"
    size: float = 12.0
    color: str = "#000000"
