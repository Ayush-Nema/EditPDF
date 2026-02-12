import asyncio
from functools import partial

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .models import AddTextRequest, EditRequest, PageTextResponse, UploadResponse
from .pdf_service import (
    MAX_UPLOAD_SIZE,
    add_text,
    edit_span,
    extract_text_spans,
    get_pdf_bytes,
    render_page,
    save_upload,
)

app = FastAPI(title="EditPDF")


async def _run_sync(fn, *args, **kwargs):
    """Run a blocking function in a thread so it doesn't stall the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


@app.post("/api/upload", response_model=UploadResponse)
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024} MB)")
    try:
        doc_id, page_count = await _run_sync(save_upload, content, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Invalid PDF: {e}")
    return UploadResponse(doc_id=doc_id, page_count=page_count)


@app.get("/api/documents/{doc_id}/pages/{page_num}/image")
async def get_page_image(doc_id: str, page_num: int):
    try:
        png = await _run_sync(render_page, doc_id, page_num)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return Response(content=png, media_type="image/png")


@app.get("/api/documents/{doc_id}/pages/{page_num}/text", response_model=PageTextResponse)
async def get_page_text(doc_id: str, page_num: int):
    try:
        data = await _run_sync(extract_text_spans, doc_id, page_num)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return data


@app.post("/api/documents/{doc_id}/pages/{page_num}/edit")
async def edit_text(doc_id: str, page_num: int, req: EditRequest):
    try:
        await _run_sync(edit_span, doc_id, page_num, req.span_index, req.new_text,
                        req.font, req.size, req.color)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/pages/{page_num}/add")
async def add_new_text(doc_id: str, page_num: int, req: AddTextRequest):
    try:
        await _run_sync(add_text, doc_id, page_num, req.x, req.y, req.text,
                        req.font, req.size, req.color)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    return {"status": "ok"}


@app.get("/api/documents/{doc_id}/download")
async def download_pdf(doc_id: str):
    try:
        pdf_bytes = await _run_sync(get_pdf_bytes, doc_id)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={doc_id}.pdf"},
    )


# Serve frontend static files (must be last to not shadow API routes)
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
