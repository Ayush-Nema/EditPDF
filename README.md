# EditPDF

A locally-hosted PDF editor that runs in Docker. Upload PDFs, view rendered pages, inspect text properties (font, size, color), edit or delete text spans, add new text, insert/move/resize images, and download the modified PDF — all from your browser. Full undo/redo support.

## Tech Stack

- **Backend**: Python, FastAPI, PyMuPDF (pymupdf), managed with `uv`
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **Rendering**: Backend renders pages as PNG via `page.get_pixmap()` — pixel-perfect, no PDF.js needed
- **Editing**: Redact + re-insert strategy preserves document structure (images, bookmarks, metadata)

## How It Works

1. Upload a PDF
2. Pages render as images with transparent overlays positioned over each text span
3. Click any text span to see its font, size, and color in the properties panel
4. Edit the text or properties and click Apply — the backend redacts the old text and inserts the replacement
5. Add new text by enabling Add Mode and clicking on the page
6. Insert, move, resize, or delete images
7. Undo/redo any change with keyboard shortcuts
8. Download the final PDF with all edits preserved

## Project Structure

```
EditPDF/
├── .pre-commit-config.yaml
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── backend/
│   ├── __init__.py
│   ├── config.py          # Centralised configuration constants
│   ├── document.py        # Document lifecycle (validate, open, upload, render, download)
│   ├── history.py         # Undo/redo snapshot logic
│   ├── image_service.py   # Image extraction, add/delete/move/resize
│   ├── main.py            # FastAPI app, routes, static file serving
│   ├── models.py          # Pydantic request/response models
│   └── text_service.py    # Text extraction, editing, bullet detection, font/color helpers
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── uploads/               # Runtime storage for uploaded PDFs
```

## Setup

### Docker (recommended)

```bash
docker compose up --build
```

Open http://localhost:8000 in your browser.

To stop:

```bash
docker compose down
```

To rebuild after code changes:

```bash
docker compose up --build
```

To run in the background:

```bash
docker compose up --build -d
docker compose logs -f    # view logs
docker compose down       # stop
```

### Without Docker

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                        # install project dependencies
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

### Useful uv commands

```bash
uv sync                        # install/update project dependencies from pyproject.toml
uv run <command>               # run a command in the project's virtual environment
uv add <package>               # add a new dependency to pyproject.toml
uv remove <package>            # remove a dependency
uv tool install <tool>         # install a standalone CLI tool (e.g. pre-commit)
uv tool list                   # list installed CLI tools
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/upload` | Upload a PDF file |
| `GET` | `/api/documents/{doc_id}/pages/{page_num}/image` | Rendered page as PNG |
| `GET` | `/api/documents/{doc_id}/pages/{page_num}/text` | Text spans with font/size/color/bbox |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/edit` | Edit or delete a text span |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/add` | Add new text at coordinates |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/add-image` | Insert an image (multipart form) |
| `GET` | `/api/documents/{doc_id}/pages/{page_num}/images` | List image placements on a page |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/move-image` | Move an image |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/resize-image` | Resize an image |
| `POST` | `/api/documents/{doc_id}/pages/{page_num}/delete-image` | Delete an image |
| `POST` | `/api/documents/{doc_id}/undo` | Undo last edit |
| `POST` | `/api/documents/{doc_id}/redo` | Redo last undone edit |
| `GET` | `/api/documents/{doc_id}/download` | Download the edited PDF |

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+Z` (or `Cmd+Z`) | Undo |
| `Ctrl+Y` (or `Cmd+Y` / `Cmd+Shift+Z`) | Redo |
| `Delete` / `Backspace` | Delete selected image or text span |

Shortcuts are disabled when an input field is focused.

## Development

### Pre-commit hooks

This project uses [pre-commit](https://pre-commit.com/) to run checks automatically on every commit.

| Hook | Purpose |
|------|---------|
| `check-added-large-files` | Blocks files larger than 1 MB from being committed |
| `detect-private-key` | Prevents accidental commit of private keys |
| `ruff` | Python linting with auto-fix |
| `ruff-format` | Python code formatting |

```bash
# Install pre-commit (requires uv)
uv tool install pre-commit

# Install the git hook (runs automatically on every commit)
pre-commit install

# Run manually on all files
pre-commit run --all-files

# Run a specific hook
pre-commit run ruff --all-files
pre-commit run detect-private-key --all-files
```

Ruff configuration lives in `pyproject.toml` under `[tool.ruff]`.

## Known Limitations

- **Font substitution**: Embedded/subset fonts are replaced with the closest Base14 match (Helvetica, Times, Courier). A PDF using e.g. Garamond will have edits rendered in Helvetica.
- **Single-span editing**: One text span at a time; no multi-span selection.
- **No RTL/complex script support**.
- **Upload limit**: 50 MB max file size.
