import hashlib
import re
import uuid
from pathlib import Path

import pymupdf

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

_DOC_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Map common PDF font names to Base14 equivalents
_FONT_MAP = {
    "helv": "helv",
    "helvetica": "helv",
    "arial": "helv",
    "tisa": "helv",
    "times": "tiro",
    "timesnewroman": "tiro",
    "times-roman": "tiro",
    "courier": "cour",
    "couriernew": "cour",
    "symbol": "symb",
    "zapfdingbats": "zadb",
}


def _validate_doc_id(doc_id: str) -> None:
    """Reject any doc_id that isn't exactly 16 hex chars (path traversal guard)."""
    if not _DOC_ID_RE.match(doc_id):
        raise ValueError(f"Invalid document id: {doc_id}")


def _normalize_font(font_name: str) -> str:
    """Map a PDF font name to the closest Base14 font."""
    key = font_name.lower().replace(" ", "").replace("-", "")
    # Strip subset prefix like "ABCDEF+"
    if "+" in key:
        key = key.split("+", 1)[1]
    for pattern, base14 in _FONT_MAP.items():
        if pattern in key:
            if "bold" in key and "italic" in key:
                return base14 + "bi"
            if "bold" in key:
                return base14 + "bo"
            if "italic" in key or "oblique" in key:
                return base14 + "it"
            return base14
    # Default fallback
    if "bold" in font_name.lower() and "italic" in font_name.lower():
        return "hebo"
    if "bold" in font_name.lower():
        return "hebo"
    if "italic" in font_name.lower():
        return "heit"
    return "helv"


def _int_to_hex_color(color_int: int) -> str:
    """Convert PyMuPDF integer color to hex string."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    """Convert hex color string to (r, g, b) floats 0-1."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r / 255.0, g / 255.0, b / 255.0)


def _find_span_by_index(page, span_index: int) -> dict | None:
    """Extract spans and return the one matching span_index."""
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    index = 0
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if not span["text"].strip():
                    continue
                if index == span_index:
                    return span
                index += 1
    return None


def save_upload(content: bytes, filename: str) -> tuple[str, int]:
    """Save uploaded PDF, return (doc_id, page_count)."""
    if len(content) > MAX_UPLOAD_SIZE:
        raise ValueError(f"File too large ({len(content)} bytes, max {MAX_UPLOAD_SIZE})")
    # Use hash + random suffix so re-uploading the same PDF doesn't overwrite edits
    content_hash = hashlib.sha256(content).hexdigest()[:12]
    unique_suffix = uuid.uuid4().hex[:4]
    doc_id = content_hash + unique_suffix
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    path.write_bytes(content)
    doc = pymupdf.open(str(path))
    try:
        page_count = len(doc)
    finally:
        doc.close()
    return doc_id, page_count


def _open_doc(doc_id: str) -> pymupdf.Document:
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return pymupdf.open(str(path))


def render_page(doc_id: str, page_num: int, scale: float = 2.0) -> bytes:
    """Render a page as PNG bytes."""
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        mat = pymupdf.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def extract_text_spans(doc_id: str, page_num: int) -> dict:
    """Extract all text spans with metadata from a page."""
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        page_rect = page.rect
        data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)

        spans = []
        index = 0
        for block in data["blocks"]:
            if block["type"] != 0:  # text blocks only
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"]
                    if not text.strip():
                        continue
                    spans.append({
                        "index": index,
                        "text": text,
                        "bbox": list(span["bbox"]),
                        "font": span["font"],
                        "size": round(span["size"], 2),
                        "color": _int_to_hex_color(span["color"]),
                        "flags": span["flags"],
                    })
                    index += 1

        return {
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
            "spans": spans,
        }
    finally:
        doc.close()


def edit_span(doc_id: str, page_num: int, span_index: int,
              new_text: str, font: str | None = None,
              size: float | None = None, color: str | None = None) -> None:
    """Edit a text span using redact + re-insert."""
    doc = _open_doc(doc_id)
    try:
        page = doc[page_num]
        target = _find_span_by_index(page, span_index)
        if target is None:
            raise IndexError(f"Span {span_index} not found")

        bbox = pymupdf.Rect(target["bbox"])
        orig_font = target["font"]
        orig_size = target["size"]
        orig_color = _int_to_hex_color(target["color"])

        use_font = _normalize_font(font if font else orig_font)
        use_size = size if size is not None else orig_size
        use_color = _hex_to_rgb(color if color else orig_color)

        # Redact the old text
        page.add_redact_annot(bbox)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        # Re-insert new text — expand bbox if new text doesn't fit
        if new_text:
            text_width = pymupdf.get_text_length(new_text, fontname=use_font, fontsize=use_size)
            # Ensure height fits at least one line
            min_height = use_size * 1.3
            if bbox.height < min_height:
                bbox.y1 = bbox.y0 + min_height
            if text_width > bbox.width:
                bbox.x1 = bbox.x0 + text_width + 2
                bbox.x1 = min(bbox.x1, page.rect.width - 5)

            rc = page.insert_textbox(
                bbox,
                new_text,
                fontname=use_font,
                fontsize=use_size,
                color=use_color,
                align=pymupdf.TEXT_ALIGN_LEFT,
            )
            # rc < 0 means text didn't fully fit — try again with taller box
            if rc < 0:
                lines_needed = (-rc) + 1
                bbox.y1 = bbox.y0 + use_size * 1.3 * lines_needed
                bbox.y1 = min(bbox.y1, page.rect.height - 5)
                page.insert_textbox(
                    bbox,
                    new_text,
                    fontname=use_font,
                    fontsize=use_size,
                    color=use_color,
                    align=pymupdf.TEXT_ALIGN_LEFT,
                )

        path = UPLOAD_DIR / f"{doc_id}.pdf"
        doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()


def add_text(doc_id: str, page_num: int, x: float, y: float,
             text: str, font: str = "helv", size: float = 12.0,
             color: str = "#000000") -> None:
    """Add new text at the given coordinates."""
    doc = _open_doc(doc_id)
    try:
        page = doc[page_num]
        use_font = _normalize_font(font)
        use_color = _hex_to_rgb(color)

        # Size the box to fit the text
        text_width = pymupdf.get_text_length(text, fontname=use_font, fontsize=size)
        right = min(x + text_width + 10, page.rect.width - 5)
        rect = pymupdf.Rect(x, y, right, y + size * 1.5)

        page.insert_textbox(
            rect,
            text,
            fontname=use_font,
            fontsize=size,
            color=use_color,
            align=pymupdf.TEXT_ALIGN_LEFT,
        )

        path = UPLOAD_DIR / f"{doc_id}.pdf"
        doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()


def get_pdf_bytes(doc_id: str) -> bytes:
    """Return the PDF file bytes for download."""
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return path.read_bytes()
