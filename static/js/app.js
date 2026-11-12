(() => {
  "use strict";

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const modeSelect = document.getElementById("mode-select");
  const strengthSelect = document.getElementById("strength-select");
  const queuePanel = document.getElementById("queue-panel");
  const queueRow = document.getElementById("queue-row");
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
  let activeJobs = {}; // job_id -> { filename }
  let toastTimer = null;
  let caseTypes = ["copyright", "ncii", "generic"];
  let caseStatuses = ["drafted", "sent", "acknowledged", "removed", "rejected"];

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

  async function loadConfig() {
    try {
      const cfg = await api("/api/config");
      trackerConfigured = !!cfg.tracker_configured;
      if (Array.isArray(cfg.case_types)) caseTypes = cfg.case_types;
      if (Array.isArray(cfg.case_statuses)) caseStatuses = cfg.case_statuses;
    } catch (_e) { /* keep previous values */ }
  }

  // ------------------------------------------------------------- upload
  function queueRender() {
    const ids = Object.keys(activeJobs);
    queuePanel.hidden = ids.length === 0;
    queueRow.innerHTML = ids.map((jobId) => `
      <div class="queue-item" data-job="${jobId}">
        <div class="queue-spinner"></div>
        <span>${escapeHtml(activeJobs[jobId].filename)} — ${escapeHtml(activeJobs[jobId].message || "Queued")}</span>
      </div>
    `).join("");
  }

  async function pollJob(jobId) {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (activeJobs[jobId]) activeJobs[jobId].message = job.message;
      queueRender();
      if (job.state === "done") {
        delete activeJobs[jobId];
        queueRender();
        await loadGallery();
        showToast("Protected and added to your ledger.");
      } else if (job.state === "failed") {
        delete activeJobs[jobId];
        queueRender();
        await loadGallery();
        showToast(`Failed: ${job.error || "unknown error"}`, true);
      } else {
        setTimeout(() => pollJob(jobId), 700);
      }
    } catch (err) {
      delete activeJobs[jobId];
      queueRender();
      showToast(err.message, true);
    }
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList);
    if (!files.length) return;
    const mode = modeSelect.value;
    const strength = strengthSelect.value;

    if (files.length === 1) {
      const formData = new FormData();
      formData.append("image", files[0]);
      formData.append("mode", mode);
      formData.append("strength", strength);
      try {
        const res = await api("/api/protect", { method: "POST", body: formData });
        activeJobs[res.job_id] = { filename: files[0].name, message: "Queued" };
        queueRender();
        pollJob(res.job_id);
      } catch (err) {
        showToast(err.message, true);
      }
      return;
    }

    const formData = new FormData();
    files.forEach((f) => formData.append("images", f));
    formData.append("mode", mode);
    formData.append("strength", strength);
    try {
      const res = await api("/api/protect/bulk", { method: "POST", body: formData });
      res.submitted.forEach((s) => { activeJobs[s.job_id] = { filename: s.filename, message: "Queued" }; });
      queueRender();
      res.submitted.forEach((s) => pollJob(s.job_id));
      if (res.errors && res.errors.length) {
        showToast(`${res.errors.length} file(s) skipped: ${res.errors[0].error}`, true);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", () => { uploadFiles(fileInput.files); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag-over"); });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag-over"); });
  });
  dropzone.addEventListener("drop", (e) => { uploadFiles(e.dataTransfer.files); });

  // ------------------------------------------------------------- gallery
  function renderGallery() {
    galleryCount.textContent = images.length ? `${images.length} protected` : "";
    galleryEmpty.hidden = images.length > 0;
    galleryGrid.querySelectorAll(".ward-card").forEach((el) => el.remove());

    for (const img of images) {
      const card = document.createElement("div");
      card.className = "ward-card";
      card.dataset.id = img.id;
      const statusClass = img.status === "protected" ? "status-protected"
        : img.status === "failed" ? "status-failed" : "status-processing";
      card.innerHTML = `
        <div class="ward-card-thumb">
          ${img.thumb_url ? `<img src="${img.thumb_url}" alt="">` : ""}
        </div>
        <div class="ward-card-body">
          <div class="ward-card-code">${escapeHtml(img.id)}</div>
          <span class="ward-card-status ${statusClass}">${escapeHtml(img.status)}</span>
        </div>
        ${img.match_count ? `<div class="ward-card-matches">${img.match_count}</div>` : ""}
      `;
      card.addEventListener("click", () => openDetail(img.id));
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
  function confidenceLabel(score) {
    if (score == null) return "";
    const pct = Math.round(score * 100);
    const cls = pct >= 80 ? "conf-high" : pct >= 50 ? "conf-mid" : "conf-low";
    return `<span class="confidence-badge ${cls}">${pct}% hash match</span>`;
  }

  // Links and image URLs come from third-party search results: only ever
  // allow plain http(s) into an href/src (blocks javascript: and data:).
  function safeUrl(url) {
    return /^https?:\/\//i.test(url || "") ? url : "";
  }

  function matchRow(m, record) {
    const evidenceImg = m.evidence_url || safeUrl(m.thumbnail) || safeUrl(m.image_url) || "";
    return `
      <div class="match-item-row" data-match-id="${m.id}">
        <a class="match-item" href="${escapeHtml(safeUrl(m.link) || "#")}" target="_blank" rel="noopener noreferrer">
          ${evidenceImg ? `<img class="match-thumb" src="${escapeHtml(evidenceImg)}" alt="">` : `<div class="match-thumb"></div>`}
          <div>
            <div class="match-title">${escapeHtml(m.title || m.source || "Untitled page")}</div>
            <div class="match-source">${escapeHtml(m.source || "")} ${confidenceLabel(m.confidence_score)}</div>
          </div>
        </a>
        <button class="btn-mini open-case-btn" data-match-id="${m.id}" type="button">Open a case</button>
      </div>
      <div class="case-form-slot" data-match-slot="${m.id}"></div>
    `;
  }

  function caseFormHtml(match) {
    return `
      <div class="settings-block case-form">
        <h3>Open a case${match ? ` on this match` : ""}</h3>
        <div class="settings-field">
          <label>Type</label>
          <select class="case-type-select">
            ${caseTypes.map((t) => `<option value="${t}">${t === "ncii" ? "Privacy / NCII" : t.charAt(0).toUpperCase() + t.slice(1)}</option>`).join("")}
          </select>
        </div>
        <div class="settings-field case-violation-field" hidden>
          <label>What policy does this violate?</label>
          <input type="text" class="case-violation-input" placeholder="e.g. impersonation account using my art">
        </div>
        <div class="btn-row" style="margin-top:10px;margin-bottom:0;">
          <button class="btn case-submit-btn" type="button">Create case</button>
          <button class="btn btn-secondary case-cancel-btn" type="button">Cancel</button>
        </div>
      </div>
    `;
  }

  function caseCard(c) {
    const platform = c.target_platform || "(platform not set)";
    return `
      <div class="settings-block case-card" data-case-id="${c.id}">
        <div class="case-card-head">
          <span class="case-type-tag">${c.case_type === "ncii" ? "Privacy / NCII" : c.case_type}</span>
          <select class="case-status-select">
            ${caseStatuses.map((s) => `<option value="${s}" ${s === c.status ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </div>
        <p class="empty-note" style="margin:8px 0;">
          ${escapeHtml(platform)} — <a href="${escapeHtml(safeUrl(c.target_url) || "#")}" target="_blank" rel="noopener noreferrer">target link</a>
          · opened ${timeAgo(c.created_at)}
          ${c.sent_at ? ` · sent ${timeAgo(c.sent_at)}` : ""}
          ${c.resolved_at ? ` · resolved ${timeAgo(c.resolved_at)}` : ""}
        </p>
        <textarea class="case-notes-input" placeholder="Notes (follow-up plan, contact reference, etc.)">${escapeHtml(c.notes || "")}</textarea>
        <div class="btn-row" style="margin-top:10px;margin-bottom:0;">
          <a class="btn-mini" href="/api/cases/${c.id}/notice" download>Download notice</a>
          <button class="btn-mini case-followup-btn" type="button">Log follow-up</button>
          <button class="btn-mini case-delete-btn" type="button">Delete case</button>
        </div>
      </div>
    `;
  }

  function wireCaseForm(container, record, match) {
    const typeSelect = container.querySelector(".case-type-select");
    const violationField = container.querySelector(".case-violation-field");
    const violationInput = container.querySelector(".case-violation-input");
    typeSelect.addEventListener("change", () => {
      violationField.hidden = typeSelect.value !== "generic";
    });
    container.querySelector(".case-cancel-btn").addEventListener("click", () => { container.innerHTML = ""; });
    container.querySelector(".case-submit-btn").addEventListener("click", async () => {
      const body = {
        image_id: record.id,
        case_type: typeSelect.value,
        violation_description: violationInput.value || "",
      };
      if (match) body.match_id = match.id;
      try {
        await api("/api/cases", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        const fresh = await api(`/api/images/${record.id}`);
        const idx = images.findIndex((i) => i.id === record.id);
        if (idx >= 0) images[idx] = fresh;
        renderGallery();
        renderDetail(fresh);
        showToast("Case created — notice drafted below.");
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  function wireCaseCard(container, record, c) {
    container.querySelector(".case-status-select").addEventListener("change", async (e) => {
      try {
        await api(`/api/cases/${c.id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: e.target.value }),
        });
        const fresh = await api(`/api/images/${record.id}`);
        const idx = images.findIndex((i) => i.id === record.id);
        if (idx >= 0) images[idx] = fresh;
        renderGallery();
        renderDetail(fresh);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    container.querySelector(".case-notes-input").addEventListener("blur", async (e) => {
      try {
        await api(`/api/cases/${c.id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: e.target.value }),
        });
      } catch (err) {
        showToast(err.message, true);
      }
    });
    container.querySelector(".case-followup-btn").addEventListener("click", async () => {
      try {
        await api(`/api/cases/${c.id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ log_followup: true }),
        });
        showToast("Follow-up logged.");
        const fresh = await api(`/api/images/${record.id}`);
        renderDetail(fresh);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    container.querySelector(".case-delete-btn").addEventListener("click", async () => {
      if (!confirm("Delete this case? The notice text goes with it.")) return;
      try {
        await api(`/api/cases/${c.id}`, { method: "DELETE" });
        const fresh = await api(`/api/images/${record.id}`);
        const idx = images.findIndex((i) => i.id === record.id);
        if (idx >= 0) images[idx] = fresh;
        renderGallery();
        renderDetail(fresh);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  function provenanceLine(record) {
    if (!record.provenance_kind) {
      return `<p class="empty-note">Provenance: not generated.</p>`;
    }
    if (record.provenance_kind === "c2pa") {
      return `<p class="empty-note">Provenance: real C2PA manifest embedded in the file (your configured certificate).</p>`;
    }
    return `<p class="empty-note">Provenance: Ward-signed local record (not a C2PA/Content-Credentials manifest —
      see Settings for the public key to share alongside this piece if you want others to be able to verify it).</p>`;
  }

  function renderDetail(record) {
    const matches = record.matches || [];
    const cases = record.cases || [];
    detailPanel.innerHTML = `
      <button class="detail-close" id="detail-close" aria-label="Close">&times;</button>
      ${record.thumb_url ? `<img class="detail-preview" src="${record.thumb_url}" alt="">` : ""}
      <div class="detail-code">${escapeHtml(record.id)}</div>
      <div class="detail-filename">${escapeHtml(record.original_filename || "")} · ${record.width || "?"}&times;${record.height || "?"}</div>
      <dl class="detail-stats">
        <dt>Status</dt><dd>${escapeHtml(record.status)}</dd>
        <dt>Watermark</dt><dd>${escapeHtml(record.watermark_hex || "—")}</dd>
        <dt>Cloak shift</dt><dd>${record.cloak_hamming != null ? `${record.cloak_hamming}/${record.cloak_bits_total} bits` : "—"}</dd>
        <dt>Faces cloaked</dt><dd>${record.faces_detected || 0}</dd>
        <dt>GPS stripped</dt><dd>${record.gps_stripped ? "yes" : "n/a"}</dd>
        <dt>No-AI-training tag</dt><dd>${record.ai_training_opt_out_tagged ? "yes" : "no"}</dd>
        <dt>Last scanned</dt><dd>${timeAgo(record.last_scanned_at) || "never"}</dd>
      </dl>
      ${provenanceLine(record)}
      <div class="btn-row">
        <button class="btn" id="scan-btn" ${trackerConfigured ? "" : "disabled title=\"Add a SerpApi key in Settings first\""}>Check for sightings</button>
        ${record.download_url ? `<a class="btn btn-secondary" href="${record.download_url}">Download protected</a>` : ""}
        ${record.original_url ? `<a class="btn btn-secondary" href="${record.original_url}">Download original</a>` : ""}
        <button class="btn btn-secondary" id="delete-btn">Delete</button>
      </div>
      <p class="section-label">Matches (${matches.length})</p>
      <div class="matches-list" id="matches-list">
        ${matches.length ? matches.map((m) => matchRow(m, record)).join("") : `<p class="empty-note">No matches found yet.</p>`}
      </div>
      <p class="section-label">Cases (${cases.length})</p>
      <div class="cases-list" id="cases-list">
        ${cases.length ? cases.map(caseCard).join("") : `<p class="empty-note">No cases opened yet — use "Open a case" on a match above.</p>`}
        <div class="case-form-slot" data-match-slot="none"></div>
      </div>
      <div class="btn-row" style="margin-top:0;">
        <button class="btn-mini" id="freeform-case-btn" type="button">Open a freeform case (no match needed)</button>
      </div>
    `;

    detailPanel.querySelector("#detail-close").addEventListener("click", closeDetail);

    matches.forEach((m) => {
      const btn = detailPanel.querySelector(`.open-case-btn[data-match-id="${m.id}"]`);
      if (!btn) return;
      btn.addEventListener("click", () => {
        const slot = detailPanel.querySelector(`.case-form-slot[data-match-slot="${m.id}"]`);
        if (slot.innerHTML) { slot.innerHTML = ""; return; }
        slot.innerHTML = caseFormHtml(m);
        wireCaseForm(slot, record, m);
      });
    });

    cases.forEach((c) => {
      const cardEl = detailPanel.querySelector(`.case-card[data-case-id="${c.id}"]`);
      if (cardEl) wireCaseCard(cardEl, record, c);
    });

    detailPanel.querySelector("#freeform-case-btn").addEventListener("click", () => {
      const slot = detailPanel.querySelector('.case-form-slot[data-match-slot="none"]');
      if (slot.innerHTML) { slot.innerHTML = ""; return; }
      slot.innerHTML = caseFormHtml(null).replace(
        "<h3>Open a case</h3>",
        `<h3>Open a freeform case</h3><div class="settings-field"><label>Where is it?</label>
         <input type="text" class="case-url-input" placeholder="https://..."></div>`
      );
      const urlInput = slot.querySelector(".case-url-input");
      wireCaseForm(slot, record, null);
      // Freeform cases need a URL from the person, not a match -- swap in
      // a submit handler that includes it instead of the matched-case one
      // wireCaseForm just attached.
      const oldSubmitBtn = slot.querySelector(".case-submit-btn");
      const clonedBtn = oldSubmitBtn.cloneNode(true);
      oldSubmitBtn.replaceWith(clonedBtn);
      clonedBtn.addEventListener("click", async () => {
        const typeSelect = slot.querySelector(".case-type-select");
        const violationInput = slot.querySelector(".case-violation-input");
        const url = urlInput.value.trim();
        if (!url) { showToast("Enter the URL where it was found.", true); return; }
        try {
          await api("/api/cases", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              image_id: record.id, case_type: typeSelect.value, target_url: url,
              violation_description: violationInput.value || "",
            }),
          });
          const fresh = await api(`/api/images/${record.id}`);
          renderDetail(fresh);
          showToast("Case created — notice drafted below.");
        } catch (err) {
          showToast(err.message, true);
        }
      });
    });

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
          renderDetail(fresh);
          showToast(result.match_count ? `Found ${result.match_count} new match(es).` : "No new matches found.");
        } catch (err) {
          showToast(err.message, true);
          scanBtn.disabled = false;
          scanBtn.textContent = "Check for sightings";
        }
      });
    }

    detailPanel.querySelector("#delete-btn").addEventListener("click", async () => {
      if (!confirm("Remove this image from Ward, including its cases and matches? This can't be undone.")) return;
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

  async function openDetail(id) {
    try {
      const record = await api(`/api/images/${id}`);
      renderDetail(record);
      detailOverlay.hidden = false;
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function closeDetail() {
    detailOverlay.hidden = true;
    detailPanel.innerHTML = "";
  }

  detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeDetail(); });

  // ------------------------------------------------------------ settings
  async function openSettings() {
    let current = {};
    let provenanceKey = "";
    try { current = await api("/api/settings"); } catch (_e) { /* use defaults */ }
    try { provenanceKey = (await api("/api/settings/provenance-key")).public_key_b64; } catch (_e) { /* skip */ }

    settingsPanel.innerHTML = `
      <button class="detail-close" id="settings-close" aria-label="Close">&times;</button>
      <p class="section-label">Settings</p>

      <div class="settings-block">
        <h3>Your profile (used to fill in takedown notices)</h3>
        <div class="settings-field"><label>Name</label>
          <input type="text" id="s-artist-name" value="${escapeHtml(current.artist_name || "")}"></div>
        <div class="settings-field"><label>Contact email</label>
          <input type="email" id="s-contact-email" value="${escapeHtml(current.contact_email || "")}"></div>
        <div class="settings-field"><label>Portfolio URL</label>
          <input type="text" id="s-portfolio-url" value="${escapeHtml(current.portfolio_url || "")}"></div>
        <div class="settings-field"><label>Mailing address (optional, some platforms require one)</label>
          <input type="text" id="s-mailing-address" value="${escapeHtml(current.mailing_address || "")}"></div>
      </div>

      <div class="settings-block">
        <h3>Reverse-image search</h3>
        <p class="empty-note" style="margin:-4px 0 12px;">
          Uses <a href="https://serpapi.com/" target="_blank" rel="noopener noreferrer" style="color:var(--brass-bright)">SerpApi</a>'s
          Google Lens API. Paste a key here, or set <code>SERPAPI_API_KEY</code> in your <code>.env</code>.
        </p>
        <div class="settings-field">
          <label for="s-serpapi-key">SerpApi key ${current.has_serpapi_override ? "(currently set — leave blank to keep it)" : ""}</label>
          <input type="text" id="s-serpapi-key" placeholder="${current.has_serpapi_override ? "••••••••••••" : "Paste your SerpApi key"}">
        </div>
      </div>

      <div class="settings-block">
        <h3>Protection defaults</h3>
        <label class="toggle-row">
          <input type="checkbox" id="s-keep-originals" ${current.keep_originals ? "checked" : ""}>
          Keep unmarked originals (needed for "Download original")
        </label>
        <label class="toggle-row" style="margin-top:10px;">
          <input type="checkbox" id="s-no-ai-tag" ${current.tag_no_ai_training ? "checked" : ""}>
          Tag protected files "no AI training" (IPTC/PLUS Data Mining metadata)
        </label>
      </div>

      <div class="settings-block">
        <h3>Provenance</h3>
        <p class="empty-note" style="margin:-4px 0 10px;">
          Without a configured C2PA certificate (set <code>C2PA_CERT_PATH</code>/<code>C2PA_KEY_PATH</code> in
          <code>.env</code>), Ward signs a local-only record instead — not a real C2PA/Content-Credentials
          manifest, but a tamper-evident record you control. Its public key, safe to publish alongside your work:
        </p>
        <div class="settings-field">
          <input type="text" readonly value="${escapeHtml(provenanceKey)}" onclick="this.select()">
        </div>
      </div>

      <div class="btn-row" style="margin-top:14px;margin-bottom:0;">
        <button class="btn" id="save-settings-btn">Save</button>
      </div>
    `;

    settingsPanel.querySelector("#settings-close").addEventListener("click", closeSettings);
    settingsPanel.querySelector("#save-settings-btn").addEventListener("click", async () => {
      const body = {
        artist_name: settingsPanel.querySelector("#s-artist-name").value.trim(),
        contact_email: settingsPanel.querySelector("#s-contact-email").value.trim(),
        portfolio_url: settingsPanel.querySelector("#s-portfolio-url").value.trim(),
        mailing_address: settingsPanel.querySelector("#s-mailing-address").value.trim(),
        keep_originals: settingsPanel.querySelector("#s-keep-originals").checked,
        tag_no_ai_training: settingsPanel.querySelector("#s-no-ai-tag").checked,
      };
      const keyVal = settingsPanel.querySelector("#s-serpapi-key").value.trim();
      if (keyVal) body.serpapi_key_override = keyVal;
      try {
        await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        await loadConfig();
        showToast("Saved.");
        closeSettings();
      } catch (err) {
        showToast(err.message, true);
      }
    });

    settingsOverlay.hidden = false;
  }

  function closeSettings() {
    settingsOverlay.hidden = true;
    settingsPanel.innerHTML = "";
  }

  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeSettings(); });
  settingsBtn.addEventListener("click", openSettings);

  // -------------------------------------------------------------- verify
  const verifyFile = document.getElementById("verify-file");
  const verifyId = document.getElementById("verify-id");
  const verifyBtn = document.getElementById("verify-btn");
  const verifyResult = document.getElementById("verify-result");

  verifyBtn.addEventListener("click", async () => {
    if (!verifyFile.files.length) { showToast("Choose the image you want to check.", true); return; }
    const formData = new FormData();
    formData.append("image", verifyFile.files[0]);
    if (verifyId.value.trim()) formData.append("tracking_id", verifyId.value.trim());
    verifyBtn.disabled = true;
    verifyResult.textContent = "Checking…";
    try {
      const r = await api("/api/verify", { method: "POST", body: formData });
      if (r.confidence !== undefined) {
        const pct = Math.round(r.confidence * 100);
        verifyResult.textContent = r.likely_match
          ? `Likely a copy of ${r.tracking_id} (${pct}% watermark agreement).`
          : `No clear match to ${r.tracking_id} (${pct}% agreement).`;
      } else if (r.identified) {
        verifyResult.textContent = `This is a Ward-protected image: ${r.tracking_id}` +
          (r.original_filename ? ` (${r.original_filename}).` : ".");
      } else {
        verifyResult.textContent = "No Ward watermark found. If the copy was resized or cropped, " +
          "enter the tracking code above and check again.";
      }
    } catch (err) {
      verifyResult.textContent = "";
      showToast(err.message, true);
    } finally {
      verifyBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- init
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDetail(); closeSettings(); }
  });

  loadConfig();
  loadGallery();
})();
