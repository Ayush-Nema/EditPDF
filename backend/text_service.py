"""Text extraction, editing, bullet detection, and font/color helpers."""

import re

import pymupdf

from .config import (
    DEFAULT_FONT,
    DEFAULT_FONT_SIZE,
    DEFAULT_TEXT_COLOR,
    FONT_MAP,
    IMAGE_PADDING,
    LINE_HEIGHT_FACTOR,
    PAGE_MARGIN,
    SYMBOL_FONT_HINTS,
    TEXT_BOX_HEIGHT_FACTOR,
    TEXT_WIDTH_PADDING,
    UPLOAD_DIR,
)
from .document import _open_doc
from .history import _snapshot


def _normalize_font(font_name: str) -> str:
    """Map a PDF font name to the closest Base14 font."""
    key = font_name.lower().replace(" ", "").replace("-", "")
    # Strip subset prefix like "ABCDEF+"
    if "+" in key:
        key = key.split("+", 1)[1]
    for pattern, base14 in FONT_MAP.items():
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


# Bullet markers: Unicode bullets, Private Use Area (common in PDF symbol
# fonts), and ASCII markers that require a trailing space.
_BULLET_RE = re.compile(
    r"^\s*(?:[\u2022\u2023\u25E6\u2043\u2219\u00B7\u25AA\u25B8\u25BA\u25CB\u25CF]"
    r"|[\uE000-\uF8FF]"  # Private Use Area (PDF symbol fonts)
    r"|[\u2013\u2014\-\*]\s"  # dashes / asterisk + space
    r"|\d+[.\)]\s"  # numbered lists
    r"|[a-zA-Z][.\)]\s)"  # lettered lists
)


def _line_is_bullet(line) -> bool:
    """Check if a PyMuPDF text-dict line starts with a bullet marker."""
    for span in line["spans"]:
        text = span["text"]
        if not text.strip():
            continue
        # First non-empty span in a symbol font → bullet
        font_lower = span["font"].lower().replace(" ", "")
        if any(hint in font_lower for hint in SYMBOL_FONT_HINTS):
            return True
        # Text-content check
        return bool(_BULLET_RE.match(text))
    return False


def _collect_block_lines(block):
    """Return [(line_text, line_bbox, is_bullet), ...] and first non-empty span."""
    lines = []
    first_span = None
    for line in block["lines"]:
        parts = []
        for span in line["spans"]:
            if not span["text"].strip():
                continue
            if first_span is None:
                first_span = span
            parts.append(span["text"])
        text = "".join(parts)
        if text.strip():
            lines.append((text, line["bbox"], _line_is_bullet(line)))
    return lines, first_span


def _union_bbox(bboxes):
    """Compute the bounding box that encloses all given bboxes."""
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _split_block(block):
    """Split a text block into logical items.

    Paragraphs stay as one item; bullet lists are split so each bullet
    point (possibly multi-line) becomes its own item.

    Yields (text, bbox, first_span) tuples.
    """
    lines, first_span = _collect_block_lines(block)
    if not lines or first_span is None:
        return

    has_bullets = any(is_b for _, _, is_b in lines)
    if not has_bullets:
        yield "\n".join(t for t, _, _ in lines), block["bbox"], first_span
        return

    # Group lines into bullet items; a new bullet starts a new group,
    # non-bullet lines are continuations of the previous group.
    current: list[tuple[str, tuple]] = []
    for text, bbox, is_bullet in lines:
        if is_bullet and current:
            joined = "\n".join(t for t, _ in current)
            yield joined, _union_bbox([b for _, b in current]), first_span
            current = [(text, bbox)]
        else:
            current.append((text, bbox))
    if current:
        joined = "\n".join(t for t, _ in current)
        yield joined, _union_bbox([b for _, b in current]), first_span


def _find_span_by_index(page, span_index: int) -> dict | None:
    """Find the text item matching span_index and return aggregated info."""
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    index = 0
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for _text, bbox, first_span in _split_block(block):
            if index == span_index:
                return {
                    "bbox": bbox,
                    "font": first_span["font"],
                    "size": first_span["size"],
                    "color": first_span["color"],
                }
            index += 1
    return None


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
            for text, bbox, first_span in _split_block(block):
                spans.append(
                    {
                        "index": index,
                        "text": text,
                        "bbox": list(bbox),
                        "font": first_span["font"],
                        "size": round(first_span["size"], 2),
                        "color": _int_to_hex_color(first_span["color"]),
                        "flags": first_span["flags"],
                    }
                )
                index += 1

        return {
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
            "spans": spans,
        }
    finally:
        doc.close()


def edit_span(
    doc_id: str,
    page_num: int,
    span_index: int,
    new_text: str,
    font: str | None = None,
    size: float | None = None,
    color: str | None = None,
) -> None:
    """Edit a text span using redact + re-insert."""
    _snapshot(doc_id)
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
            min_height = use_size * LINE_HEIGHT_FACTOR
            if bbox.height < min_height:
                bbox.y1 = bbox.y0 + min_height
            if text_width > bbox.width:
                bbox.x1 = bbox.x0 + text_width + TEXT_WIDTH_PADDING
                bbox.x1 = min(bbox.x1, page.rect.width - PAGE_MARGIN)

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
                bbox.y1 = bbox.y0 + use_size * LINE_HEIGHT_FACTOR * lines_needed
                bbox.y1 = min(bbox.y1, page.rect.height - PAGE_MARGIN)
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


def add_text(
    doc_id: str,
    page_num: int,
    x: float,
    y: float,
    text: str,
    font: str = DEFAULT_FONT,
    size: float = DEFAULT_FONT_SIZE,
    color: str = DEFAULT_TEXT_COLOR,
) -> None:
    """Add new text at the given coordinates."""
    _snapshot(doc_id)
    doc = _open_doc(doc_id)
    try:
        page = doc[page_num]
        use_font = _normalize_font(font)
        use_color = _hex_to_rgb(color)

        # Size the box to fit the text
        text_width = pymupdf.get_text_length(text, fontname=use_font, fontsize=size)
        right = min(x + text_width + IMAGE_PADDING, page.rect.width - PAGE_MARGIN)
        rect = pymupdf.Rect(x, y, right, y + size * TEXT_BOX_HEIGHT_FACTOR)

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
