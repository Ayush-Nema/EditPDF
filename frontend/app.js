const $ = (sel) => document.querySelector(sel);

const state = {
  docId: null,
  pageCount: 0,
  currentPage: 0,
  spans: [],
  pageWidth: 0,
  pageHeight: 0,
  selectedSpan: null,
  addMode: false,
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
  updateSelectionButtons();

  // Set onload BEFORE setting src to avoid race with cached images
  const imageLoaded = new Promise((resolve) => {
    pageImage.onload = resolve;
  });

  pageImage.src = `/api/documents/${docId}/pages/${currentPage}/image?t=${Date.now()}`;

  // Load text spans in parallel
  const res = await fetch(`/api/documents/${docId}/pages/${currentPage}/text`);
  if (!res.ok) {
    console.error("Failed to load text spans:", res.status);
    spanOverlay.innerHTML = "";
    return;
  }
  const data = await res.json();

  state.spans = data.spans;
  state.pageWidth = data.width;
  state.pageHeight = data.height;

  // Wait for image to finish loading so we have correct display dimensions
  await imageLoaded;
  renderSpanOverlays();
}

function renderSpanOverlays() {
  spanOverlay.innerHTML = "";

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
  state.selectedSpan = span;
  updateSelectionButtons();

  // Highlight
  document.querySelectorAll(".text-span").forEach((el) => {
    el.classList.toggle("selected", parseInt(el.dataset.index) === span.index);
  });

  // Fill properties panel
  propText.value = span.text;
  propFont.value = span.font;
  propSize.value = span.size;
  propColor.value = span.color;
}

// -- Apply edit --

applyBtn.addEventListener("click", async () => {
  const span = state.selectedSpan;
  if (!span) return;

  const body = {
    span_index: span.index,
    new_text: propText.value,
    font: propFont.value,
    size: parseFloat(propSize.value),
    color: propColor.value,
  };

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

// -- Add text mode --

addModeBtn.addEventListener("click", () => {
  state.addMode = !state.addMode;
  addModeBtn.classList.toggle("active", state.addMode);
  addModeBtn.textContent = state.addMode ? "Cancel Add Mode" : "Enable Add Mode";
  spanOverlay.style.cursor = state.addMode ? "crosshair" : "";
});

// Click on page to add text
spanOverlay.addEventListener("click", async (e) => {
  if (!state.addMode || !state.docId) return;

  const text = addText.value.trim();
  if (!text) {
    alert("Enter text to add first");
    return;
  }

  const rect = spanOverlay.getBoundingClientRect();
  const clickX = e.clientX - rect.left;
  const clickY = e.clientY - rect.top;

  const displayWidth = pageImage.clientWidth;
  const displayHeight = pageImage.clientHeight;
  const pdfX = (clickX / displayWidth) * state.pageWidth;
  const pdfY = (clickY / displayHeight) * state.pageHeight;

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
      alert("Add failed: " + (await res.json()).detail);
      return;
    }

    addText.value = "";
    state.addMode = false;
    addModeBtn.classList.remove("active");
    addModeBtn.textContent = "Enable Add Mode";
    spanOverlay.style.cursor = "";
    loadPage();
  } finally {
    setLoading(addModeBtn, false);
  }
});

// -- Resize handler --

window.addEventListener("resize", () => {
  if (state.spans.length > 0) renderSpanOverlays();
});
