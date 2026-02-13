"""Undo/redo snapshot logic for PDF mutations."""

from .config import MAX_UNDO, UPLOAD_DIR
from .document import _validate_doc_id

# Undo/redo snapshot stacks (in-memory, per document)
_undo_stacks: dict[str, list[bytes]] = {}
_redo_stacks: dict[str, list[bytes]] = {}


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
