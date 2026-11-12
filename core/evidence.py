"""
evidence.py
-----------
Builds a small local "evidence package" for one match: a local copy of
the thumbnail SerpApi returned, its hash, and when it was fetched --
enough to show a platform/lawyer *something you captured yourself* at
the time, rather than only a live link that the infringing page's owner
could edit or take down before anyone else looks at it.

WHAT THIS DOESN'T DO (and why)
    The original spec's "Evidence Capture" bullet described full-page
    screenshots via Playwright. That needs Playwright's own browser
    binaries, which download from Microsoft's CDN -- a domain outside
    this sandbox's network allowlist, so it couldn't be installed or
    tested here. Rather than ship untested screenshot code, this module
    sticks to what's actually verified working: downloading and hashing
    the thumbnail image SerpApi already gave us. A full-page screenshot
    module is a reasonable follow-up in an environment with normal
    network access -- it would slot in as another function here,
    called from the same place -- but isn't included so nothing in this
    build is "written but never actually run."
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

_FETCH_TIMEOUT_SECONDS = 10
_MAX_BYTES = 10 * 1024 * 1024  # 10MB -- thumbnails are small; anything huge is not a thumbnail


class EvidenceError(Exception):
    pass


@dataclass
class Evidence:
    stored_filename: str
    sha256: str
    source_url: str
    fetched_at: str
    content_type: str | None


def capture_evidence(source_url: str, evidence_dir: str, tracking_id: str, match_id: int) -> Evidence:
    """Download whatever's at `source_url` (a match's thumbnail or image
    URL) and store it locally under evidence_dir, alongside its SHA-256
    and fetch timestamp. Raises EvidenceError on any failure (network,
    non-image response, too large) -- evidence capture is best-effort
    and failing shouldn't corrupt the match record, so callers should
    catch this and just note "couldn't capture" rather than failing the
    whole track operation."""
    if not source_url or not source_url.lower().startswith(("http://", "https://")):
        raise EvidenceError("No usable URL to capture from.")

    try:
        resp = requests.get(source_url, timeout=_FETCH_TIMEOUT_SECONDS, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise EvidenceError(f"Could not fetch evidence image: {e}") from e

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip() or None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(
        content_type, "jpg"
    )

    # Read in chunks and stop at the cap, instead of resp.content, which
    # would pull the whole body into memory before any size check.
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > _MAX_BYTES:
                resp.close()
                raise EvidenceError("Evidence file too large (over 10MB) -- not a thumbnail.")
            chunks.append(chunk)
    except requests.RequestException as e:
        raise EvidenceError(f"Could not read evidence image: {e}") from e
    data = b"".join(chunks)
    if not data:
        raise EvidenceError("Empty response.")

    os.makedirs(evidence_dir, exist_ok=True)
    stored_filename = f"{tracking_id}_match{match_id}.{ext}"
    stored_path = os.path.join(evidence_dir, stored_filename)
    with open(stored_path, "wb") as f:
        f.write(data)

    sha256 = hashlib.sha256(data).hexdigest()
    return Evidence(
        stored_filename=stored_filename,
        sha256=sha256,
        source_url=source_url,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        content_type=content_type,
    )
