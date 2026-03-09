"""Text extraction, editing, bullet detection, and font/color helpers."""

import logging
import os
import re
import urllib.request

import pymupdf

from .config import (
    DEFAULT_FONT,
    DEFAULT_FONT_SIZE,
    DEFAULT_TEXT_COLOR,
    FONT_CACHE_DIR,
    FONT_FAMILY_MAP,
    FONT_MAP,
    GOOGLE_FONTS_MAP,
    GOOGLE_FONTS_TIMEOUT,
    IMAGE_PADDING,
    LIBERATION_FONT_DIR,
    LIBERATION_MAP,
    LINE_HEIGHT_FACTOR,
    PAGE_MARGIN,
    SYMBOL_FONT_HINTS,
    TEXT_BOX_HEIGHT_FACTOR,
    TEXT_WIDTH_PADDING,
    UPLOAD_DIR,
)
from .document import _open_doc
from .history import _snapshot

logger = logging.getLogger(__name__)

_BASE14_STYLES: dict[str, dict[str, str]] = {
    "helv": {"": "helv", "bold": "hebo", "italic": "heit", "bolditalic": "hebi"},
    "tiro": {"": "tiro", "bold": "tibo", "italic": "tiit", "bolditalic": "tibi"},
    "cour": {"": "cour", "bold": "cobo", "italic": "coit", "bolditalic": "cobi"},
}


def _normalize_font(font_name: str, span_flags: int = 0) -> str:
    """Map a PDF font name to the closest Base14 font.

    *span_flags* is the PyMuPDF span flags bitmask (bit 4 = bold, bit 1 = italic).
    Used as a fallback when the font name doesn't contain style keywords.
    """
    key = font_name.lower().replace(" ", "").replace("-", "")
    # Strip subset prefix like "ABCDEF+"
    if "+" in key:
        key = key.split("+", 1)[1]

    # Detect style from name, falling back to span flags
    has_bold = "bold" in key or bool(span_flags & 16)
    has_italic = "italic" in key or "oblique" in key or bool(span_flags & 2)
    if has_bold and has_italic:
        style = "bolditalic"
    elif has_bold:
        style = "bold"
    elif has_italic:
        style = "italic"
    else:
        style = ""

    for pattern, base14 in FONT_MAP.items():
        if pattern in key:
            variants = _BASE14_STYLES.get(base14, {})
            return variants.get(style, base14)

    # Default fallback → Helvetica family
    return _BASE14_STYLES["helv"].get(style, "helv")


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
    """Return [(line_text, line_bbox, is_bullet, line_first_span), ...] and block first span."""
    raw_lines = []
    block_first_span = None
    for line in block["lines"]:
        parts = []
        line_first_span = None
        for span in line["spans"]:
            if line_first_span is None and span["text"].strip():
                line_first_span = span
            if block_first_span is None and span["text"].strip():
                block_first_span = span
            parts.append(span["text"])
        text = "".join(parts)
        if text.strip():
            raw_lines.append((text, line["bbox"], _line_is_bullet(line), line_first_span))

    # Merge lines that sit on the same visual row (same y-range).
    # PyMuPDF sometimes splits "2 Background" into two "lines" at identical y.
    if len(raw_lines) <= 1:
        return raw_lines, block_first_span

    lines = [raw_lines[0]]
    for text, bbox, is_bullet, first_span in raw_lines[1:]:
        prev_text, prev_bbox, prev_bullet, prev_span = lines[-1]
        prev_h = prev_bbox[3] - prev_bbox[1]
        curr_h = bbox[3] - bbox[1]
        min_h = min(prev_h, curr_h)
        y_overlap = min(prev_bbox[3], bbox[3]) - max(prev_bbox[1], bbox[1])
        if min_h > 0 and y_overlap > 0.5 * min_h:
            # Same visual row — but check horizontal gap first.
            # Don't merge items that are far apart (e.g. left-aligned label
            # and right-aligned value with whitespace in between).
            h_gap = bbox[0] - prev_bbox[2]
            font_size = prev_span.get("size", 12) if prev_span else 12
            if h_gap > font_size * 3:
                lines.append((text, bbox, is_bullet, first_span))
                continue
            # Close enough — merge with a space separator
            sep = "" if prev_text.endswith(" ") or text.startswith(" ") else " "
            merged_bbox = (
                min(prev_bbox[0], bbox[0]),
                min(prev_bbox[1], bbox[1]),
                max(prev_bbox[2], bbox[2]),
                max(prev_bbox[3], bbox[3]),
            )
            lines[-1] = (prev_text + sep + text, merged_bbox, prev_bullet, prev_span)
        else:
            lines.append((text, bbox, is_bullet, first_span))

    return lines, block_first_span


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
    point (possibly multi-line) becomes its own item.  Lines separated
    by a vertical gap larger than the font size are treated as separate
    items (handles independently-added texts grouped into one block).

    Yields (text, bbox, first_span) tuples.  Each item gets the first_span
    from its own lines, not from the block as a whole.
    """
    lines, _block_first_span = _collect_block_lines(block)
    if not lines or _block_first_span is None:
        return

    has_bullets = any(is_b for _, _, is_b, _ in lines)

    if not has_bullets:
        # Split on large vertical gaps OR large horizontal gaps between
        # consecutive lines.  The horizontal check catches items that sit
        # on the same y-row but are far apart (e.g. a left label and a
        # right-aligned value).
        groups: list[list[tuple[str, tuple, dict]]] = [[(lines[0][0], lines[0][1], lines[0][3])]]
        for i in range(1, len(lines)):
            prev_bbox = lines[i - 1][1]
            cur_bbox = lines[i][1]
            line_height = prev_bbox[3] - prev_bbox[1]
            gap = cur_bbox[1] - prev_bbox[3]
            # Vertical gap split
            if gap > line_height:
                groups.append([])
            else:
                # Horizontal gap split (same y-row, far apart)
                h_gap = cur_bbox[0] - prev_bbox[2]
                font_size = lines[i - 1][3].get("size", 12) if lines[i - 1][3] else 12
                if h_gap > font_size * 3:
                    groups.append([])
            groups[-1].append((lines[i][0], lines[i][1], lines[i][3]))

        for group in groups:
            text = "\n".join(t for t, _, _ in group)
            bbox = _union_bbox([b for _, b, _ in group])
            group_span = group[0][2]  # first span of this group
            yield text, bbox, group_span
        return

    # Group lines into bullet items; a new bullet starts a new group,
    # non-bullet lines are continuations of the previous group.
    current: list[tuple[str, tuple, dict]] = []
    for text, bbox, is_bullet, line_span in lines:
        if is_bullet and current:
            joined = "\n".join(t for t, _, _ in current)
            yield joined, _union_bbox([b for _, b, _ in current]), current[0][2]
            current = [(text, bbox, line_span)]
        else:
            current.append((text, bbox, line_span))
    if current:
        joined = "\n".join(t for t, _, _ in current)
        yield joined, _union_bbox([b for _, b, _ in current]), current[0][2]


def _collect_page_items(page):
    """Collect all text items from a page, merging same-line fragments.

    PyMuPDF may put horizontally-adjacent text in separate blocks (e.g.
    "2" and "Background" from one heading).  This function merges items
    whose bounding boxes overlap vertically and are close horizontally.

    Returns a list of ``(text, bbox_tuple, first_span)`` tuples in
    reading order (top-to-bottom, left-to-right).
    """
    data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)

    # First pass: collect raw items from all blocks.
    raw: list[tuple[str, list, dict]] = []
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for text, bbox, first_span in _split_block(block):
            raw.append((text, list(bbox), first_span))

    if not raw:
        return []

    # Sort into reading order (y then x).
    raw.sort(key=lambda r: (r[1][1], r[1][0]))

    # Merge items that sit on the same horizontal line.
    merged: list[tuple[str, list, dict]] = [raw[0]]
    for text, bbox, first_span in raw[1:]:
        prev_text, prev_bbox, prev_span = merged[-1]

        # Vertical overlap check — are the two items on the same line?
        prev_h = prev_bbox[3] - prev_bbox[1]
        curr_h = bbox[3] - bbox[1]
        y_overlap = min(prev_bbox[3], bbox[3]) - max(prev_bbox[1], bbox[1])
        min_h = min(prev_h, curr_h)
        same_line = min_h > 0 and y_overlap > 0.5 * min_h

        if same_line:
            # Horizontal gap check — items must proceed left-to-right
            # (h_gap >= 0) and be close enough (within one font-size) to
            # belong to the same phrase.  This prevents merging across
            # columns in multi-column layouts.
            h_gap = bbox[0] - prev_bbox[2]
            max_gap = max(prev_span.get("size", 12), first_span.get("size", 12))
            if 0 <= h_gap < max_gap:
                sep = "" if prev_text.endswith(" ") or text.startswith(" ") else " "
                merged[-1] = (
                    prev_text + sep + text,
                    [
                        min(prev_bbox[0], bbox[0]),
                        min(prev_bbox[1], bbox[1]),
                        max(prev_bbox[2], bbox[2]),
                        max(prev_bbox[3], bbox[3]),
                    ],
                    prev_span,
                )
                continue

        merged.append((text, bbox, first_span))

    return [(t, tuple(b), s) for t, b, s in merged]


def _find_span_by_index(page, span_index: int) -> dict | None:
    """Find the text item matching span_index and return aggregated info."""
    items = _collect_page_items(page)
    if span_index < 0 or span_index >= len(items):
        return None
    text, bbox, first_span = items[span_index]
    return {
        "bbox": bbox,
        "font": first_span["font"],
        "size": first_span["size"],
        "color": first_span["color"],
        "flags": first_span.get("flags", 0),
        "text": text,
    }


def extract_text_spans(doc_id: str, page_num: int) -> dict:
    """Extract all text spans with metadata from a page."""
    doc = _open_doc(doc_id)
    try:
        if page_num < 0 or page_num >= len(doc):
            raise IndexError(f"Page {page_num} out of range")
        page = doc[page_num]
        page_rect = page.rect

        spans = []
        for index, (text, bbox, first_span) in enumerate(_collect_page_items(page)):
            spans.append(
                {
                    "index": index,
                    "text": text,
                    "bbox": list(bbox),
                    "font": first_span["font"],
                    "normalized_font": _normalize_font(first_span["font"]),
                    "size": round(first_span["size"], 2),
                    "color": _int_to_hex_color(first_span["color"]),
                    "flags": first_span["flags"],
                }
            )

        return {
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
            "spans": spans,
        }
    finally:
        doc.close()


def _font_core_name(name: str) -> str:
    """Reduce a font name to its core for fuzzy matching.

    Strips subset prefixes, PostScript suffixes, style words, and
    normalises whitespace/punctuation so that names like ``ArialMT``,
    ``Arial Regular``, and ``ABCDEF+Arial-BoldMT`` all compare correctly.
    """
    if "+" in name:
        name = name.split("+", 1)[1]
    key = name.lower().replace(" ", "").replace("-", "").replace(",", "")
    # Strip common PostScript / style suffixes (may appear at end or before
    # style keywords like "bold"/"italic").
    for suffix in ("psmt", "mt"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    # Remove "ps" tag that can appear mid-name (e.g. "TimesNewRomanPS-Bold")
    key = re.sub(r"ps(?=bold|italic|$)", "", key)
    # Strip trailing "regular" / "roman" style names
    for suffix in ("regular", "roman"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


def _extract_page_font(doc, page, font_name: str):
    """Extract a font matching *font_name* from the page.

    Returns a ``pymupdf.Font`` object built from the embedded font program,
    or ``None`` if the font cannot be safely extracted.

    We only reject Type3 fonts (synthetic/bitmap, no TrueType data).
    Subset and CID/Type0 fonts are allowed — modern PDF generators
    (Word, Chrome, etc.) produce well-formed subsets whose TrueType
    cmap tables correctly map Unicode codepoints to glyphs.  The caller
    still validates via ``_font_covers_text`` before using the font.
    """
    target_core = _font_core_name(font_name)

    for xref, _ext, ftype, basefont, _name, _enc in page.get_fonts():
        if xref == 0:
            continue
        if _font_core_name(basefont) != target_core:
            continue

        # Type3 fonts are synthetic/bitmap — no usable TrueType data
        if ftype == "Type3":
            return None

        try:
            _basename, ext, _subtype, content = doc.extract_font(xref)
            if not content or ext == "n/a":
                continue
            return pymupdf.Font(fontbuffer=content)
        except Exception:
            continue
    return None


def _font_covers_text(font_obj, text: str) -> bool:
    """Return True if *font_obj* has glyphs for ALL characters in *text*.

    Subset fonts only contain glyphs for the characters originally used in the
    document.  If even one glyph is missing, TextWriter will produce a .notdef
    placeholder or map to the wrong glyph, so we require full coverage.
    """
    unique = {ch for ch in text if ch.isprintable() and not ch.isspace()}
    if not unique:
        return True
    return all(font_obj.has_glyph(ord(ch)) for ch in unique)


def _insert_with_extracted_font(page, bbox, text, font_obj, size, color) -> bool:
    """Insert *text* using a ``pymupdf.Font`` via ``TextWriter``.

    For single-line text, uses ``TextWriter.append`` (no word-wrapping) so the
    text is placed at the exact baseline position.  For multi-line text, uses
    ``TextWriter.fill_textbox`` with generous sizing.

    Returns ``True`` on success.
    """
    try:
        tw = pymupdf.TextWriter(page.rect)

        if "\n" not in text:
            # Single-line: place at the original baseline position directly.
            # The bbox top (y0) is the top of ascenders; the baseline sits at
            # y0 + ascender * fontsize.
            baseline_y = bbox.y0 + font_obj.ascender * size
            tw.append((bbox.x0, baseline_y), text, font=font_obj, fontsize=size)
        else:
            # Multi-line: use fill_textbox with generous rect sizing
            insert_rect = pymupdf.Rect(bbox)
            lines = text.count("\n") + 1
            needed_height = size * LINE_HEIGHT_FACTOR * lines
            if insert_rect.height < needed_height:
                insert_rect.y1 = insert_rect.y0 + needed_height
                insert_rect.y1 = min(insert_rect.y1, page.rect.height - PAGE_MARGIN)
            # Generous width to prevent unwanted word-wrapping
            max_line_width = max(font_obj.text_length(ln, fontsize=size) for ln in text.split("\n"))
            if max_line_width > insert_rect.width:
                insert_rect.x1 = insert_rect.x0 + max_line_width + TEXT_WIDTH_PADDING
                insert_rect.x1 = min(insert_rect.x1, page.rect.width - PAGE_MARGIN)
            tw.fill_textbox(
                insert_rect,
                text,
                font=font_obj,
                fontsize=size,
                align=pymupdf.TEXT_ALIGN_LEFT,
            )

        tw.write_text(page, color=color)
        return True
    except Exception:
        return False


def _insert_with_base14(page, bbox, text, fontname, size, color):
    """Insert *text* using a Base14 font via ``insert_textbox``."""
    insert_rect = pymupdf.Rect(bbox)
    text_width = pymupdf.get_text_length(text, fontname=fontname, fontsize=size)

    min_height = size * LINE_HEIGHT_FACTOR
    if insert_rect.height < min_height:
        insert_rect.y1 = insert_rect.y0 + min_height
    if text_width > insert_rect.width:
        insert_rect.x1 = insert_rect.x0 + text_width + TEXT_WIDTH_PADDING
        insert_rect.x1 = min(insert_rect.x1, page.rect.width - PAGE_MARGIN)

    rc = page.insert_textbox(
        insert_rect,
        text,
        fontname=fontname,
        fontsize=size,
        color=color,
        align=pymupdf.TEXT_ALIGN_LEFT,
    )
    if rc < 0:
        lines_needed = (-rc) + 1
        insert_rect.y1 = insert_rect.y0 + size * LINE_HEIGHT_FACTOR * lines_needed
        insert_rect.y1 = min(insert_rect.y1, page.rect.height - PAGE_MARGIN)
        page.insert_textbox(
            insert_rect,
            text,
            fontname=fontname,
            fontsize=size,
            color=color,
            align=pymupdf.TEXT_ALIGN_LEFT,
        )


def _load_liberation_font(font_name: str, span_flags: int = 0) -> pymupdf.Font | None:
    """Load a Liberation font as a close visual match for *font_name*.

    *span_flags* is the PyMuPDF span flags bitmask (bit 4 = bold, bit 1 = italic).
    Used as a fallback when the font name doesn't contain style keywords.

    Returns ``None`` if the Liberation fonts are not installed (e.g. local dev
    without Docker) or if the font file is missing.
    """
    if not LIBERATION_FONT_DIR.is_dir():
        return None

    # Strip subset prefix (e.g. "ABCDEF+Arial-BoldItalic" → "Arial-BoldItalic")
    name = font_name
    if "+" in name:
        name = name.split("+", 1)[1]
    lower = name.lower().replace(" ", "").replace("-", "")

    # Detect style from font name, falling back to span flags
    has_bold = "bold" in lower or bool(span_flags & 16)
    has_italic = "italic" in lower or "oblique" in lower or bool(span_flags & 2)
    if has_bold and has_italic:
        style = "bolditalic"
    elif has_bold:
        style = "bold"
    elif has_italic:
        style = "italic"
    else:
        style = ""

    # Classify into sans/serif/mono
    family = "sans"  # default
    for pattern, fam in FONT_FAMILY_MAP.items():
        if pattern in lower:
            family = fam
            break

    key = (family, style)
    filename = LIBERATION_MAP.get(key)
    if filename is None:
        return None

    path = LIBERATION_FONT_DIR / filename
    if not path.is_file():
        return None

    try:
        return pymupdf.Font(fontfile=str(path))
    except Exception:
        return None


def _download_google_font(font_name: str, span_flags: int = 0) -> pymupdf.Font | None:
    """Download a metric-compatible Google Font as a fallback for *font_name*.

    Uses the Croscore/Crosextra families (Arimo, Tinos, Cousine, Carlito,
    Caladea) which are designed as metric-compatible replacements for common
    Windows/PDF fonts.  Downloads are cached in ``FONT_CACHE_DIR``.

    *span_flags* is the PyMuPDF span flags bitmask (bit 4 = bold, bit 1 = italic).
    Used as a fallback when the font name doesn't contain style keywords.

    Returns a ``pymupdf.Font`` on success, or ``None`` on any failure
    (no network, timeout, unknown font family, etc.).
    """
    try:
        # Strip subset prefix
        name = font_name
        if "+" in name:
            name = name.split("+", 1)[1]
        lower = name.lower().replace(" ", "").replace("-", "").replace(",", "")

        # Detect bold/italic from name, falling back to span flags
        has_bold = "bold" in lower or bool(span_flags & 16)
        has_italic = "italic" in lower or "oblique" in lower or bool(span_flags & 2)

        # Get core name (strip PS suffixes + style words)
        core = _font_core_name(font_name)
        for style_word in ("bold", "italic", "oblique"):
            core = core.replace(style_word, "")

        # Look up in Google Fonts map, default to Arimo (wide Unicode coverage)
        family = None
        for pattern, fam in GOOGLE_FONTS_MAP.items():
            if pattern in core:
                family = fam
                break
        if family is None:
            family = "Arimo"

        # Compute weight and italic flag
        weight = 700 if has_bold else 400
        italic = 1 if has_italic else 0

        # Cache path
        FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = FONT_CACHE_DIR / f"{family}-{weight}-{italic}.ttf"

        # Return cached font if available
        if cache_file.is_file():
            return pymupdf.Font(fontbuffer=cache_file.read_bytes())

        # Fetch CSS from Google Fonts (User-Agent triggers TTF format)
        css_url = (
            f"https://fonts.googleapis.com/css2"
            f"?family={family.replace(' ', '+')}:ital,wght@{italic},{weight}"
        )
        req = urllib.request.Request(css_url, headers={"User-Agent": "Python-urllib/3.0"})
        with urllib.request.urlopen(req, timeout=GOOGLE_FONTS_TIMEOUT) as resp:
            css = resp.read().decode("utf-8")

        # Parse TTF URL from CSS
        ttf_match = re.search(r"url\((https://[^)]+\.ttf)\)", css)
        if not ttf_match:
            logger.debug("No TTF URL found in Google Fonts CSS for %s", family)
            return None
        ttf_url = ttf_match.group(1)

        # Download TTF data
        req = urllib.request.Request(ttf_url, headers={"User-Agent": "Python-urllib/3.0"})
        with urllib.request.urlopen(req, timeout=GOOGLE_FONTS_TIMEOUT) as resp:
            data = resp.read()

        # Atomic write to cache
        tmp_path = cache_file.with_suffix(".tmp")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, cache_file)

        return pymupdf.Font(fontbuffer=data)
    except Exception:
        logger.debug("Google Font download failed for %s", font_name, exc_info=True)
        return None


def edit_span(
    doc_id: str,
    page_num: int,
    span_index: int,
    new_text: str,
    font: str | None = None,
    size: float | None = None,
    color: str | None = None,
) -> None:
    """Edit a text span, preserving the original font when possible.

    Strategy (tried in order):
      1. Direct content-stream edit — best fidelity, text-only changes.
      2. Redact + reinsert with the *original* font extracted from the PDF.
      2.5. Redact + reinsert with a Liberation font (Docker only).
      3. Redact + reinsert with a Google Font download (cached).
      4. Redact + reinsert with a Base14 substitute (last resort).
    """
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
        orig_flags = target.get("flags", 0)

        use_size = size if size is not None else orig_size
        use_color = _hex_to_rgb(color if color else orig_color)

        # --- Attempt 1: direct stream edit (text-only changes) ---
        text_only = font is None and size is None and color is None
        if text_only and new_text:
            from .stream_editor import try_direct_edit

            if try_direct_edit(doc, page, page_num, target["text"], new_text):
                path = UPLOAD_DIR / f"{doc_id}.pdf"
                doc.save(
                    str(path),
                    incremental=True,
                    encryption=pymupdf.PDF_ENCRYPT_KEEP,
                )
                return

        # --- Attempt 2 & 3: redact + reinsert ---
        # Extract the original font *before* redacting (font data stays in
        # the document either way, but this keeps intent clear).
        original_font_obj = None
        if font is None:
            original_font_obj = _extract_page_font(doc, page, orig_font)
            # Only use it if the new text's glyphs are all present
            if original_font_obj and not _font_covers_text(original_font_obj, new_text):
                original_font_obj = None

        # Redact the old text
        page.add_redact_annot(bbox)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        if new_text:
            inserted = False

            # Attempt 2: reinsert with the extracted original font
            if original_font_obj is not None:
                inserted = _insert_with_extracted_font(
                    page,
                    bbox,
                    new_text,
                    original_font_obj,
                    use_size,
                    use_color,
                )

            # Attempt 2.5: Liberation font fallback
            if not inserted:
                lib_font = _load_liberation_font(orig_font, span_flags=orig_flags)
                if lib_font and _font_covers_text(lib_font, new_text):
                    inserted = _insert_with_extracted_font(
                        page, bbox, new_text, lib_font, use_size, use_color
                    )

            # Attempt 3: Google Fonts download
            if not inserted:
                gf = _download_google_font(orig_font, span_flags=orig_flags)
                if gf and _font_covers_text(gf, new_text):
                    inserted = _insert_with_extracted_font(
                        page, bbox, new_text, gf, use_size, use_color
                    )

            # Attempt 4: Base14 fallback
            if not inserted:
                use_font = (
                    _normalize_font(font)
                    if font
                    else _normalize_font(orig_font, span_flags=orig_flags)
                )
                _insert_with_base14(
                    page,
                    bbox,
                    new_text,
                    use_font,
                    use_size,
                    use_color,
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
