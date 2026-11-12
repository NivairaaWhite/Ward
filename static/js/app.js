(() => {
  "use strict";

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const galleryGrid = document.getElementById("gallery-grid");
  const galleryEmpty = document.getElementById("gallery-empty");
  const galleryCount = document.getElementById("gallery-count");
  const detailOverlay = document.getElementById("detail-overlay");
  const detailPanel = document.getElementById("detail-panel");
  const settingsBtn = document.getElementById("settings-btn");
  const settingsOverlay = document.getElementById("settings-overlay");
  const settingsPanel = document.getElementById("settings-panel");
  const toast = document.getElementById("toast");

  let trackerConfigured = window.WARD_TRACKER_CONFIGURED === true;
  let images = []; // summaries, newest first
  let toastTimer = null;
  let currentDetailId = null; // which image's panel is open, so a slow scan can't paint over another
  let lastFocus = null;

  // --------------------------------------------------------------- utils
  function showToast(message, isError = false) {
    toast.textContent = message;
    toast.style.borderColor = isError ? "var(--rust)" : "var(--brass)";
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  // Match links come from third parties: only ever allow http(s) URLs.
  function safeUrl(u) {
    return /^https?:\/\//i.test(u || "") ? u : "#";
  }

  function timeAgo(isoString) {
    if (!isoString) return null;
    const then = new Date(isoString).getTime();
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(isoString).toLocaleDateString();
  }

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    let body = null;
    try { body = await res.json(); } catch (_e) { /* no body */ }
    if (!res.ok) {
      const message = (body && body.error) || `Request failed (${res.status})`;
      throw new Error(message);
    }
    return body;
  }

  // ------------------------------------------------------------- upload
  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    dropzone.classList.add("busy");
    let added = 0;
    let lastError = null;
    for (const file of list) {
      const formData = new FormData();
      formData.append("image", file);
      try {
        const record = await api("/api/images", { method: "POST", body: formData });
        images.unshift(record);
        renderGallery();
        added += 1;
      } catch (err) {
        lastError = `${file.name}: ${err.message}`;
      }
    }
    dropzone.classList.remove("busy");
    if (lastError) {
      showToast(added ? `Added ${added}; ${lastError}` : lastError, true);
    } else {
      showToast(added === 1 ? `Added "${images[0].original_filename}" to your ledger.` : `Added ${added} images to your ledger.`);
    }
  }

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", () => {
    uploadFiles(fileInput.files);
    fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag-over"); });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag-over"); });
  });
  dropzone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
  // A file dropped anywhere else would make the browser navigate away from Ward.
  ["dragover", "drop"].forEach((evt) => window.addEventListener(evt, (e) => e.preventDefault()));

  // ------------------------------------------------------------- gallery
  function renderGallery() {
    galleryCount.textContent = images.length ? `${images.length} tracked` : "";
    galleryEmpty.hidden = images.length > 0;
    galleryGrid.querySelectorAll(".ward-card").forEach((el) => el.remove());

    for (const img of images) {
      const card = document.createElement("div");
      card.className = "ward-card";
      card.dataset.id = img.id;
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", `Open ${img.original_filename || img.id}`);
      card.innerHTML = `
        <div class="ward-card-thumb">
          ${img.thumb_url ? `<img src="${escapeHtml(img.thumb_url)}" alt="" loading="lazy">` : ""}
        </div>
        <div class="ward-card-body">
          <div class="ward-card-code">${escapeHtml(img.id)}</div>
          <span class="ward-card-status ${img.scan_count ? "status-scanned" : "status-pending"}">${img.scan_count ? "scanned " + escapeHtml(timeAgo(img.last_scanned_at)) : "not scanned yet"}</span>
        </div>
        ${img.match_count ? `<div class="ward-card-matches" title="${img.match_count} distinct pages">${img.match_count}</div>` : ""}
        ${img.new_count ? `<div class="ward-card-new">${img.new_count} new</div>` : ""}
      `;
      card.addEventListener("click", () => openDetail(img.id, card));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(img.id, card); }
      });
      galleryGrid.appendChild(card);
    }
  }

  async function loadGallery() {
    try {
      images = await api("/api/images");
      renderGallery();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // -------------------------------------------------------------- detail
  function confidenceClass(c) {
    if (c == null) return "confidence-unknown";
    if (c >= 85) return "confidence-high";
    if (c >= 55) return "confidence-mid";
    return "confidence-low";
  }

  function matchRow(m) {
    const thumb = m.thumbnail || m.image_url || "";
    const conf = m.confidence != null ? Number(m.confidence) : null;
    const confBadge = conf != null
      ? `<span class="tag-confidence ${confidenceClass(conf)}" title="Rank-based confidence from Google Lens ordering">${conf}%</span>`
      : "";
    return `
      <a class="match-item" href="${escapeHtml(safeUrl(m.link))}" target="_blank" rel="noopener noreferrer">
        ${thumb ? `<img class="match-thumb" src="${escapeHtml(safeUrl(thumb))}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="match-thumb"></div>`}
        <div class="match-body">
          <div class="match-title">${m.is_new ? `<span class="tag-new">new</span> ` : ""}${escapeHtml(m.title || m.source || "Untitled page")}</div>
          <div class="match-source">${escapeHtml(m.source || "")}</div>
        </div>
        ${confBadge}
      </a>
    `;
  }

  function renderDetail(record) {
    // Exact matches only; sort highest confidence first when available
    const matches = (record.matches || []).slice().sort((a, b) => {
      const ca = a.confidence != null ? a.confidence : -1;
      const cb = b.confidence != null ? b.confidence : -1;
      return cb - ca;
    });
    detailPanel.innerHTML = `
      <button class="detail-close" id="detail-close" aria-label="Close">&times;</button>
      ${record.file_url ? `<img class="detail-preview" src="${record.file_url}" alt="Preview of ${escapeHtml(record.original_filename || record.id)}">` : ""}
      <div class="detail-code">${escapeHtml(record.id)}</div>
      <div class="detail-filename">${escapeHtml(record.original_filename || "")} · ${record.width || "?"}&times;${record.height || "?"}</div>
      <dl class="detail-stats">
        <dt>Times scanned</dt><dd>${record.scan_count || 0}</dd>
        <dt>Last scanned</dt><dd>${timeAgo(record.last_scanned_at) || "never"}</dd>
      </dl>
      ${record.scan_count > 1 ? `<p class="scan-summary">${record.new_count ? `${record.new_count} new since the previous scan.` : "Nothing new since the previous scan."}</p>` : ""}
      <div class="btn-row">
        <button class="btn" id="scan-btn" ${trackerConfigured ? "" : "disabled title=\"Add a SerpApi key in Settings first\""}>Check for sightings</button>
        ${record.file_url ? `<a class="btn btn-secondary" href="${record.file_url}" download>Download</a>` : ""}
        ${matches.length ? `<a class="btn btn-secondary" href="/api/images/${encodeURIComponent(record.id)}/export.csv">Export CSV</a>` : ""}
        <button class="btn btn-secondary" id="delete-btn">Delete</button>
      </div>
      <p class="section-label">Exact matches (${matches.length} distinct page${matches.length === 1 ? "" : "s"})</p>
      <div class="matches-list" id="matches-list">
        ${matches.length ? matches.map(matchRow).join("") : `<p class="empty-note">No exact matches found yet.</p>`}
      </div>
    `;

    detailPanel.querySelector("#detail-close").addEventListener("click", closeDetail);

    const scanBtn = detailPanel.querySelector("#scan-btn");
    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        scanBtn.disabled = true;
        scanBtn.textContent = "Searching…";
        try {
          const result = await api(`/api/images/${record.id}/track`, { method: "POST" });
          const fresh = await api(`/api/images/${record.id}`);
          const idx = images.findIndex((i) => i.id === record.id);
          if (idx >= 0) images[idx] = fresh;
          renderGallery();
          // The user may have closed this panel or opened another image while
          // the scan ran; only repaint if this image is still the one on screen.
          if (currentDetailId === record.id) renderDetail(fresh);
          const total = fresh.matches.length;
          showToast(
            total === 0 ? "No matches found."
              : fresh.scan_count > 1 ? `${result.new_count} new, ${total} total.`
              : `Found ${total} match${total === 1 ? "" : "es"}.`
          );
        } catch (err) {
          showToast(err.message, true);
          if (scanBtn.isConnected) {
            scanBtn.disabled = false;
            scanBtn.textContent = "Check for sightings";
          }
        }
      });
    }

    detailPanel.querySelector("#delete-btn").addEventListener("click", async () => {
      if (!confirm("Remove this image from Ward? This can't be undone.")) return;
      try {
        await api(`/api/images/${record.id}`, { method: "DELETE" });
        images = images.filter((i) => i.id !== record.id);
        renderGallery();
        closeDetail();
        showToast("Removed.");
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  async function openDetail(id, trigger) {
    currentDetailId = id;
    lastFocus = trigger || document.activeElement;
    try {
      const record = await api(`/api/images/${id}`);
      if (currentDetailId !== id) return; // user already moved on
      renderDetail(record);
      detailOverlay.hidden = false;
      const close = detailPanel.querySelector("#detail-close");
      if (close) close.focus();
    } catch (err) {
      if (currentDetailId === id) currentDetailId = null;
      showToast(err.message, true);
    }
  }

  function closeDetail() {
    if (detailOverlay.hidden && currentDetailId === null) return;
    currentDetailId = null;
    detailOverlay.hidden = true;
    detailPanel.innerHTML = "";
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeDetail(); });

  // ------------------------------------------------------------ settings
  async function openSettings() {
    let current = { has_serpapi_override: false };
    try { current = await api("/api/settings"); } catch (_e) { /* use default */ }

    settingsPanel.innerHTML = `
      <button class="detail-close" id="settings-close" aria-label="Close">&times;</button>
      <p class="section-label">Settings</p>
      <div class="settings-block">
        <h3>Reverse-image search</h3>
        <p class="empty-note" style="margin:-4px 0 12px;">
          Ward uses <a href="https://serpapi.com/" target="_blank" rel="noopener noreferrer" style="color:var(--brass-bright)">SerpApi</a>'s
          Google Lens API to check where an image appears online. Paste a key here, or set
          <code>SERPAPI_API_KEY</code> in your <code>.env</code> file instead.
        </p>
        <div class="settings-field">
          <label for="serpapi-key">SerpApi key ${current.has_serpapi_override ? "(currently set — leave blank to keep it)" : ""}</label>
          <input type="password" id="serpapi-key" autocomplete="off" spellcheck="false" placeholder="${current.has_serpapi_override ? "••••••••••••" : "Paste your SerpApi key"}">
        </div>
        <div class="btn-row" style="margin-top:14px;margin-bottom:0;">
          <button class="btn" id="save-settings-btn">Save</button>
          ${current.has_serpapi_override ? `<button class="btn btn-secondary" id="clear-settings-btn">Clear saved key</button>` : ""}
        </div>
      </div>
    `;

    settingsPanel.querySelector("#settings-close").addEventListener("click", closeSettings);

    settingsPanel.querySelector("#save-settings-btn").addEventListener("click", async () => {
      const value = settingsPanel.querySelector("#serpapi-key").value.trim();
      if (!value) { showToast("Enter a key first, or use Clear to remove the saved one.", true); return; }
      try {
        await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ serpapi_key_override: value }),
        });
        await refreshConfig();
        showToast("Saved.");
        closeSettings();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    const clearBtn = settingsPanel.querySelector("#clear-settings-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", async () => {
        try {
          await api("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ serpapi_key_override: "" }),
          });
          await refreshConfig();
          showToast("Cleared.");
          closeSettings();
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }

    lastFocus = document.activeElement;
    settingsOverlay.hidden = false;
    const keyInput = settingsPanel.querySelector("#serpapi-key");
    if (keyInput) keyInput.focus();
  }

  function closeSettings() {
    if (settingsOverlay.hidden) return;
    settingsOverlay.hidden = true;
    settingsPanel.innerHTML = "";
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }

  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeSettings(); });
  settingsBtn.addEventListener("click", openSettings);

  async function refreshConfig() {
    try {
      const cfg = await api("/api/config");
      trackerConfigured = !!cfg.tracker_configured;
    } catch (_e) { /* keep previous value */ }
  }

  // ---------------------------------------------------------------- init
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!settingsOverlay.hidden) closeSettings();
      else closeDetail();
    }
  });

  loadGallery();
})();
