"""
tracker.py
----------
Reverse-image-search integration via SerpApi's Google Lens API.

Requires the user's own SERPAPI_API_KEY (set in .env) -- Ward does not ship
with one, and there's a free tier (100 searches/month as of this writing;
verify current limits at https://serpapi.com/pricing before relying on it).

Flow (per SerpApi's documented Image API + Google Lens API):
  1. POST the image to https://serpapi.com/image (multipart/form-data) to
     get a short-lived image_id (expires in 10 minutes, 500KB file limit,
     JPG/PNG/WebP only) -- this lets us search a *local* file without first
     hosting it at a public URL.
  2. GET https://serpapi.com/search.json?engine=google_lens&image_id=...
     to run the actual visual search and get back visual_matches.

"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import requests
from PIL import Image

SERPAPI_IMAGE_UPLOAD_URL = "https://serpapi.com/image"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
MAX_UPLOAD_BYTES = 500 * 1024  # SerpApi's documented Image API limit


class TrackerError(Exception):
    """Raised for any SerpApi request problem -- caught by the caller and
    surfaced to the user as a clear status message, never a stack trace."""


@dataclass
class Match:
    title: str = ""
    link: str = ""
    source: str = ""
    thumbnail: str = ""
    image: str = ""


@dataclass
class SearchResult:
    matches: list[Match] = field(default_factory=list)


def is_configured(api_key: str | None) -> bool:
    return bool(api_key and api_key.strip())


def _compress_for_upload(image: Image.Image) -> bytes:
    """Re-encode down to fit SerpApi's 500KB Image API limit, trying
    progressively lower JPEG quality/size until it fits."""
    rgb = image.convert("RGB")
    for quality in (90, 80, 70, 60, 50):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_UPLOAD_BYTES:
            return buf.getvalue()
    # Still too big even at low quality -- shrink dimensions and retry once.
    scale = 0.6
    small = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _upload_image(image_bytes: bytes, api_key: str) -> str:
    try:
        resp = requests.post(
            SERPAPI_IMAGE_UPLOAD_URL,
            files={"image": ("upload.jpg", image_bytes, "image/jpeg")},
            data={"api_key": api_key},
            timeout=30,
        )
    except requests.RequestException as e:
        raise TrackerError(f"Could not reach SerpApi to upload the image: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise TrackerError(f"SerpApi returned a non-JSON response (HTTP {resp.status_code}).")

    if "error" in data:
        raise TrackerError(f"SerpApi rejected the image upload: {data['error']}")
    image_id = data.get("image_id")
    if not image_id:
        raise TrackerError("SerpApi did not return an image_id for the upload.")
    return image_id


def safe_http_url(url: str | None) -> str:
    """Return `url` only if it is a plain http(s) URL, else "". Search
    results are untrusted third-party data and end up in href/src
    attributes, so javascript:, data: and similar schemes are dropped."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def _parse_visual_matches(data: dict) -> SearchResult:
    if "error" in data:
        raise TrackerError(f"SerpApi search failed: {data['error']}")

    matches: list[Match] = []
    seen_links: set[str] = set()
    for m in data.get("visual_matches", []):
        link = safe_http_url(m.get("link"))
        if not link or link in seen_links:
            continue  # unusable or duplicate page
        seen_links.add(link)
        matches.append(
            Match(
                title=str(m.get("title", ""))[:500],
                link=link,
                source=str(m.get("source", ""))[:255],
                thumbnail=safe_http_url(m.get("thumbnail")),
                image=safe_http_url(m.get("image")),
            )
        )
    return SearchResult(matches=matches)


def search_by_upload(image: Image.Image, api_key: str, search_type: str = "visual_matches") -> SearchResult:
    """Upload `image` directly to SerpApi and run a Google Lens search on
    it. This is what Ward uses by default, since protected images live on
    the user's own machine rather than a public URL."""
    if not is_configured(api_key):
        raise TrackerError("No SerpApi key configured. Add SERPAPI_API_KEY to your .env file.")

    image_bytes = _compress_for_upload(image)
    image_id = _upload_image(image_bytes, api_key)

    try:
        resp = requests.get(
            SERPAPI_SEARCH_URL,
            params={"engine": "google_lens", "image_id": image_id, "type": search_type, "api_key": api_key},
            timeout=30,
        )
    except requests.RequestException as e:
        raise TrackerError(f"Could not reach SerpApi to run the search: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise TrackerError(f"SerpApi returned a non-JSON response (HTTP {resp.status_code}).")

    return _parse_visual_matches(data)
