"""Image extraction, insertion, deletion, move, and resize operations."""

import pymupdf

from .config import (
    DEFAULT_IMAGE_WIDTH,
    IMAGE_PADDING,
    MIN_IMAGE_SIZE,
    PAGE_MARGIN,
    UPLOAD_DIR,
)
from .document import _open_doc
from .history import _snapshot


def _save_full(doc, doc_id: str) -> None:
    """Non-incremental save with garbage collection (required after redaction).

    PyMuPDF won't allow non-incremental save to the same open file,
    so we serialize to bytes, close the doc, and overwrite the file.
    """
    pdf_bytes = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    path.write_bytes(pdf_bytes)


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
                images.append(
                    {
                        "index": index,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                        "width": rect.width,
                        "height": rect.height,
                        "xref": xref,
                    }
                )
                index += 1

        return {
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
            "images": images,
        }
    finally:
        doc.close()


def add_image(
    doc_id: str,
    page_num: int,
    x: float,
    y: float,
    image_bytes: bytes,
    width: float = 0,
    height: float = 0,
) -> None:
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
            scale = min(DEFAULT_IMAGE_WIDTH / img_w, (page.rect.width - x - IMAGE_PADDING) / img_w)
            if width <= 0:
                width = img_w * scale
            if height <= 0:
                height = img_h * scale

        # Clamp to page bounds
        x1 = min(x + width, page.rect.width - PAGE_MARGIN)
        y1 = min(y + height, page.rect.height - PAGE_MARGIN)
        rect = pymupdf.Rect(x, y, x1, y1)

        page.insert_image(rect, stream=image_bytes)

        path = UPLOAD_DIR / f"{doc_id}.pdf"
        doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()


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


def move_image(doc_id: str, page_num: int, image_index: int, new_x: float, new_y: float) -> None:
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


def resize_image(
    doc_id: str,
    page_num: int,
    image_index: int,
    new_x: float,
    new_y: float,
    new_w: float,
    new_h: float,
) -> None:
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
        new_w = max(MIN_IMAGE_SIZE, new_w)
        new_h = max(MIN_IMAGE_SIZE, new_h)

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
