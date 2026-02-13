"""Pydantic request and response models for the API."""

from pydantic import BaseModel

from .config import DEFAULT_FONT, DEFAULT_FONT_SIZE, DEFAULT_TEXT_COLOR


class UploadResponse(BaseModel):
    doc_id: str
    page_count: int


class TextSpan(BaseModel):
    index: int
    text: str
    bbox: list[float]  # [x0, y0, x1, y1]
    font: str
    normalized_font: str  # Base14 font used for edits
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
    font: str = DEFAULT_FONT
    size: float = DEFAULT_FONT_SIZE
    color: str = DEFAULT_TEXT_COLOR


class ImageInfo(BaseModel):
    index: int
    bbox: list[float]  # [x0, y0, x1, y1]
    width: float
    height: float
    xref: int


class PageImagesResponse(BaseModel):
    page_num: int
    width: float
    height: float
    images: list[ImageInfo]


class MoveImageRequest(BaseModel):
    image_index: int
    x: float
    y: float


class ResizeImageRequest(BaseModel):
    image_index: int
    x: float
    y: float
    width: float
    height: float


class DeleteImageRequest(BaseModel):
    image_index: int
