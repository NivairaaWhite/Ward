"""
images.py
---------
Minimal image storage for the tracker: save an uploaded file exactly as
it was uploaded (so it can be re-scanned later and handed back to the
user unchanged), plus a small JPEG preview for the gallery grid.

No watermarking, no cloaking, no face detection, no metadata rewriting
-- this module's whole job is "get the file onto disk and make a
thumbnail," which is all reverse-image-search tracking actually needs.
"""
from __future__ import annotations

import logging
import os

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

THUMB_MAX_DIMENSION = 480

# Refuse absurdly large pixel counts (decompression bombs) instead of Pillow's
# warn-then-maybe-crash default.
Image.MAX_IMAGE_PIXELS = 100_000_000


def save_tracked_image(file_storage, storage_dir: str, thumb_dir: str, db_id: str) -> dict:
    """Saves `file_storage` (a Flask FileStorage) under a name keyed to
    db_id, in its original format, plus a JPEG thumbnail alongside it.
    Returns the fields a caller needs to populate a TrackedImage row.
    Raises PIL.UnidentifiedImageError / OSError if the file isn't a
    readable image -- callers should catch and turn that into a 400."""
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    stored_filename = f"{db_id}.{ext}"
    stored_path = os.path.join(storage_dir, stored_filename)
    file_storage.save(stored_path)

    try:
        with Image.open(stored_path) as img:
            img = ImageOps.exif_transpose(img)  # normalize orientation before measuring/thumbnailing
            width, height = img.size

            thumb = img.convert("RGB")
            thumb.thumbnail((THUMB_MAX_DIMENSION, THUMB_MAX_DIMENSION))
            thumb_filename = f"{db_id}_thumb.jpg"
            thumb.save(os.path.join(thumb_dir, thumb_filename), format="JPEG", quality=85)
    except Exception:
        # Not a readable image after all (or something else went wrong
        # mid-thumbnail) -- don't leave an orphaned file with no DB record
        # pointing at it behind on disk.
        if os.path.exists(stored_path):
            os.remove(stored_path)
        raise

    return {
        "stored_filename": stored_filename,
        "thumb_filename": thumb_filename,
        "width": width,
        "height": height,
    }


def remove_files(storage_dir: str, thumb_dir: str, stored_filename: str | None, thumb_filename: str | None) -> None:
    """Best-effort delete of an image's files. Never raises -- callers use
    this after the database change has already succeeded."""
    for folder, fname in ((storage_dir, stored_filename), (thumb_dir, thumb_filename)):
        if not fname:
            continue
        try:
            os.remove(os.path.join(folder, fname))
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not delete %s", os.path.join(folder, fname), exc_info=True)
