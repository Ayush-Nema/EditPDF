import asyncio
from functools import partial

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .config import MAX_UPLOAD_SIZE
from .models import (
    AddTextRequest,
    DeleteImageRequest,
    EditRequest,
    MoveImageRequest,
    PageImagesResponse,
    PageTextResponse,
    ResizeImageRequest,
    UploadResponse,
)
from .pdf_service import (
    add_image,
    add_text,
    delete_image,
    edit_span,
    extract_images,
    extract_text_spans,
    get_pdf_bytes,
    move_image,
    redo,
    render_page,
    resize_image,
    save_upload,
    undo,
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
        await _run_sync(
            edit_span, doc_id, page_num, req.span_index, req.new_text, req.font, req.size, req.color
        )
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
        await _run_sync(
            add_text, doc_id, page_num, req.x, req.y, req.text, req.font, req.size, req.color
        )
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/pages/{page_num}/add-image")
async def add_image_to_page(
    doc_id: str,
    page_num: int,
    file: UploadFile = File(...),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(0),
    height: float = Form(0),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are accepted")
    image_bytes = await file.read()
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file")
    try:
        await _run_sync(add_image, doc_id, page_num, x, y, image_bytes, width, height)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except Exception as e:
        raise HTTPException(400, f"Failed to insert image: {e}")
    return {"status": "ok"}


@app.get("/api/documents/{doc_id}/pages/{page_num}/images", response_model=PageImagesResponse)
async def get_page_images(doc_id: str, page_num: int):
    try:
        data = await _run_sync(extract_images, doc_id, page_num)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return data


@app.post("/api/documents/{doc_id}/pages/{page_num}/move-image")
async def move_image_endpoint(doc_id: str, page_num: int, req: MoveImageRequest):
    try:
        await _run_sync(move_image, doc_id, page_num, req.image_index, req.x, req.y)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/pages/{page_num}/resize-image")
async def resize_image_endpoint(doc_id: str, page_num: int, req: ResizeImageRequest):
    try:
        await _run_sync(
            resize_image, doc_id, page_num, req.image_index, req.x, req.y, req.width, req.height
        )
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/pages/{page_num}/delete-image")
async def delete_image_endpoint(doc_id: str, page_num: int, req: DeleteImageRequest):
    try:
        await _run_sync(delete_image, doc_id, page_num, req.image_index)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    except FileNotFoundError:
        raise HTTPException(404, "Document not found")
    except IndexError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/undo")
async def undo_endpoint(doc_id: str):
    try:
        success = await _run_sync(undo, doc_id)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    if not success:
        raise HTTPException(404, "Nothing to undo")
    return {"status": "ok"}


@app.post("/api/documents/{doc_id}/redo")
async def redo_endpoint(doc_id: str):
    try:
        success = await _run_sync(redo, doc_id)
    except ValueError:
        raise HTTPException(400, "Invalid document id")
    if not success:
        raise HTTPException(404, "Nothing to redo")
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
