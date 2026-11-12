"""
pipeline.py
-----------
Wires watermark.py + cloak.py together into the single "protect this
image" operation, and handles the practical bits neither module deals
with on its own: preserving transparency on PNG artwork (both modules
work on RGB internally), writing a reasonably-sized output file, and
generating a thumbnail for the dashboard.
"""
from __future__ import annotations

import os
import secrets
import shutil

import numpy as np
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo

from core import cloak, faces, metadata_tags, watermark

THUMB_SIZE = (360, 360)
WATERMARK_PAYLOAD_BYTES = 6  # 48-bit tracking code -- see watermark.py capacity notes
TRACKING_ID_HEX_LEN = WATERMARK_PAYLOAD_BYTES * 2  # tracking ID == the watermark payload, as hex
MAX_DIMENSION = 8000  # sanity guard; see README "Limits"

# Mode presets. Both modes run the exact same mechanism (watermark.py +
# cloak.py) -- the presets only tune how hard the cloak pushes, calibrated
# for the kind of image each is meant for. Portrait mode is deliberately
# gentler: skin tones and soft gradients show banding at a lower amplitude
# than illustration flats/lines do, and profile photos tend to already get
# aggressively re-compressed by the platforms they're posted to, so there's
# less room to spend before an extra JPEG pass starts eating the margin.
#
# Neither mode does anything about facial recognition or AI video/deepfake
# generation specifically -- see README "What Ward does not do" before
# calling this "Identity" or "anti-deepfake" protection anywhere in a UI.
MODE_PRESETS = {
    # watermark_strength=20.0 for both -- this is the v2 (DWT-DCT + Reed-
    # Solomon) scheme's calibrated default, NOT comparable to the old
    # pixel-domain scheme's numbers (that version used 30.0; the two
    # scales aren't the same thing -- see watermark.py's docstring for
    # why). Chosen empirically against real re-save/resize/repost
    # pipeline simulations (not a simplified/easier stand-in -- an
    # earlier pass at this calibration used gentler test scenarios than
    # the ones actually in that script and got a falsely low answer;
    # 20.0 is the value re-verified against the actual, established
    # pipelines): this is the minimum strength (with a small safety
    # margin over the measured threshold of 18.0) that cleanly fixes two
    # real, reproducible v1 failures -- a single very heavy re-save, and
    # an image reposted three times through progressively smaller
    # resizes -- confirmed against two different test images. Higher
    # strength values were tested and didn't meaningfully help the
    # remaining failure cases (thumbnails resized under ~300px), which
    # are a genuine information-density floor, not a tunable parameter --
    # resizing to 150px discards ~90%+ of the original pixel data before
    # the watermark ever gets a chance.
    "art": {"floor_push": 3.0, "cap_push": 25.0, "watermark_strength": 20.0},
    "portrait": {"floor_push": 2.0, "cap_push": 16.0, "watermark_strength": 20.0},
}
DEFAULT_MODE = "art"


class PipelineError(Exception):
    pass


def new_tracking_id() -> str:
    """A fresh tracking ID. It IS the watermark payload (6 random bytes,
    hex-encoded), so a suspect image can be identified from the
    watermark alone, without knowing which ID to test it against."""
    return secrets.token_hex(WATERMARK_PAYLOAD_BYTES)


def is_valid_tracking_id(value: str | None) -> bool:
    if not value or len(value) != TRACKING_ID_HEX_LEN:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _normalize_depth(img: Image.Image) -> Image.Image:
    """Bring 16-bit / 32-bit / float grayscale images down to 8-bit.
    Pillow's plain .convert("RGB") on these modes clips instead of
    scaling, so a 16-bit grayscale PNG came out almost entirely white."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N"):
        arr = np.asarray(img, dtype=np.float64) / 257.0  # 65535 -> 255
        return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), mode="L")
    if img.mode == "I":
        arr = np.asarray(img, dtype=np.float64)
        scale = 257.0 if arr.max() > 255 else 1.0
        return Image.fromarray(np.clip(np.rint(arr / scale), 0, 255).astype(np.uint8), mode="L")
    if img.mode == "F":
        arr = np.asarray(img, dtype=np.float64)
        if arr.size and arr.max() <= 1.0:
            arr = arr * 255.0
        return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), mode="L")
    return img


def _rgb_icc_profile(img: Image.Image) -> bytes | None:
    """The source's ICC profile, but only if it describes RGB. Output is
    always RGB(A), so attaching a gray or CMYK profile would be wrong."""
    icc = img.info.get("icc_profile")
    if icc and len(icc) >= 20 and icc[16:20] == b"RGB ":
        return icc
    return None


def _split_alpha(image: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    """Return (rgb_image, alpha_channel_or_None)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        r, g, b, a = rgba.split()
        return Image.merge("RGB", (r, g, b)), a
    return image.convert("RGB"), None


def _source_had_gps(img: Image.Image) -> bool:
    """Informational only -- checks whether the *uploaded* file carried
    GPS coordinates in its EXIF, so the UI can show "GPS location data
    removed" as a concrete fact rather than a vague claim. GPS-tagged
    personal photos are a real, well-documented safety risk (they reveal
    where a photo was taken -- home, workplace, a child's school) wholly
    separate from anything cloak.py or watermark.py do."""
    try:
        exif = img.getexif()
        return bool(exif) and 34853 in exif  # 34853 = GPSInfo IFD pointer tag
    except Exception:
        return False


def run_protect(
    image_path: str,
    outputs_dir: str,
    tracking_id: str | None = None,
    strength: float = 1.0,
    mode: str = DEFAULT_MODE,
    face_aware: bool = True,
    max_dimension: int = MAX_DIMENSION,
    keep_original: bool = True,
    originals_dir: str | None = None,
    tag_no_ai_training: bool = True,
) -> dict:
    """Run the full protect pipeline on the image at `image_path`, write
    outputs into `outputs_dir`, and return a dict of everything the caller
    should persist to the database. Pass `tracking_id` if the caller has
    already created a database row for this image (e.g. to show it as
    "queued" in a gallery before processing finishes); otherwise one is
    generated here. `mode` selects a calibration preset -- see
    MODE_PRESETS above.

    keep_original / originals_dir: if both are set, an exact byte-for-byte
    copy of the uploaded file is stashed in originals_dir under the
    tracking ID, so the user can later get their unmarked file back
    (see app.py's /api/images/<id>/original route and README "Getting
    your original back / switching tools"). This is a real, deliberate
    design choice, not a bug: Ward does not try to mathematically invert
    the cloak+watermark from the *protected* output, because after any
    re-compression that inverse isn't exact -- keeping the true original
    is the only way to hand back a byte-identical unmarked file on
    request. Set keep_original=False if you'd rather not retain a second
    copy of every source image on disk (see Settings).

    tag_no_ai_training: if True (default), embeds the real IPTC/PLUS
    "Data Mining" XMP property declaring the image prohibited for AI/ML
    training -- see metadata_tags.py for exactly what this does and
    doesn't guarantee (short version: it's a real, current, legally
    meaningful standard, but it's a declared preference like robots.txt,
    not a technical barrier, and many platforms strip it on upload)."""
    if mode not in MODE_PRESETS:
        raise PipelineError(f"Unknown mode '{mode}'. Must be one of {list(MODE_PRESETS)}.")
    preset = MODE_PRESETS[mode]

    try:
        img = Image.open(image_path)
        img.load()
    except Exception as e:
        raise PipelineError(f"Could not read image file: {e}") from e

    # Read what only the *file* knows before the pixels are rewritten.
    had_gps = _source_had_gps(img)
    icc_profile = _rgb_icc_profile(img)
    # Phone photos store rotation as an EXIF tag, which is dropped on
    # save -- bake the rotation into the pixels so the output isn't sideways.
    img = ImageOps.exif_transpose(img)
    img = _normalize_depth(img)

    width, height = img.size
    if max(width, height) > max_dimension:
        raise PipelineError(
            f"Image is {width}x{height}, which is above the {max_dimension}px limit "
            f"-- downscale it and try again."
        )
    # Computed dynamically (not a hardcoded "32x32"-style minimum) because
    # the v2 DWT watermark's usable capacity depends on the DWT LL
    # sub-band size, which is roughly a quarter of the pixel-domain
    # scheme's capacity at the same dimensions -- a stale hardcoded
    # minimum would either reject images that would actually work or
    # (worse) accept ones that don't have room for the payload + Reed-
    # Solomon parity at all. Some redundancy margin beyond the bare
    # minimum matters for majority-vote robustness, hence the buffer.
    min_bits_needed = (WATERMARK_PAYLOAD_BYTES + watermark.RS_PARITY_BYTES) * 8
    min_bits_for_redundancy = min_bits_needed * 3  # want a few repeats, not exactly zero margin
    if watermark.capacity_bits(width, height) < min_bits_for_redundancy:
        raise PipelineError(
            f"Image is {width}x{height}, too small to watermark reliably at this size -- "
            f"try at least roughly 300x300."
        )

    source_ext = os.path.splitext(image_path)[1] or ".bin"

    rgb, alpha = _split_alpha(img)

    if tracking_id is None:
        tracking_id = new_tracking_id()
    elif not is_valid_tracking_id(tracking_id):
        raise PipelineError(f"tracking_id must be {TRACKING_ID_HEX_LEN} hex characters.")
    # The watermark payload IS the tracking ID.
    payload = bytes.fromhex(tracking_id)
    watermarked = watermark.embed(rgb, payload, strength=preset["watermark_strength"])
    cloaked, report = cloak.apply_cloak(
        watermarked, strength=strength, floor_push=preset["floor_push"], cap_push=preset["cap_push"]
    )

    # Face-aware pass: detect faces and give each one its own localized
    # cloak so a scraped/cropped face -- a common thing to get reverse-
    # searched on its own -- also lands far from its original hash. Best-
    # effort: a detection failure here should never fail the whole
    # protect job, since the main whole-image cloak above already ran.
    face_boxes_out: list[dict] = []
    if face_aware:
        try:
            detected = faces.detect_faces(cloaked)
            padded = [fb.padded(width, height) for fb in detected]
            if padded:
                cloaked, face_reports = cloak.apply_face_cloak(
                    cloaked,
                    [fb.as_tuple() for fb in padded],
                    strength=strength,
                    floor_push=preset["floor_push"] * 0.85,
                    cap_push=preset["cap_push"] * 0.7,
                )
                face_boxes_out = [
                    {
                        "x": fr.box[0], "y": fr.box[1], "w": fr.box[2], "h": fr.box[3],
                        "hamming_distance": fr.hamming_distance,
                        "hash_bits_total": fr.hash_bits_total,
                    }
                    for fr in face_reports
                ]
        except Exception:
            # Detection is best-effort and optional -- swallow and report
            # zero faces rather than failing an otherwise-successful protect.
            face_boxes_out = []

    xmp_bytes = metadata_tags.build_xmp_packet() if tag_no_ai_training else None

    if alpha is not None:
        final = Image.merge("RGBA", (*cloaked.split(), alpha))
        out_name = f"{tracking_id}.png"
        # PNG: only pass exactly the encoder kwargs below -- never img.info
        # or final.info -- so no source metadata (EXIF text chunks, GPS,
        # camera serials) rides along into the "protected" file. pnginfo
        # carries *only* the IPTC/PLUS Data Mining opt-out tag we build
        # ourselves -- see metadata_tags.py.
        pnginfo = None
        if xmp_bytes:
            pnginfo = PngInfo()
            pnginfo.add_itxt("XML:com.adobe.xmp", xmp_bytes.decode("utf-8"), zip=False)
        final.save(
            os.path.join(outputs_dir, out_name), format="PNG", optimize=True, pnginfo=pnginfo,
            icc_profile=icc_profile,
        )
    else:
        final = cloaked
        out_name = f"{tracking_id}.jpg"
        # Quality 95: comfortably above the JPEG-85 level the watermark and
        # cloak were validated to survive, while keeping the file a
        # realistic size to actually upload/share. No exif= kwarg passed,
        # same reasoning as the PNG branch above -- the source's EXIF
        # (including GPS, if any) is not carried into this file. xmp= is
        # the one piece of metadata Ward adds itself (see xmp_bytes above).
        final.save(
            os.path.join(outputs_dir, out_name), format="JPEG", quality=95, xmp=xmp_bytes,
            icc_profile=icc_profile,
        )

    # Self-check: read the file we just wrote back from disk and make sure
    # the watermark really decodes from it. If not (a very flat or tiny
    # image, say), fail loudly rather than hand back an unmarked file that
    # looks protected.
    saved_path = os.path.join(outputs_dir, out_name)
    with Image.open(saved_path) as saved:
        saved.load()
        saved_rgb, _ = _split_alpha(saved)
    if watermark.extract_checked(saved_rgb, WATERMARK_PAYLOAD_BYTES) != payload:
        os.remove(saved_path)
        raise PipelineError(
            "The tracking watermark could not be verified in the saved file, so the image was "
            "not protected. Very flat, very small or heavily saturated images can leave too "
            "little room for it -- try a different image."
        )

    thumb = final.convert("RGB").copy()
    thumb.thumbnail(THUMB_SIZE)
    thumb_name = f"{tracking_id}_thumb.jpg"
    thumb.save(os.path.join(outputs_dir, thumb_name), format="JPEG", quality=85)

    original_stored_filename = None
    if keep_original and originals_dir:
        os.makedirs(originals_dir, exist_ok=True)
        original_stored_filename = f"{tracking_id}_original{source_ext}"
        shutil.copy2(image_path, os.path.join(originals_dir, original_stored_filename))

    return {
        "tracking_id": tracking_id,
        "mode": mode,
        "width": width,
        "height": height,
        "has_alpha": alpha is not None,
        "protected_filename": out_name,
        "thumb_filename": thumb_name,
        "watermark_hex": payload.hex(),
        "watermark_bytes_len": len(payload),
        "cloak_hamming": report.hamming_distance,
        "cloak_bits_total": report.hash_bits_total,
        "cloak_mean_delta": round(report.mean_pixel_delta, 3),
        "cloak_max_delta": round(report.max_pixel_delta, 3),
        "faces_detected": len(face_boxes_out),
        "face_boxes": face_boxes_out,
        "gps_stripped": had_gps,
        "ai_training_opt_out_tagged": bool(xmp_bytes),
        "original_stored_filename": original_stored_filename,
    }


def verify_watermark(
    suspect_path: str,
    expected_payload_hex: str,
    original_size: tuple[int, int] | None,
) -> dict:
    """Check whether a suspect image (e.g. found elsewhere online) carries
    a specific Ward tracking code. Returns a confidence score and the
    decoded payload for comparison."""
    try:
        img = Image.open(suspect_path)
        img.load()
    except Exception as e:
        raise PipelineError(f"Could not read image file: {e}") from e

    expected = bytes.fromhex(expected_payload_hex)
    rgb, _ = _split_alpha(_normalize_depth(img))

    conf = watermark.confidence(rgb, expected)
    if original_size:
        resized = rgb.resize(original_size, Image.LANCZOS) if rgb.size != original_size else rgb
        conf_resized = watermark.confidence(resized, expected)
        conf = max(conf, conf_resized)

    decoded = watermark.extract_auto(rgb, len(expected), original_size)

    # Fallback: the fast path above only corrects for a uniform resize.
    # A low score here doesn't necessarily mean "not a match" -- it's also
    # exactly what a center-cropped copy looks like. Only worth the extra
    # ~3-4 seconds when the fast path actually looks uncertain.
    used_crop_recovery = False
    if conf < 0.90 and original_size:
        crop_decoded, crop_conf = watermark.recover_from_crop(rgb, expected, original_size)
        if crop_conf > conf:
            conf = crop_conf
            decoded = crop_decoded if crop_decoded is not None else decoded
            used_crop_recovery = True

    return {
        "confidence": round(conf, 4),
        "decoded_hex": decoded.hex(),
        "likely_match": conf >= 0.75,
        "used_crop_recovery": used_crop_recovery,
    }


def identify_watermark(suspect_path: str) -> str | None:
    """Decode a suspect image's watermark with no ID to test against.
    Possible because the watermark payload is the tracking ID. Only
    handles an image at its original pixel size (a resized or cropped
    copy needs verify_watermark with a known ID, which can undo that).
    Returns the tracking ID as hex, or None if nothing decodes."""
    try:
        img = Image.open(suspect_path)
        img.load()
    except Exception as e:
        raise PipelineError(f"Could not read image file: {e}") from e
    rgb, _ = _split_alpha(_normalize_depth(img))
    found = watermark.extract_checked(rgb, WATERMARK_PAYLOAD_BYTES)
    return found.hex() if found is not None else None
