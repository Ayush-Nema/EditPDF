import hashlib
import re
import uuid
from pathlib import Path

import pymupdf

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

# Undo/redo snapshot stacks (in-memory, per document)
_undo_stacks: dict[str, list[bytes]] = {}
_redo_stacks: dict[str, list[bytes]] = {}
MAX_UNDO = 20


def _snapshot(doc_id: str) -> None:
    """Save current PDF bytes to the undo stack before a mutation."""
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        return
    pdf_bytes = path.read_bytes()
    stack = _undo_stacks.setdefault(doc_id, [])
    stack.append(pdf_bytes)
    if len(stack) > MAX_UNDO:
        stack.pop(0)
    # Any new mutation clears the redo stack
    _redo_stacks.pop(doc_id, None)


def undo(doc_id: str) -> bool:
    """Restore the previous PDF snapshot. Returns True if undo was performed."""
    _validate_doc_id(doc_id)
    stack = _undo_stacks.get(doc_id)
    if not stack:
        return False
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    # Push current state to redo stack
    redo_stack = _redo_stacks.setdefault(doc_id, [])
    redo_stack.append(path.read_bytes())
    # Restore previous state
    path.write_bytes(stack.pop())
    return True


def redo(doc_id: str) -> bool:
    """Re-apply the last undone operation. Returns True if redo was performed."""
    _validate_doc_id(doc_id)
    stack = _redo_stacks.get(doc_id)
    if not stack:
        return False
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    # Push current state to undo stack
    undo_stack = _undo_stacks.setdefault(doc_id, [])
    undo_stack.append(path.read_bytes())
    # Restore redo state
    path.write_bytes(stack.pop())
    return True

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


# Bullet markers: Unicode bullets, Private Use Area (common in PDF symbol
# fonts), and ASCII markers that require a trailing space.
_BULLET_RE = re.compile(
    r"^\s*(?:[\u2022\u2023\u25E6\u2043\u2219\u00B7\u25AA\u25B8\u25BA\u25CB\u25CF]"
    r"|[\uE000-\uF8FF]"                       # Private Use Area (PDF symbol fonts)
    r"|[\u2013\u2014\-\*]\s"                   # dashes / asterisk + space
    r"|\d+[.\)]\s"                             # numbered lists
    r"|[a-zA-Z][.\)]\s)"                       # lettered lists
)

# Font families that are almost always decorative bullet/symbol glyphs.
_SYMBOL_FONT_HINTS = {"symbol", "zapf", "dingbat", "wingding", "webding", "bullet"}


def _line_is_bullet(line) -> bool:
    """Check if a PyMuPDF text-dict line starts with a bullet marker."""
    for span in line["spans"]:
        text = span["text"]
        if not text.strip():
            continue
        # First non-empty span in a symbol font → bullet
        font_lower = span["font"].lower().replace(" ", "")
        if any(hint in font_lower for hint in _SYMBOL_FONT_HINTS):
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
        max(b[3] for b in bboxes)
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
            for text, bbox, first_span in _split_block(block):
                spans.append({
                    "index": index,
                    "text": text,
                    "bbox": list(bbox),
                    "font": first_span["font"],
                    "size": round(first_span["size"], 2),
                    "color": _int_to_hex_color(first_span["color"]),
                    "flags": first_span["flags"],
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
    _snapshot(doc_id)
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


def add_image(doc_id: str, page_num: int, x: float, y: float,
              image_bytes: bytes, width: float = 0, height: float = 0) -> None:
    """Insert an image at the given coordinates on a page."""
    _snapshot(doc_id)
    doc = _open_doc(doc_id)
    try:
        page = doc[page_num]

        # Determine image dimensions from the image itself if not specified
        if width <= 0 or height <= 0:
            tmp_pix = pymupdf.Pixmap(image_bytes)
            img_w, img_h = tmp_pix.width, tmp_pix.height
            tmp_pix = None
            # Scale down to fit on page if needed, default to 200px wide
            scale = min(200 / img_w, (page.rect.width - x - 10) / img_w)
            if width <= 0:
                width = img_w * scale
            if height <= 0:
                height = img_h * scale

        # Clamp to page bounds
        x1 = min(x + width, page.rect.width - 5)
        y1 = min(y + height, page.rect.height - 5)
        rect = pymupdf.Rect(x, y, x1, y1)

        page.insert_image(rect, stream=image_bytes)

        path = UPLOAD_DIR / f"{doc_id}.pdf"
        doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()


def _save_full(doc, doc_id: str) -> None:
    """Non-incremental save with garbage collection (required after redaction).

    PyMuPDF won't allow non-incremental save to the same open file,
    so we serialize to bytes, close the doc, and overwrite the file.
    """
    pdf_bytes = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    path.write_bytes(pdf_bytes)


def extract_images(doc_id: str, page_num: int) -> dict:
    """List all image placements on a page."""
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        page_rect = page.rect

        images = []
        index = 0
        for img in page.get_images(full=True):
            xref = img[0]
            # Skip transparent-pixel placeholders left by delete_image()
            if img[2] <= 1 and img[3] <= 1:
                continue
            rects = page.get_image_rects(xref)
            for rect in rects:
                images.append({
                    "index": index,
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "width": rect.width,
                    "height": rect.height,
                    "xref": xref,
                })
                index += 1

        return {
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
            "images": images,
        }
    finally:
        doc.close()


def _find_image_by_index(page, image_index: int):
    """Resolve an image index to (xref, Rect)."""
    index = 0
    for img in page.get_images(full=True):
        xref = img[0]
        # Skip transparent-pixel placeholders left by delete_image()
        if img[2] <= 1 and img[3] <= 1:
            continue
        rects = page.get_image_rects(xref)
        for rect in rects:
            if index == image_index:
                return xref, rect
            index += 1
    return None


def delete_image(doc_id: str, page_num: int, image_index: int) -> None:
    """Delete an image without affecting surrounding text."""
    _snapshot(doc_id)
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        result = _find_image_by_index(page, image_index)
        if result is None:
            raise IndexError(f"Image {image_index} not found")

        xref, _rect = result
        page.delete_image(xref)
        _save_full(doc, doc_id)
    finally:
        if not doc.is_closed:
            doc.close()


def move_image(doc_id: str, page_num: int, image_index: int,
               new_x: float, new_y: float) -> None:
    """Move an image to new coordinates, preserving original dimensions."""
    _snapshot(doc_id)
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        result = _find_image_by_index(page, image_index)
        if result is None:
            raise IndexError(f"Image {image_index} not found")

        xref, old_rect = result
        img_data = doc.extract_image(xref)
        img_bytes = img_data["image"]

        # Preserve original dimensions
        w = old_rect.width
        h = old_rect.height

        # Clamp to page bounds
        page_rect = page.rect
        new_x = max(0, min(new_x, page_rect.width - w))
        new_y = max(0, min(new_y, page_rect.height - h))
        new_rect = pymupdf.Rect(new_x, new_y, new_x + w, new_y + h)

        # Remove old image (replaces with transparent pixel, preserves text)
        page.delete_image(xref)

        # Insert at new position
        page.insert_image(new_rect, stream=img_bytes)
        _save_full(doc, doc_id)
    finally:
        if not doc.is_closed:
            doc.close()


def resize_image(doc_id: str, page_num: int, image_index: int,
                 new_x: float, new_y: float,
                 new_w: float, new_h: float) -> None:
    """Resize and reposition an image."""
    _snapshot(doc_id)
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        result = _find_image_by_index(page, image_index)
        if result is None:
            raise IndexError(f"Image {image_index} not found")

        xref, old_rect = result
        img_data = doc.extract_image(xref)
        img_bytes = img_data["image"]

        # Enforce minimum size
        new_w = max(10, new_w)
        new_h = max(10, new_h)

        # Clamp to page bounds
        page_rect = page.rect
        new_x = max(0, min(new_x, page_rect.width - new_w))
        new_y = max(0, min(new_y, page_rect.height - new_h))
        new_rect = pymupdf.Rect(new_x, new_y, new_x + new_w, new_y + new_h)

        # Remove old image (replaces with transparent pixel, preserves text)
        page.delete_image(xref)

        # Insert at new size/position
        page.insert_image(new_rect, stream=img_bytes)
        _save_full(doc, doc_id)
    finally:
        if not doc.is_closed:
            doc.close()


def get_pdf_bytes(doc_id: str) -> bytes:
    """Return the PDF file bytes for download."""
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return path.read_bytes()
