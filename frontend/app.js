const $ = (sel) => document.querySelector(sel);

const state = {
  docId: null,
  pageCount: 0,
  currentPage: 0,
  spans: [],
  images: [],
  pageWidth: 0,
  pageHeight: 0,
  selectedSpan: null,
  selectedImage: null,
  addMode: false,
  imgMode: false,
  imgDragging: false,
  imgDragStart: null,
  // Image move/resize state
  imgDragMoving: false,
  imgResizing: false,
  imgResizeHandle: null,
  imgDragOffset: null,
  imgOrigRect: null,
};

// Elements
const fileInput = $("#file-input");
const downloadBtn = $("#download-btn");
const emptyState = $("#empty-state");
const pageWrapper = $("#page-wrapper");
const pageImage = $("#page-image");
const spanOverlay = $("#span-overlay");
const pageInfo = $("#page-info");
const prevBtn = $("#prev-btn");
const nextBtn = $("#next-btn");
const panel = $("#properties-panel");
const propText = $("#prop-text");
const propFont = $("#prop-font");
const propSize = $("#prop-size");
const propColor = $("#prop-color");
const applyBtn = $("#apply-btn");
const deleteBtn = $("#delete-btn");
const addText = $("#add-text");
const addSize = $("#add-size");
const addColor = $("#add-color");
const addModeBtn = $("#add-mode-btn");
const imgFile = $("#img-file");
const imgModeBtn = $("#img-mode-btn");
const imgPreviewRect = $("#img-preview-rect");
const imgPreviewThumb = $("#img-preview-thumb");
const imgProps = $("#img-props");
const spanProps = $("#span-props");
const imgPropX = $("#img-prop-x");
const imgPropY = $("#img-prop-y");
const imgPropW = $("#img-prop-w");
const imgPropH = $("#img-prop-h");
const imgDeleteBtn = $("#img-delete-btn");

// -- Helpers --

function setLoading(el, loading) {
  el.disabled = loading;
  el.classList.toggle("loading", loading);
}

function updateSelectionButtons() {
  const hasSelection = state.selectedSpan !== null;
  applyBtn.disabled = !hasSelection;
  deleteBtn.disabled = !hasSelection;
}

// Initialize button states
updateSelectionButtons();

// -- Upload --

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  const form = new FormData();
  form.append("file", file);

  setLoading(downloadBtn, true);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: form });
    if (!res.ok) {
      alert("Upload failed: " + (await res.json()).detail);
      return;
    }

    const data = await res.json();
    state.docId = data.doc_id;
    state.pageCount = data.page_count;
    state.currentPage = 0;

    emptyState.hidden = true;
    pageWrapper.hidden = false;
    downloadBtn.hidden = false;
    panel.hidden = false;

    loadPage();
  } finally {
    setLoading(downloadBtn, false);
    // Reset so re-selecting the same file still triggers change
    fileInput.value = "";
  }
});

// -- Download --

downloadBtn.addEventListener("click", () => {
  if (!state.docId) return;
  window.open(`/api/documents/${state.docId}/download`, "_blank");
});

// -- Page navigation --

prevBtn.addEventListener("click", () => {
  if (state.currentPage > 0) {
    state.currentPage--;
    loadPage();
  }
});

nextBtn.addEventListener("click", () => {
  if (state.currentPage < state.pageCount - 1) {
    state.currentPage++;
    loadPage();
  }
});

async function loadPage() {
  const { docId, currentPage, pageCount } = state;

  pageInfo.textContent = `Page ${currentPage + 1} / ${pageCount}`;
  prevBtn.disabled = currentPage === 0;
  nextBtn.disabled = currentPage === pageCount - 1;
  state.selectedSpan = null;
  deselectImage();
  updateSelectionButtons();

  // Set onload BEFORE setting src to avoid race with cached images
  const imageLoaded = new Promise((resolve) => {
    pageImage.onload = resolve;
  });

  pageImage.src = `/api/documents/${docId}/pages/${currentPage}/image?t=${Date.now()}`;

  // Load text spans and images in parallel
  const [textRes, imgRes] = await Promise.all([
    fetch(`/api/documents/${docId}/pages/${currentPage}/text`),
    fetch(`/api/documents/${docId}/pages/${currentPage}/images`),
  ]);

  if (!textRes.ok) {
    console.error("Failed to load text spans:", textRes.status);
    spanOverlay.querySelectorAll(".text-span").forEach((el) => el.remove());
    return;
  }
  const data = await textRes.json();
  state.spans = data.spans;
  state.pageWidth = data.width;
  state.pageHeight = data.height;

  if (imgRes.ok) {
    const imgData = await imgRes.json();
    state.images = imgData.images;
  } else {
    state.images = [];
  }

  // Wait for image to finish loading so we have correct display dimensions
  await imageLoaded;
  renderSpanOverlays();
  renderImageOverlays();
}

function renderSpanOverlays() {
  // Remove text spans but keep the preview rect
  spanOverlay.querySelectorAll(".text-span").forEach((el) => el.remove());

  const displayWidth = pageImage.clientWidth;
  const displayHeight = pageImage.clientHeight;
  const scaleX = displayWidth / state.pageWidth;
  const scaleY = displayHeight / state.pageHeight;

  for (const span of state.spans) {
    const div = document.createElement("div");
    div.className = "text-span";
    div.dataset.index = span.index;

    const [x0, y0, x1, y1] = span.bbox;
    div.style.left = `${x0 * scaleX}px`;
    div.style.top = `${y0 * scaleY}px`;
    div.style.width = `${(x1 - x0) * scaleX}px`;
    div.style.height = `${(y1 - y0) * scaleY}px`;

    div.addEventListener("click", (e) => {
      e.stopPropagation();
      selectSpan(span);
    });

    spanOverlay.appendChild(div);
  }
}

function selectSpan(span) {
  deselectImage();
  state.selectedSpan = span;
  updateSelectionButtons();

  // Highlight
  document.querySelectorAll(".text-span").forEach((el) => {
    el.classList.toggle("selected", parseInt(el.dataset.index) === span.index);
  });

  // Show span props, hide image props
  spanProps.hidden = false;
  imgProps.hidden = true;

  // Fill properties panel — show normalized font so user sees what will be used
  propText.value = span.text;
  propFont.value = span.normalized_font || span.font;
  propSize.value = span.size;
  propColor.value = span.color;
}

// -- Image overlays --

function renderImageOverlays() {
  spanOverlay.querySelectorAll(".image-overlay").forEach((el) => el.remove());

  const displayWidth = pageImage.clientWidth;
  const displayHeight = pageImage.clientHeight;
  const scaleX = displayWidth / state.pageWidth;
  const scaleY = displayHeight / state.pageHeight;

  for (const img of state.images) {
    const div = document.createElement("div");
    div.className = "image-overlay";
    div.dataset.index = img.index;

    const [x0, y0, x1, y1] = img.bbox;
    div.style.left = `${x0 * scaleX}px`;
    div.style.top = `${y0 * scaleY}px`;
    div.style.width = `${(x1 - x0) * scaleX}px`;
    div.style.height = `${(y1 - y0) * scaleY}px`;

    // Resize handles (corners + edges)
    for (const corner of ["nw", "ne", "sw", "se", "n", "s", "e", "w"]) {
      const handle = document.createElement("div");
      handle.className = `resize-handle handle-${corner}`;
      handle.dataset.handle = corner;
      handle.addEventListener("mousedown", (e) => {
        e.stopPropagation();
        e.preventDefault();
        if (!state.selectedImage || state.selectedImage.index !== img.index) {
          selectImage(img);
        }
        startImageResize(e, corner);
      });
      div.appendChild(handle);
    }

    div.addEventListener("mousedown", (e) => {
      // Don't interfere with add modes
      if (state.addMode || state.imgMode) return;
      e.stopPropagation();
      e.preventDefault();
      selectImage(img);
      startImageMove(e);
    });

    if (state.selectedImage && state.selectedImage.index === img.index) {
      div.classList.add("selected");
    }

    spanOverlay.appendChild(div);
  }
}

function selectImage(img) {
  // Deselect text span
  state.selectedSpan = null;
  updateSelectionButtons();
  document.querySelectorAll(".text-span").forEach((el) => el.classList.remove("selected"));

  state.selectedImage = img;

  // Highlight
  document.querySelectorAll(".image-overlay").forEach((el) => {
    el.classList.toggle("selected", parseInt(el.dataset.index) === img.index);
  });

  // Show image props, hide span props
  spanProps.hidden = true;
  imgProps.hidden = false;

  const [x0, y0, x1, y1] = img.bbox;
  imgPropX.value = Math.round(x0);
  imgPropY.value = Math.round(y0);
  imgPropW.value = Math.round(x1 - x0);
  imgPropH.value = Math.round(y1 - y0);
}

function deselectImage() {
  state.selectedImage = null;
  state.imgDragMoving = false;
  state.imgResizing = false;
  document.querySelectorAll(".image-overlay").forEach((el) => el.classList.remove("selected"));
  imgProps.hidden = true;
}

// -- Image move --

function startImageMove(e) {
  const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
  if (!overlay) return;

  const rect = overlay.getBoundingClientRect();
  state.imgDragMoving = true;
  state.imgDragOffset = {
    x: e.clientX - rect.left,
    y: e.clientY - rect.top,
  };
  state.imgOrigRect = {
    left: parseFloat(overlay.style.left),
    top: parseFloat(overlay.style.top),
  };
}

function startImageResize(e, handle) {
  const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
  if (!overlay) return;

  state.imgResizing = true;
  state.imgResizeHandle = handle;
  state.imgOrigRect = {
    left: parseFloat(overlay.style.left),
    top: parseFloat(overlay.style.top),
    width: parseFloat(overlay.style.width),
    height: parseFloat(overlay.style.height),
    startX: e.clientX,
    startY: e.clientY,
  };
}

// -- Image delete --

imgDeleteBtn.addEventListener("click", async () => {
  const img = state.selectedImage;
  if (!img) return;

  setLoading(imgDeleteBtn, true);
  try {
    const res = await fetch(
      `/api/documents/${state.docId}/pages/${state.currentPage}/delete-image`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_index: img.index }),
      }
    );
    if (!res.ok) {
      alert("Delete image failed: " + (await res.json()).detail);
      return;
    }
    deselectImage();
    loadPage();
  } finally {
    setLoading(imgDeleteBtn, false);
  }
});

// -- Apply edit --

applyBtn.addEventListener("click", async () => {
  const span = state.selectedSpan;
  if (!span) return;

  // Only send properties the user actually changed
  const body = {
    span_index: span.index,
    new_text: propText.value,
  };
  const origFont = span.normalized_font || span.font;
  if (propFont.value !== origFont) body.font = propFont.value;
  if (parseFloat(propSize.value) !== span.size) body.size = parseFloat(propSize.value);
  if (propColor.value !== span.color) body.color = propColor.value;

  setLoading(applyBtn, true);
  setLoading(deleteBtn, true);
  try {
    const res = await fetch(
      `/api/documents/${state.docId}/pages/${state.currentPage}/edit`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    );

    if (!res.ok) {
      alert("Edit failed: " + (await res.json()).detail);
      return;
    }

    state.selectedSpan = null;
    updateSelectionButtons();
    loadPage();
  } finally {
    setLoading(applyBtn, false);
    setLoading(deleteBtn, false);
  }
});

// -- Delete span --

deleteBtn.addEventListener("click", async () => {
  const span = state.selectedSpan;
  if (!span) return;

  const body = {
    span_index: span.index,
    new_text: "",
  };

  setLoading(deleteBtn, true);
  setLoading(applyBtn, true);
  try {
    const res = await fetch(
      `/api/documents/${state.docId}/pages/${state.currentPage}/edit`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    );

    if (!res.ok) {
      alert("Delete failed: " + (await res.json()).detail);
      return;
    }

    state.selectedSpan = null;
    updateSelectionButtons();
    loadPage();
  } finally {
    setLoading(deleteBtn, false);
    setLoading(applyBtn, false);
  }
});

// -- Helpers for mutually exclusive add modes --

function exitAddMode() {
  state.addMode = false;
  addModeBtn.classList.remove("active");
  addModeBtn.textContent = "Enable Add Mode";
}

function exitImgMode() {
  state.imgMode = false;
  state.imgDragging = false;
  state.imgDragStart = null;
  imgPreviewRect.style.display = "none";
  imgModeBtn.classList.remove("active");
  imgModeBtn.textContent = "Enable Image Mode";
}

function updateOverlayCursor() {
  if (state.addMode || state.imgMode) {
    spanOverlay.style.cursor = "crosshair";
    spanOverlay.classList.add("placement-mode");
  } else {
    spanOverlay.style.cursor = "";
    spanOverlay.classList.remove("placement-mode");
  }
}

// -- Add text mode --

addModeBtn.addEventListener("click", () => {
  if (state.imgMode) exitImgMode();
  state.addMode = !state.addMode;
  addModeBtn.classList.toggle("active", state.addMode);
  addModeBtn.textContent = state.addMode ? "Cancel Add Mode" : "Enable Add Mode";
  updateOverlayCursor();
});

// -- Add image mode --

imgModeBtn.addEventListener("click", () => {
  if (!imgFile.files[0]) {
    alert("Select an image file first");
    return;
  }
  if (state.addMode) exitAddMode();
  state.imgMode = !state.imgMode;
  imgModeBtn.classList.toggle("active", state.imgMode);
  imgModeBtn.textContent = state.imgMode ? "Cancel Image Mode" : "Enable Image Mode";
  updateOverlayCursor();
});

// -- Click on page to add text --

spanOverlay.addEventListener("click", async (e) => {
  if (!state.docId) return;

  // Ignore clicks that came from an image overlay or its children (resize handles)
  if (e.target.closest(".image-overlay")) return;

  // If not in add mode, clicking the overlay (empty area) deselects everything
  if (!state.addMode) {
    if (state.selectedImage) deselectImage();
    if (state.selectedSpan) {
      state.selectedSpan = null;
      updateSelectionButtons();
      document.querySelectorAll(".text-span").forEach((el) => el.classList.remove("selected"));
      spanProps.hidden = false;
      imgProps.hidden = true;
    }
    return;
  }

  const rect = spanOverlay.getBoundingClientRect();
  const clickX = e.clientX - rect.left;
  const clickY = e.clientY - rect.top;
  const displayWidth = pageImage.clientWidth;
  const displayHeight = pageImage.clientHeight;
  const pdfX = (clickX / displayWidth) * state.pageWidth;
  const pdfY = (clickY / displayHeight) * state.pageHeight;

  const text = addText.value.trim();
  if (!text) {
    alert("Enter text to add first");
    return;
  }

  const body = {
    x: pdfX,
    y: pdfY,
    text: text,
    size: parseFloat(addSize.value),
    color: addColor.value,
  };

  setLoading(addModeBtn, true);
  try {
    const res = await fetch(
      `/api/documents/${state.docId}/pages/${state.currentPage}/add`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    );
    if (!res.ok) {
      alert("Add text failed: " + (await res.json()).detail);
      return;
    }
    addText.value = "";
    exitAddMode();
    updateOverlayCursor();
    loadPage();
  } finally {
    setLoading(addModeBtn, false);
  }
});

// -- Image drag-to-draw --

spanOverlay.addEventListener("mousedown", (e) => {
  if (!state.imgMode || !state.docId) return;
  e.preventDefault();

  const rect = spanOverlay.getBoundingClientRect();
  state.imgDragging = true;
  state.imgDragStart = { x: e.clientX - rect.left, y: e.clientY - rect.top };

  // Show preview rect at zero size
  imgPreviewRect.style.left = `${state.imgDragStart.x}px`;
  imgPreviewRect.style.top = `${state.imgDragStart.y}px`;
  imgPreviewRect.style.width = "0px";
  imgPreviewRect.style.height = "0px";
  imgPreviewRect.style.display = "block";

  // Load thumbnail preview from selected file
  const file = imgFile.files[0];
  if (file) {
    imgPreviewThumb.src = URL.createObjectURL(file);
  }
});

document.addEventListener("mousemove", (e) => {
  // Image move
  if (state.imgDragMoving && state.selectedImage) {
    const overlayRect = spanOverlay.getBoundingClientRect();
    const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
    if (overlay) {
      let newLeft = e.clientX - overlayRect.left - state.imgDragOffset.x;
      let newTop = e.clientY - overlayRect.top - state.imgDragOffset.y;
      const maxLeft = pageImage.clientWidth - parseFloat(overlay.style.width);
      const maxTop = pageImage.clientHeight - parseFloat(overlay.style.height);
      newLeft = Math.max(0, Math.min(newLeft, maxLeft));
      newTop = Math.max(0, Math.min(newTop, maxTop));
      overlay.style.left = `${newLeft}px`;
      overlay.style.top = `${newTop}px`;
    }
    return;
  }

  // Image resize
  if (state.imgResizing && state.selectedImage) {
    const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
    if (overlay) {
      const dx = e.clientX - state.imgOrigRect.startX;
      const dy = e.clientY - state.imgOrigRect.startY;
      const handle = state.imgResizeHandle;
      let { left, top, width, height } = state.imgOrigRect;
      const minSize = 20;

      if (handle.includes("e")) {
        width = Math.max(minSize, width + dx);
      }
      if (handle.includes("w")) {
        const newW = Math.max(minSize, width - dx);
        left = left + (width - newW);
        width = newW;
      }
      if (handle.includes("s")) {
        height = Math.max(minSize, height + dy);
      }
      if (handle.includes("n")) {
        const newH = Math.max(minSize, height - dy);
        top = top + (height - newH);
        height = newH;
      }

      // Clamp to page
      left = Math.max(0, left);
      top = Math.max(0, top);
      if (left + width > pageImage.clientWidth) width = pageImage.clientWidth - left;
      if (top + height > pageImage.clientHeight) height = pageImage.clientHeight - top;

      overlay.style.left = `${left}px`;
      overlay.style.top = `${top}px`;
      overlay.style.width = `${width}px`;
      overlay.style.height = `${height}px`;
    }
    return;
  }

  // Image drag-to-draw
  if (!state.imgDragging) return;

  const rect = spanOverlay.getBoundingClientRect();
  const curX = e.clientX - rect.left;
  const curY = e.clientY - rect.top;
  const start = state.imgDragStart;

  const left = Math.min(start.x, curX);
  const top = Math.min(start.y, curY);
  const width = Math.abs(curX - start.x);
  const height = Math.abs(curY - start.y);

  imgPreviewRect.style.left = `${left}px`;
  imgPreviewRect.style.top = `${top}px`;
  imgPreviewRect.style.width = `${width}px`;
  imgPreviewRect.style.height = `${height}px`;
});

document.addEventListener("mouseup", async (e) => {
  // Image move end
  if (state.imgDragMoving && state.selectedImage) {
    state.imgDragMoving = false;
    const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
    if (overlay) {
      const newLeft = parseFloat(overlay.style.left);
      const newTop = parseFloat(overlay.style.top);
      const origLeft = state.imgOrigRect.left;
      const origTop = state.imgOrigRect.top;
      const dist = Math.hypot(newLeft - origLeft, newTop - origTop);
      if (dist > 2) {
        // Convert display coords to PDF coords
        const scaleX = state.pageWidth / pageImage.clientWidth;
        const scaleY = state.pageHeight / pageImage.clientHeight;
        const pdfX = newLeft * scaleX;
        const pdfY = newTop * scaleY;
        try {
          const res = await fetch(
            `/api/documents/${state.docId}/pages/${state.currentPage}/move-image`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                image_index: state.selectedImage.index,
                x: pdfX,
                y: pdfY,
              }),
            }
          );
          if (!res.ok) {
            console.error("Move image failed:", res.status);
          }
        } catch (err) {
          console.error("Move image error:", err);
        }
        loadPage();
      }
    }
    return;
  }

  // Image resize end
  if (state.imgResizing && state.selectedImage) {
    state.imgResizing = false;
    const overlay = spanOverlay.querySelector(`.image-overlay[data-index="${state.selectedImage.index}"]`);
    if (overlay) {
      const scaleX = state.pageWidth / pageImage.clientWidth;
      const scaleY = state.pageHeight / pageImage.clientHeight;
      const pdfX = parseFloat(overlay.style.left) * scaleX;
      const pdfY = parseFloat(overlay.style.top) * scaleY;
      const pdfW = parseFloat(overlay.style.width) * scaleX;
      const pdfH = parseFloat(overlay.style.height) * scaleY;
      try {
        const res = await fetch(
          `/api/documents/${state.docId}/pages/${state.currentPage}/resize-image`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              image_index: state.selectedImage.index,
              x: pdfX,
              y: pdfY,
              width: pdfW,
              height: pdfH,
            }),
          }
        );
        if (!res.ok) {
          console.error("Resize image failed:", res.status);
        }
      } catch (err) {
        console.error("Resize image error:", err);
      }
      loadPage();
    }
    return;
  }

  if (!state.imgDragging) return;
  state.imgDragging = false;

  const rect = spanOverlay.getBoundingClientRect();
  const endX = e.clientX - rect.left;
  const endY = e.clientY - rect.top;
  const start = state.imgDragStart;
  state.imgDragStart = null;

  // Hide preview
  imgPreviewRect.style.display = "none";

  // Revoke thumbnail object URL
  if (imgPreviewThumb.src.startsWith("blob:")) {
    URL.revokeObjectURL(imgPreviewThumb.src);
  }
  imgPreviewThumb.src = "";

  // Ignore tiny clicks (< 5px drag)
  const dragDist = Math.hypot(endX - start.x, endY - start.y);
  if (dragDist < 5) return;

  // Compute display rect (handles any drag direction)
  const displayLeft = Math.min(start.x, endX);
  const displayTop = Math.min(start.y, endY);
  const displayWidth = Math.abs(endX - start.x);
  const displayHeight = Math.abs(endY - start.y);

  // Convert to PDF coordinates
  const imgW = pageImage.clientWidth;
  const imgH = pageImage.clientHeight;
  const pdfX = (displayLeft / imgW) * state.pageWidth;
  const pdfY = (displayTop / imgH) * state.pageHeight;
  const pdfW = (displayWidth / imgW) * state.pageWidth;
  const pdfH = (displayHeight / imgH) * state.pageHeight;

  const file = imgFile.files[0];
  if (!file) return;

  const form = new FormData();
  form.append("file", file);
  form.append("x", pdfX);
  form.append("y", pdfY);
  form.append("width", pdfW);
  form.append("height", pdfH);

  setLoading(imgModeBtn, true);
  try {
    const res = await fetch(
      `/api/documents/${state.docId}/pages/${state.currentPage}/add-image`,
      { method: "POST", body: form }
    );
    if (!res.ok) {
      alert("Add image failed: " + (await res.json()).detail);
      return;
    }
    imgFile.value = "";
    exitImgMode();
    updateOverlayCursor();
    loadPage();
  } finally {
    setLoading(imgModeBtn, false);
  }
});

// -- Keyboard shortcuts (Undo / Redo / Delete) --

document.addEventListener("keydown", async (e) => {
  if (!state.docId) return;

  // Skip when focus is in an input or textarea (let browser handle it)
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

  // Ctrl+Z — Undo
  if ((e.ctrlKey || e.metaKey) && e.key === "z" && !e.shiftKey) {
    e.preventDefault();
    try {
      const res = await fetch(`/api/documents/${state.docId}/undo`, { method: "POST" });
      if (res.ok) loadPage();
    } catch (err) {
      console.error("Undo error:", err);
    }
    return;
  }

  // Ctrl+Y — Redo
  if ((e.ctrlKey || e.metaKey) && (e.key === "y" || (e.key === "z" && e.shiftKey))) {
    e.preventDefault();
    try {
      const res = await fetch(`/api/documents/${state.docId}/redo`, { method: "POST" });
      if (res.ok) loadPage();
    } catch (err) {
      console.error("Redo error:", err);
    }
    return;
  }

  // Delete / Backspace — delete selected image or span
  if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    if (state.selectedImage) {
      imgDeleteBtn.click();
    } else if (state.selectedSpan) {
      deleteBtn.click();
    }
  }
});

// -- Resize handler --

window.addEventListener("resize", () => {
  if (state.spans.length > 0) renderSpanOverlays();
  if (state.images.length > 0) renderImageOverlays();
});
