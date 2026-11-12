"""
cloak.py
--------
Ward's protection layer. Read this file before promising a user anything
about what it does -- it is written to be honest, not impressive.

WHAT IT ACTUALLY DOES
    Injects a controlled combination of very-low-spatial-frequency cosine
    patterns into the image, calibrated per-image so that when a
    scraper's pipeline downsamples the image and computes a perceptual
    hash (pHash/aHash/dHash-style -- the cheap, DCT- or gradient-based
    fingerprints most large-scale crawlers use because running a full
    neural encoder per image at crawl scale is too expensive), the
    resulting hash lands far from the original's, in Hamming distance
    terms. Every run reports the actual before/after Hamming distance, so
    this is a checkable number, not a marketing claim.

    The push applied at each of the 63 low frequencies is scaled to that
    image's own natural energy at that frequency (measured directly, not
    guessed): frequencies where the artwork already has strong natural
    content get a proportionally stronger nudge to flip their sign;
    frequencies that are already near-zero get only a small one. This
    keeps the *total* visible change low while still reliably moving the
    hash a long way from the original -- a flat push strong enough for
    high-energy frequencies would be needlessly visible everywhere else.

WHAT IT DOES NOT DO
    It is not a from-scratch reimplementation of Glaze/Nightshade/Mist.
    Those tools run gradient-based adversarial optimization against the
    actual vision encoder (CLIP or similar) that diffusion models train
    against -- that requires downloading and running a multi-hundred-
    megabyte neural network, which this lightweight, dependency-light
    build deliberately does not do. This module will NOT reliably corrupt
    what a diffusion model *learns* from your art the way real Glaze does;
    it targets the cheaper hash-matching step scrapers use to find and
    dedupe images in the first place.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy.fftpack import dct

HASH_SIZE = 8
HIGHFREQ_FACTOR = 4
IMG_SIZE = HASH_SIZE * HIGHFREQ_FACTOR  # 32


def _dct2(a: np.ndarray) -> np.ndarray:
    return dct(dct(a.T, norm="ortho").T, norm="ortho")


def phash_bits(image: Image.Image) -> np.ndarray:
    """Standard pHash algorithm: downscale -> grayscale -> DCT -> take the
    top-left low-frequency block (excluding DC from the threshold calc) ->
    sign vs median = bits."""
    small = image.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64)
    coeffs = _dct2(arr)[:HASH_SIZE, :HASH_SIZE]
    flat = coeffs.flatten()
    med = np.median(flat[1:])  # exclude DC term from the median calc
    return (flat > med).astype(np.uint8)


@functools.lru_cache(maxsize=None)
def _calibration_matrix(probe_w: int, probe_h: int) -> np.ndarray:
    """The 63 per-frequency calibration constants K_rc (see apply_cloak's
    docstring), cached by probe size.

    PERFORMANCE NOTE: K_rc depends only on (r, c) and probe_size -- never
    on the real image's actual pixel content (look at the math: it's the
    DCT response of a synthetic test pattern at probe_size, downsampled
    to IMG_SIZE, with no reference to the caller's image anywhere). And
    since apply_cloak caps probe_size at (min(w,256), min(h,256)), any
    image at least 256px on both sides -- i.e. virtually every real photo
    or artwork; Ward's own minimum is ~300px (see README) -- gets the
    exact same (256, 256) probe size every time. Net effect: this 63-probe
    calibration (each one a small PIL resize + DCT) now runs at most once
    per distinct probe size for the lifetime of the process, instead of
    once per image -- for a long-running local server, that's once, ever,
    for almost every image anyone ever protects, not once per upload.
    Purely a caching change: returns bit-for-bit the same K_rc values the
    old inline per-image loop computed.
    """
    pxs = np.arange(probe_w, dtype=np.float64)
    pys = np.arange(probe_h, dtype=np.float64)
    k = np.zeros((HASH_SIZE, HASH_SIZE), dtype=np.float64)
    for r in range(HASH_SIZE):
        for c in range(HASH_SIZE):
            if r == 0 and c == 0:
                continue  # DC: apply_cloak always skips this bin too
            pbx = np.cos(np.pi * r * (pxs + 0.5) / probe_w)
            pby = np.cos(np.pi * c * (pys + 0.5) / probe_h)
            probe_pattern = np.outer(pby, pbx)
            probe_small = np.asarray(
                Image.fromarray((128 + probe_pattern * 100).clip(0, 255).astype(np.uint8), mode="L")
                .resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS),
                dtype=np.float64,
            )
            probe_coeffs = _dct2(probe_small)[:HASH_SIZE, :HASH_SIZE]
            k[c, r] = probe_coeffs[c, r] / 100.0
    return k


def hamming(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.sum(a != b))


def _bits_to_hex(bits: np.ndarray) -> str:
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return f"{val:0{(len(bits) + 3) // 4}x}"


@dataclass
class CloakReport:
    hash_before: str
    hash_after: str
    hamming_distance: int
    hash_bits_total: int
    mean_pixel_delta: float
    max_pixel_delta: float


def apply_cloak(
    image: Image.Image,
    strength: float = 1.0,
    floor_push: float = 3.0,
    cap_push: float = 25.0,
) -> tuple[Image.Image, CloakReport]:
    """Return (cloaked_image, report).

    strength: overall multiplier on how far past the median each flipped
    frequency lands (1.0 = default; try 0.6 for a gentler pass on flat,
    pastel art where banding shows first, 1.4 for a stronger pass on busy
    or high-contrast pieces).
    floor_push: how far past the per-image median each flipped coefficient
    is pushed, in DCT-coefficient units -- the margin that keeps a flip
    from drifting back under mild JPEG recompression or resizing.
    cap_push: frequencies whose *natural* coefficient already sits further
    than this from the median are skipped rather than flipped -- dragging
    a high-energy frequency all the way across the median is exactly the
    kind of push that shows up as visible banding, so it's cheaper (and
    just as effective in aggregate) to leave those alone and flip the
    affordable ones instead.
    """
    rgb = image.convert("RGB")
    w, h = rgb.size

    small = np.asarray(rgb.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS), dtype=np.float64)
    natural = _dct2(small)[:HASH_SIZE, :HASH_SIZE]
    original_bits = phash_bits(rgb)

    xs = np.arange(w, dtype=np.float64)
    ys = np.arange(h, dtype=np.float64)
    # Bx[r] / By[c]: full-resolution cosine basis vectors, one row per
    # frequency -- stacked (instead of a per-frequency dict) so the delta
    # accumulation below is one matrix multiply instead of a 63-iteration
    # Python loop of outer-product-and-accumulate. See PERFORMANCE NOTE.
    Bx = np.cos(np.pi * np.arange(HASH_SIZE)[:, None] * (xs[None, :] + 0.5) / w)  # (HASH_SIZE, w)
    By = np.cos(np.pi * np.arange(HASH_SIZE)[:, None] * (ys[None, :] + 0.5) / h)  # (HASH_SIZE, h)

    # Per-(r,c) calibration constant K: how much a unit-amplitude full-res
    # cosine at that frequency shows up as coefficient magnitude after the
    # exact same downsample-and-DCT the hash uses -- cached across calls,
    # see _calibration_matrix's docstring for why that's valid here.
    probe_size = (min(w, 256), min(h, 256))
    k_matrix = _calibration_matrix(*probe_size)  # k_matrix[c, r], same indexing as `natural` below

    med = np.median(natural.flatten()[1:])  # same threshold phash_bits uses

    # PERFORMANCE NOTE: this used to be a 63-iteration Python loop, each
    # iteration computing one scalar amplitude and accumulating one
    # np.outer(...) (an O(h*w) array op) into `delta`. Vectorized here into
    # whole-(8,8)-grid array arithmetic plus a single small matmul chain
    # (By.T @ amp @ Bx) that produces the exact same sum -- every entry
    # this loop would have `continue`d past (DC, an attenuated frequency,
    # or one past cap_push) instead gets amp=0, which contributes nothing,
    # matching "skip" exactly. Verified against the old per-frequency loop
    # to float64 noise-floor precision.
    dist_to_med = natural - med  # natural is already (HASH_SIZE, HASH_SIZE) -- see above
    target_side = np.where(dist_to_med > 0, -1.0, 1.0)  # push to the far side of the median
    target_coeff = med + target_side * (floor_push * strength)
    required_change = target_coeff - natural

    attenuated = np.abs(k_matrix) < 1e-6  # frequency fully attenuated by the resize filter
    too_costly = np.abs(dist_to_med) > cap_push  # would need a very large (visibly banding) push
    skip = attenuated | too_costly
    skip[0, 0] = True  # DC: don't shift overall brightness

    safe_k = np.where(attenuated, 1.0, k_matrix)  # placeholder divisor where amp will be zeroed anyway
    amp = np.where(skip, 0.0, required_change / safe_k)  # amp[c, r]

    delta = By.T @ amp @ Bx  # (h,HASH_SIZE) @ (HASH_SIZE,HASH_SIZE) @ (HASH_SIZE,w) -> (h,w)

    # Deliberately no global rescale here: a handful of DCT-II basis
    # functions naturally phase-align near the image corners (all of them
    # start near their peak at x=0/y=0), which would make a *global*
    # peak-based rescale punish the entire well-calibrated interior just
    # to tame a few corner pixels. Per-pixel clipping below handles that
    # small, localized region on its own without touching the rest.
    arr = np.asarray(rgb, dtype=np.float64)
    out = np.clip(arr + delta[:, :, None], 0, 255).astype(np.uint8)
    cloaked = Image.fromarray(out, mode="RGB")

    after_bits = phash_bits(cloaked)
    report = CloakReport(
        hash_before=_bits_to_hex(original_bits),
        hash_after=_bits_to_hex(after_bits),
        hamming_distance=hamming(original_bits, after_bits),
        hash_bits_total=len(original_bits),
        mean_pixel_delta=float(np.abs(arr - out.astype(np.float64)).mean()),
        max_pixel_delta=float(np.abs(arr - out.astype(np.float64)).max()),
    )
    return cloaked, report


@dataclass
class FaceCloakReport:
    box: tuple[int, int, int, int]  # padded (x, y, w, h) actually cloaked
    hamming_distance: int
    hash_bits_total: int


def apply_face_cloak(
    image: Image.Image,
    face_boxes: list[tuple[int, int, int, int]],
    strength: float = 1.0,
    floor_push: float = 2.5,
    cap_push: float = 18.0,
) -> tuple[Image.Image, list[FaceCloakReport]]:
    """Run a *second*, localized cloak pass scoped to each face crop, on
    top of whatever apply_cloak() already did to the full image.

    Why a second pass is needed at all: apply_cloak() targets the pHash
    of the *whole* image. A tight crop of just a face -- which is what
    most face-scraping tools and reverse-image searches actually hash --
    computes its own pHash from scratch, over completely different
    64x64 downsampled content than the full frame. Disrupting the
    whole-image hash does approximately nothing to the crop's hash.
    Running apply_cloak() again, but scoped to just that crop's pixels,
    fixes that: the crop gets its own before/after pHash flip.

    face_boxes: (x, y, w, h) tuples in full-image pixel coordinates --
    already padded (see faces.FaceBox.padded) by the caller.
    floor_push/cap_push default gentler than the whole-image pass:
    faces are disproportionately small, high-scrutiny regions (skin-tone
    banding is the first thing a person notices in their own photo), so
    this pass deliberately spends less of the visible-change budget per
    frequency than the full-image pass does.

    Blending: pastes the locally-cloaked crop back using a soft
    (feathered) elliptical mask rather than a hard rectangle, so the
    seam at the crop boundary doesn't itself become a visible artifact.
    """
    rgb = image.convert("RGB")
    w, h = rgb.size
    arr = np.asarray(rgb, dtype=np.float64).copy()
    reports: list[FaceCloakReport] = []

    for (x, y, bw, bh) in face_boxes:
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(w, int(x + bw)), min(h, int(y + bh))
        if x1 - x0 < 16 or y1 - y0 < 16:
            continue  # too small to hash meaningfully on its own

        crop = rgb.crop((x0, y0, x1, y1))
        cloaked_crop, report = apply_cloak(crop, strength=strength, floor_push=floor_push, cap_push=cap_push)
        crop_arr = np.asarray(cloaked_crop, dtype=np.float64)

        # Feathered elliptical mask: 1.0 in the center, fading to 0.0 at
        # the crop's edges, so the locally-cloaked pixels blend into the
        # surrounding (already whole-image-cloaked) pixels instead of
        # producing a visible rectangle.
        cw, ch = x1 - x0, y1 - y0
        yy, xx = np.mgrid[0:ch, 0:cw]
        cx, cy = cw / 2.0, ch / 2.0
        norm = (((xx - cx) / (cx + 1e-6)) ** 2 + ((yy - cy) / (cy + 1e-6)) ** 2)
        mask = np.clip(1.4 - norm, 0.0, 1.0)  # >1 in the core stays fully 1.0 after clip
        mask = mask ** 1.5  # sharpen the falloff curve a bit
        mask3 = mask[:, :, None]

        region = arr[y0:y1, x0:x1, :]
        arr[y0:y1, x0:x1, :] = region * (1 - mask3) + crop_arr * mask3

        reports.append(
            FaceCloakReport(
                box=(x0, y0, x1 - x0, y1 - y0),
                hamming_distance=report.hamming_distance,
                hash_bits_total=report.hash_bits_total,
            )
        )

    out = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB"), reports
