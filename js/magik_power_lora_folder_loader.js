// Magik Power Lora Folder Loader -- frontend extension.
//
// Pairs with magik_power_lora_folder_loader.py. When the "lora_folder" combo changes (or on
// node creation / workflow load / manual rescan), this fetches the loras in that folder from
// the backend and builds one custom "row" widget per file: a checkbox (on/off) + a strength
// stepper. Each row's widget.value is {on, lora, strength} -- sent to Python as the "lora_x"
// input, matching the dict shape load_loras() expects.
//
// Each row also has an info button (a small "i" circle left of the strength stepper). It is
// filled green when the lora's sidecar already has trigger words, and a dim outline otherwise.
// Clicking it opens a dialog to fetch trigger words / base model / link from Civitai (by file
// hash, via the backend), copy them, or clear them. The badge data comes from the folder-scan
// route ("hasWords") and is kept in widget._hasWords -- it is never part of widget.value, so
// it isn't serialized or sent to Python.
//
// This was adapted after comparing against rgthree-comfy's actual power_lora_loader.js. Two
// things were brought in line with that reference:
//   1. Serialization: saved lora-row state is read straight out of the standard
//      `info.widgets_values` array in onConfigure (matching entries that carry a `.lora` key),
//      the same way rgthree's own `configure()` override does it -- rather than a bolted-on
//      custom property. Default LiteGraph serialization already writes each widget's value in
//      order, so no onSerialize override is needed.
//   2. Strength interaction: click the left/right arrows to step +/-0.05, click-and-drag the
//      value to scrub it, or click without dragging to type an exact number.
// rgthree's own version builds on internal classes (RgthreeBaseWidget, a hit-area dispatch
// system, drawNumberWidgetPart/fitString canvas helpers) that live in files that weren't
// provided and are tightly coupled to that package, so this re-implements the same behavior
// directly with raw canvas draw + mouse handling instead of reusing those classes.
//
// NOTE on portability: widget.mouse() below checks event.type for "down"/"move"/"up" suffixes
// so it works whether your LiteGraph build fires "pointerdown" or "mousedown" style events. If
// dragging feels off, check whether your build exposes a per-event `event.deltaX` (rgthree's
// does) -- this uses it when present and falls back to a manual delta otherwise.

import { app } from "../../scripts/app.js";

const NODE_TYPE = "Magik.PowerLoraFolderLoader";
const LIST_LORAS_ROUTE = "/magik/power_lora_folder_loader/loras";
const INFO_ROUTE = "/magik/power_lora_folder_loader/info";
const ROW_HEIGHT = 22;
const STRENGTH_STEP = 0.05;
const STRENGTH_DRAG_SCALE = 0.05;

async function fetchLorasForFolder(folder) {
  try {
    const resp = await fetch(`${LIST_LORAS_ROUTE}?folder=${encodeURIComponent(folder)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return { loras: data.loras || [], hasWords: data.hasWords || {} };
  } catch (err) {
    console.warn(`[${NODE_TYPE}] Failed to fetch loras for folder "${folder}":`, err);
    return { loras: [], hasWords: {} };
  }
}

function extractSavedLoraRows(widgetsValues) {
  if (!Array.isArray(widgetsValues)) return [];
  return widgetsValues.filter((v) => v && typeof v === "object" && typeof v.lora === "string");
}

function removeRowWidgets(node) {
  if (!node.loraRowWidgets || !node.loraRowWidgets.length) return;
  const toRemove = new Set(node.loraRowWidgets);
  node.widgets = node.widgets.filter((w) => !toRemove.has(w));
  node.loraRowWidgets = [];
}

// ---------------------------------------------------------------------------------------
// Info dialog: trigger words / base model / Civitai link for one lora, with Fetch + Clear.
// Everything that came from Civitai is written with textContent (never innerHTML), and the
// link is only used if it points at civitai.red or civitai.com.
// ---------------------------------------------------------------------------------------

function el(tag, style, text) {
  const e = document.createElement(tag);
  if (style) Object.assign(e.style, style);
  if (text !== undefined) e.textContent = text;
  return e;
}

function styledButton(label) {
  const b = el(
    "button",
    {
      background: "#333",
      color: "#ddd",
      border: "1px solid #666",
      borderRadius: "4px",
      padding: "4px 10px",
      cursor: "pointer",
      font: "12px Arial, sans-serif",
    },
    label
  );
  b.type = "button";
  return b;
}

async function getLoraInfo(lora) {
  try {
    const resp = await fetch(`${INFO_ROUTE}?lora=${encodeURIComponent(lora)}`);
    return (await resp.json().catch(() => null)) || { status: "error", message: `HTTP ${resp.status}` };
  } catch (err) {
    return { status: "error", message: String(err) };
  }
}

async function postLoraInfo(action, lora) {
  try {
    const resp = await fetch(`${INFO_ROUTE}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lora }),
    });
    return (await resp.json().catch(() => null)) || { status: "error", message: `HTTP ${resp.status}` };
  } catch (err) {
    return { status: "error", message: String(err) };
  }
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    // fall through to the legacy path (the async clipboard API needs a secure context)
  }
  const ta = el("textarea", { position: "fixed", opacity: "0" });
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  ta.remove();
  return ok;
}

let activeInfoDialog = null;

function closeInfoDialog() {
  if (!activeInfoDialog) return;
  activeInfoDialog.close();
  activeInfoDialog = null;
}

function openInfoDialog(node, rowWidget) {
  closeInfoDialog();
  const lora = rowWidget.value.lora;

  const overlay = el("div", {
    position: "fixed",
    inset: "0",
    background: "rgba(0,0,0,0.55)",
    zIndex: "10000",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  });
  const panel = el("div", {
    background: "#222",
    color: "#ddd",
    border: "1px solid #555",
    borderRadius: "8px",
    padding: "16px",
    width: "min(520px, 92vw)",
    maxHeight: "80vh",
    overflowY: "auto",
    font: "13px Arial, sans-serif",
    boxShadow: "0 8px 32px rgba(0,0,0,0.6)",
  });
  overlay.appendChild(panel);

  panel.appendChild(el("div", { fontSize: "15px", fontWeight: "bold", wordBreak: "break-all" }, lora.split("/").pop()));
  panel.appendChild(el("div", { color: "#888", fontSize: "11px", marginBottom: "10px", wordBreak: "break-all" }, lora));

  const meta = el("div", { marginBottom: "10px", lineHeight: "1.6" });
  const wordsHeader = el("div", {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "6px",
  });
  wordsHeader.appendChild(el("span", { fontWeight: "bold" }, "Trigger words"));
  const copyAllBtn = styledButton("Copy all");
  wordsHeader.appendChild(copyAllBtn);
  const chips = el("div", { display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "10px" });
  const status = el("div", { minHeight: "18px", color: "#aaa", fontSize: "12px", marginBottom: "10px" });
  const buttons = el("div", { display: "flex", gap: "8px", justifyContent: "flex-end" });
  const fetchBtn = styledButton("Fetch from Civitai");
  const clearBtn = styledButton("Clear");
  const closeBtn = styledButton("Close");
  buttons.append(fetchBtn, clearBtn, closeBtn);
  panel.append(meta, wordsHeader, chips, status, buttons);

  let currentWords = [];
  let hasData = false;

  const setStatus = (msg, isError = false) => {
    status.textContent = msg;
    status.style.color = isError ? "#e08080" : "#aaa";
  };
  const setBusy = (busy) => {
    fetchBtn.disabled = busy;
    fetchBtn.textContent = busy ? "Working\u2026" : "Fetch from Civitai";
    clearBtn.disabled = busy || !hasData;
  };

  function render(info) {
    currentWords = info?.trainedWords || [];
    const civ = info?.civitai;
    hasData = !!civ || currentWords.length > 0;

    meta.textContent = "";
    const addLine = (label, value) => {
      if (!value) return;
      const line = el("div");
      line.appendChild(el("span", { color: "#888" }, `${label}: `));
      line.appendChild(el("span", null, String(value)));
      meta.appendChild(line);
    };
    if (civ) {
      addLine("Model", civ.name);
      addLine("Version", civ.versionName);
      addLine("Base model", civ.baseModel);
      if (typeof civ.link === "string" && /^https:\/\/civitai\.(red|com)\//.test(civ.link)) {
        const line = el("div");
        const a = el("a", { color: "#8fce00" }, "Open on Civitai");
        a.href = civ.link;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        line.appendChild(a);
        meta.appendChild(line);
      }
    } else {
      meta.appendChild(el("div", { color: "#888" }, "No Civitai info saved for this lora yet."));
    }

    chips.textContent = "";
    if (!currentWords.length) chips.appendChild(el("div", { color: "#888" }, "No trigger words saved."));
    for (const word of currentWords) {
      const chip = el(
        "span",
        {
          background: "#333",
          border: "1px solid #666",
          borderRadius: "10px",
          padding: "2px 9px",
          cursor: "pointer",
        },
        word
      );
      chip.title = "Click to copy";
      chip.addEventListener("click", async () => {
        setStatus((await copyText(word)) ? `Copied "${word}"` : "Copy failed", false);
      });
      chips.appendChild(chip);
    }

    copyAllBtn.disabled = !currentWords.length;
    clearBtn.disabled = !hasData;
    if (info) {
      // keep the row's badge in sync with what's actually saved on disk
      rowWidget._hasWords = currentWords.length > 0;
      node.setDirtyCanvas(true, true);
    }
  }

  copyAllBtn.addEventListener("click", async () => {
    setStatus((await copyText(currentWords.join(", "))) ? "Copied all trigger words." : "Copy failed", false);
  });

  fetchBtn.addEventListener("click", async () => {
    setBusy(true);
    setStatus("Hashing the file and contacting Civitai (large files can take a few seconds)\u2026");
    const result = await postLoraInfo("fetch", lora);
    setBusy(false);
    if (result.status === "ok") {
      render(result);
      setStatus(
        currentWords.length
          ? `Saved ${currentWords.length} trigger word(s).`
          : "Found on Civitai, but it lists no trigger words."
      );
    } else {
      setStatus(result.message || "Fetch failed.", result.status !== "not_found");
    }
  });

  clearBtn.addEventListener("click", async () => {
    if (!window.confirm(`Remove the saved Civitai info for ${lora.split("/").pop()}?`)) return;
    setBusy(true);
    setStatus("Clearing\u2026");
    const result = await postLoraInfo("clear", lora);
    setBusy(false);
    if (result.status === "ok") {
      render(result);
      setStatus("Cleared.");
    } else {
      setStatus(result.message || "Clear failed.", true);
    }
  });

  const onKey = (e) => {
    if (e.key === "Escape") closeInfoDialog();
  };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeInfoDialog();
  });
  closeBtn.addEventListener("click", closeInfoDialog);
  activeInfoDialog = {
    close() {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
    },
  };
  document.body.appendChild(overlay);

  render(null);
  setStatus("Loading\u2026");
  getLoraInfo(lora).then((result) => {
    if (result.status === "ok") {
      render(result);
      setStatus("");
    } else {
      setStatus(result.message || "Could not load info.", true);
    }
  });
}

function drawArrow(ctx, cx, cy, size, direction) {
  // direction: -1 points left, 1 points right
  ctx.beginPath();
  ctx.moveTo(cx - (direction * size) / 2, cy - size / 2);
  ctx.lineTo(cx - (direction * size) / 2, cy + size / 2);
  ctx.lineTo(cx + (direction * size) / 2, cy);
  ctx.closePath();
  ctx.fill();
}

function makeLoraRowWidget(node, name, value) {
  const widget = {
    name,
    type: "MAGIK_LORA_FOLDER_ROW",
    value, // {on, lora, strength}
    computeSize(width) {
      return [width, ROW_HEIGHT];
    },
    serializeValue() {
      return this.value;
    },
    draw(ctx, drawNode, widgetWidth, y, widgetHeight) {
      const H = widgetHeight || ROW_HEIGHT;
      const margin = 8;
      const boxSize = 14;
      const arrowZone = 16;
      const valueZone = 44;
      const strengthAreaWidth = arrowZone * 2 + valueZone;
      const infoZone = 20;

      ctx.save();

      ctx.fillStyle = "rgba(255,255,255,0.04)";
      ctx.fillRect(margin, y + 1, widgetWidth - margin * 2, H - 3);

      // on/off checkbox
      const boxX = margin + 4;
      const boxY = y + (H - boxSize) / 2;
      ctx.strokeStyle = "#999";
      ctx.lineWidth = 1;
      ctx.strokeRect(boxX, boxY, boxSize, boxSize);
      if (this.value.on) {
        ctx.fillStyle = "#8fce00";
        ctx.fillRect(boxX + 2, boxY + 2, boxSize - 4, boxSize - 4);
      }
      this._boxHit = { x: boxX, y: boxY, w: boxSize, h: boxSize };

      // filename label (ellipsized to fit)
      const labelX = boxX + boxSize + 8;
      const labelMaxWidth = widgetWidth - margin * 2 - boxSize - strengthAreaWidth - infoZone - 20;
      ctx.font = "12px Arial";
      ctx.textBaseline = "middle";
      ctx.fillStyle = this.value.on ? "#dddddd" : "#777777";
      const fullLabel = (this.value.lora || "").split("/").pop() || "";
      let label = fullLabel;
      while (label.length > 4 && ctx.measureText(label).width > labelMaxWidth) {
        label = label.slice(0, -2);
      }
      if (label !== fullLabel) label += "\u2026";
      ctx.fillText(label, labelX, y + H / 2);

      // info button: filled green when this lora already has trigger words saved, dim outline
      // otherwise. Clicking it opens the info dialog (see openInfoDialog).
      const infoCX = widgetWidth - margin - strengthAreaWidth - infoZone / 2;
      const infoCY = y + H / 2;
      ctx.beginPath();
      ctx.arc(infoCX, infoCY, 7, 0, Math.PI * 2);
      if (this._hasWords) {
        ctx.fillStyle = "#8fce00";
        ctx.fill();
      } else {
        ctx.strokeStyle = "#777777";
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      ctx.fillStyle = this._hasWords ? "#1a1a1a" : "#999999";
      ctx.font = "bold 11px Arial";
      ctx.textAlign = "center";
      ctx.fillText("i", infoCX, infoCY + 0.5);
      ctx.textAlign = "left";
      ctx.font = "12px Arial";
      this._infoHit = { x: infoCX - 8, y: y + 2, w: 16, h: H - 5 };

      // strength stepper: [ < ] [ value ] [ > ]
      const strengthX = widgetWidth - margin - strengthAreaWidth;
      const strengthY = y + 2;
      const strengthH = H - 5;
      const midY = y + H / 2;

      ctx.fillStyle = "rgba(0,0,0,0.3)";
      ctx.fillRect(strengthX, strengthY, strengthAreaWidth, strengthH);
      ctx.strokeStyle = "#555";
      ctx.strokeRect(strengthX, strengthY, strengthAreaWidth, strengthH);

      ctx.fillStyle = this.value.on ? "#bbbbbb" : "#666666";
      drawArrow(ctx, strengthX + arrowZone / 2, midY, 8, -1);
      drawArrow(ctx, strengthX + arrowZone + valueZone + arrowZone / 2, midY, 8, 1);

      ctx.fillStyle = this.value.on ? "#ffffff" : "#777777";
      ctx.textAlign = "center";
      ctx.fillText(Number(this.value.strength).toFixed(2), strengthX + arrowZone + valueZone / 2, midY);
      ctx.textAlign = "left";

      this._decHit = { x: strengthX, y: strengthY, w: arrowZone, h: strengthH };
      this._valHit = { x: strengthX + arrowZone, y: strengthY, w: valueZone, h: strengthH };
      this._incHit = { x: strengthX + arrowZone + valueZone, y: strengthY, w: arrowZone, h: strengthH };

      ctx.restore();
    },
    mouse(event, pos, mouseNode) {
      const [x, y] = pos;
      const type = event.type || "";
      const within = (hit) =>
        hit && x >= hit.x && x <= hit.x + hit.w && y >= hit.y - 3 && y <= hit.y + hit.h + 3;

      if (type.endsWith("down")) {
        if (within(this._boxHit)) {
          this.value.on = !this.value.on;
          mouseNode.setDirtyCanvas(true, true);
          return true;
        }
        if (within(this._infoHit)) {
          openInfoDialog(mouseNode, this);
          return true;
        }
        if (within(this._decHit)) {
          this.value.strength = Math.round((this.value.strength - STRENGTH_STEP) * 100) / 100;
          mouseNode.setDirtyCanvas(true, true);
          return true;
        }
        if (within(this._incHit)) {
          this.value.strength = Math.round((this.value.strength + STRENGTH_STEP) * 100) / 100;
          mouseNode.setDirtyCanvas(true, true);
          return true;
        }
        if (within(this._valHit)) {
          this._dragging = true;
          this._movedWhileDragging = false;
          this._lastDragX = x;
          return true;
        }
      } else if (type.endsWith("move")) {
        if (this._dragging) {
          const dx = typeof event.deltaX === "number" ? event.deltaX : x - this._lastDragX;
          this._lastDragX = x;
          if (dx) {
            this._movedWhileDragging = true;
            this.value.strength = Math.round((this.value.strength + dx * STRENGTH_DRAG_SCALE) * 100) / 100;
            mouseNode.setDirtyCanvas(true, true);
          }
          return true;
        }
      } else if (type.endsWith("up")) {
        if (this._dragging) {
          this._dragging = false;
          if (!this._movedWhileDragging) {
            // A plain click (no drag) on the value -- prompt for an exact number, same as
            // rgthree's click-vs-drag distinction.
            const entered = window.prompt("Strength", this.value.strength);
            const parsed = entered === null ? null : parseFloat(entered);
            if (parsed !== null && !Number.isNaN(parsed)) {
              this.value.strength = parsed;
              mouseNode.setDirtyCanvas(true, true);
            }
          }
          return true;
        }
      }
      return false;
    },
  };

  node.addCustomWidget(widget);
  return widget;
}

async function refreshLoraRows(node, explicitSavedRows) {
  const folderWidget = node.widgets?.find((w) => w.name === "lora_folder");
  const folder = folderWidget ? folderWidget.value : ".";
  // Guard against out-of-order responses: only the newest refresh may touch the rows.
  const token = (node._magikRefreshToken = (node._magikRefreshToken || 0) + 1);
  const fetched = await fetchLorasForFolder(folder);
  if (token !== node._magikRefreshToken) return;
  const { hasWords } = fetched;
  // Non-recursive: keep only files whose direct parent directory is exactly the chosen folder.
  const loras = fetched.loras.filter((p) => {
    const slash = p.replace(/\\/g, "/").lastIndexOf("/");
    const parent = slash === -1 ? "." : p.replace(/\\/g, "/").slice(0, slash);
    return parent === folder;
  });

  // Preserve on/strength state, matched by lora filename. On initial load this comes from the
  // saved workflow (explicitSavedRows); on a live folder switch or manual rescan it comes from
  // whatever's currently on screen.
  const priorSource = explicitSavedRows || (node.loraRowWidgets || []).map((w) => w.value);
  const savedByName = {};
  for (const row of priorSource) {
    if (row && row.lora) savedByName[row.lora] = row;
  }

  removeRowWidgets(node);

  node.loraRowWidgets = loras.map((loraPath, i) => {
    const prior = savedByName[loraPath];
    const value = {
      on: prior ? !!prior.on : true,
      lora: loraPath,
      strength: prior ? prior.strength : 1.0,
    };
    const widget = makeLoraRowWidget(node, `lora_${i + 1}`, value);
    widget._hasWords = !!hasWords[loraPath]; // drives the info-button badge (not serialized)
    return widget;
  });

  if (node.computeSize) {
    const size = node.computeSize();
    node.size = [Math.max(node.size?.[0] || 0, size[0]), Math.max(size[1], node.size?.[1] || 0)];
  }
  node.setDirtyCanvas(true, true);
}

app.registerExtension({
  name: "Magik.PowerLoraFolderLoader",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_TYPE) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);

      this.serialize_widgets = true;
      this.loraRowWidgets = [];
      this._magikConfigured = false;

      const folderWidget = this.widgets?.find((w) => w.name === "lora_folder");
      if (folderWidget) {
        const origCallback = folderWidget.callback;
        folderWidget.callback = (...args) => {
          origCallback?.apply(folderWidget, args);
          refreshLoraRows(this);
        };
      }

      this.addWidget("button", "\uD83D\uDD04 Rescan Folder", null, () => refreshLoraRows(this));

      // Defer: if this node is being created as part of a workflow load, onConfigure() fires
      // right after and calls refreshLoraRows() itself with the saved row state applied. If
      // not (a fresh node dragged from the menu), this fallback populates default rows.
      setTimeout(() => {
        if (!this._magikConfigured) refreshLoraRows(this);
      }, 32);

      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const r = onConfigure?.apply(this, arguments);
      this._magikConfigured = true;
      refreshLoraRows(this, extractSavedLoraRows(info?.widgets_values));
      return r;
    };
  },
});
