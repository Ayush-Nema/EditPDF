"""Document lifecycle: validate, open, upload, render, and download PDFs."""

import hashlib
import re
import uuid

import pymupdf

from .config import DEFAULT_RENDER_SCALE, MAX_UPLOAD_SIZE, UPLOAD_DIR

UPLOAD_DIR.mkdir(exist_ok=True)

_DOC_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _validate_doc_id(doc_id: str) -> None:
    """Reject any doc_id that isn't exactly 16 hex chars (path traversal guard)."""
    if not _DOC_ID_RE.match(doc_id):
        raise ValueError(f"Invalid document id: {doc_id}")


def _open_doc(doc_id: str) -> pymupdf.Document:
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return pymupdf.open(str(path))


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


def render_page(doc_id: str, page_num: int, scale: float = DEFAULT_RENDER_SCALE) -> bytes:
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


def get_pdf_bytes(doc_id: str) -> bytes:
    """Return the PDF file bytes for download."""
    _validate_doc_id(doc_id)
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return path.read_bytes()
