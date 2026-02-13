"""Centralised configuration for the EditPDF backend."""

from pathlib import Path

# -- Storage --
UPLOAD_DIR = Path("uploads")

# -- Upload limits --
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

# -- Undo / Redo --
MAX_UNDO = 20  # max snapshots kept per document

# -- Rendering --
DEFAULT_RENDER_SCALE = 2.0  # PNG render resolution multiplier

# -- Text defaults --
DEFAULT_FONT = "helv"
DEFAULT_FONT_SIZE = 12.0
DEFAULT_TEXT_COLOR = "#000000"

# -- Layout constants --
PAGE_MARGIN = 5        # px inset from page edges when clamping
LINE_HEIGHT_FACTOR = 1.3   # bbox height = font_size * this
TEXT_BOX_HEIGHT_FACTOR = 1.5  # height of new-text boxes
TEXT_WIDTH_PADDING = 2     # extra px added when expanding bbox for wider text
DEFAULT_IMAGE_WIDTH = 200  # default width (px) for auto-scaled images
IMAGE_PADDING = 10         # px padding from page edge for images
MIN_IMAGE_SIZE = 10        # minimum image dimension (px)

# -- Font mapping (PDF name → Base14 equivalent) --
FONT_MAP: dict[str, str] = {
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

# -- Bullet detection --
SYMBOL_FONT_HINTS: set[str] = {
    "symbol", "zapf", "dingbat", "wingding", "webding", "bullet",
}
