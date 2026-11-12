"""
watermark.py
------------
A from-scratch, dependency-light blind watermark: embeds a short binary
payload (Ward's "tracking code") into an image and extracts it back out
without needing the original image.

TWO LAYERS, EACH DOING A DIFFERENT JOB
  1. WHERE the bits live: a hybrid DWT-DCT scheme (see _embed_bits /
     _decode_votes below). Convert to YCbCr, take a 1-level Haar DWT of
     the luma (Y) channel -- this splits it into a half-resolution LL
     ("approximation": roughly the image downsampled 2x, properly, not
     just resampled) sub-band plus three detail sub-bands. Only LL is
     touched: split it into 8x8 blocks, take each block's 2D DCT, and
     nudge one fixed mid-frequency coefficient pair so one reliably
     exceeds the other in the direction that encodes a 0 or 1. Repeat
     the payload to fill every available LL block; extraction takes a
     majority vote per bit across all repeats.
  2. WHAT the bits mean: Reed-Solomon error correction (`reedsolo`,
     RS_PARITY_BYTES parity bytes) wraps the payload before step 1 and
     unwraps it after. This is a second, different kind of redundancy:
     majority voting already cleans up most noise by repeating the
     payload across the whole image, but RS parity can still recover a
     handful of majority-vote bytes that came out wrong -- e.g. from a
     specific unlucky block, not random noise everywhere.
  This layering is deliberate and separately measured, not assumed --
  see "why this changed" below for the actual numbers.

WHY THIS CHANGED FROM v1 (plain block-DCT on raw pixels, no error correction)
  A real test (a stress test of re-saves, resizes and reposts) sent a v1-protected image through actual
  WhatsApp and confirmed v1 survived that specific case perfectly (0%
  bit-error). But running the same image through the project's own
  stress-test harness at more aggressive real-world scenarios found
  genuine, reproducible failures: a heavy single re-save, and an image
  reposted three times in a row, both corrupted the tracking code.
  Root cause: resizing breaks a raw-pixel block-DCT scheme's alignment
  to the original 8x8 grid, and there's no recovery once a majority-vote
  bit is simply wrong.
  DWT LL-band embedding fixes the resize part: LL survives downscaling
  by construction, since it already *is* a low-pass/half-resolution
  view of the image, not raw pixels resampled after the fact -- a
  well-established technique in the watermarking literature (hybrid
  DWT-DCT schemes are consistently more resize/recompress-robust than
  plain DCT). Reed-Solomon fixes the "majority vote landed on a wrong
  byte" part. Measured together against the same stress-test harness:
  the "very heavy re-save" and "reposted three times" cases that failed
  under v1 now pass cleanly, at a *lower* embedding strength than v1
  used (20.0 vs. v1's 30.0) -- meaning this is simultaneously more
  robust and less visible. See the README for the full scenario table.

  One specific idea considered and rejected: embedding in the DWT HH
  (diagonal high-frequency) sub-band via SVD instead of LL via DCT. HH is
  exactly the fine detail that both compression and downscaling destroy
  first -- embedding there would very likely make resize robustness
  *worse*, the opposite of the goal, which is why this wasn't built
  despite being a reasonable-sounding suggestion.

HONEST LIMITS (still true, still worth saying plainly)
  - Not designed to survive aggressive cropping, rotation, or
    screenshotting -- those need geometric re-synchronization, a much
    larger project than this one.
  - There is a real information-theoretic floor, and Reed-Solomon can't
    move it: shrink an image enough (roughly under ~300px on the long
    edge with typical compression, per the stress-test numbers) and
    there just isn't enough surviving LL-band data to reliably recover
    48 bits, no matter how it's encoded. Treat this as a provenance mark
    for realistic sharing at realistic sizes, not a forensic guarantee
    at any size.

PERFORMANCE NOTE (CPU vectorization)
  Embed/decode used to loop over every 8x8 LL block in pure Python, doing
  one scipy DCT/IDCT call per block -- fine at a few hundred blocks, but a
  large image's LL band can have tens of thousands of them, and a Python
  function call plus a tiny 8x8 transform paid tens of thousands of times
  is mostly call overhead, not real work. _blocks_from_grid/_grid_from_blocks
  reshape the whole LL band into one (N, 8, 8) stack, and _dct2_batch/
  _idct2_batch run the transform on the whole stack in a single call
  (scipy's dct/idct treat every axis except the one being transformed as
  independent batches -- this is the standard way block-transform codecs
  vectorize). This is *only* a batching change: verified to match the old
  per-block-loop output to ~1e-13 (float64 noise floor, no visible or
  functional difference) before it replaced the loop.
"""
from __future__ import annotations

import numpy as np
import pywt
from PIL import Image
from reedsolo import ReedSolomonError, RSCodec
from scipy.fftpack import dct, idct

BLOCK = 8
# Mid-frequency coefficient pair (avoids the DC term and the very high
# frequencies JPEG quantizes hardest).
COORD_A = (3, 1)
COORD_B = (1, 3)
WAVELET = "haar"  # simplest orthogonal wavelet; fast, no ringing, well-studied for exactly this use
DWT_MODE = "periodization"  # LL is exactly ceil(n/2) per axis and reconstructs to the exact original size
_HAAR_SCALE = 1.0 / np.sqrt(2.0)  # Haar's normalized low/high-pass scale factor -- see _haar_dwt2_fast

# Reed-Solomon parity bytes appended to the payload before embedding.
# 6 was chosen empirically (measured against re-save/repost scenarios): it's enough to recover the "reposted three times" scenario at
# the chosen embedding strength, and more parity didn't measurably help
# the remaining (much harder, near-information-theoretic-floor) thumbnail
# cases -- so there's no benefit to spending more capacity on it.
RS_PARITY_BYTES = 6
_rs = RSCodec(RS_PARITY_BYTES)


def _bytes_to_bits(data: bytes) -> list[int]:
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        val = 0
        for b in bits[i : i + 8]:
            val = (val << 1) | b
        out.append(val)
    return bytes(out)


def _haar_dwt2_fast(y: np.ndarray) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Hand-rolled 1-level 2D Haar DWT, periodization mode, for EVEN x EVEN
    input only (caller must check -- see _dwt2 below). Haar's periodization
    transform is nothing more than normalized pairwise sum/difference of
    adjacent samples along each axis (no filter-length-dependent boundary
    padding needed, since Haar's 2-tap filter divides an even axis exactly)
    -- there's no approximation here, this is the literal definition of
    the transform, just written directly instead of going through pywt's
    general n-dimensional/arbitrary-wavelet machinery. Verified to match
    pywt.dwt2(y, "haar", mode="periodization") to ~1e-13 (float64 noise
    floor) across many shapes."""
    s = _HAAR_SCALE
    lo_r = (y[0::2, :] + y[1::2, :]) * s
    hi_r = (y[0::2, :] - y[1::2, :]) * s
    ll = (lo_r[:, 0::2] + lo_r[:, 1::2]) * s
    lh = (lo_r[:, 0::2] - lo_r[:, 1::2]) * s
    hl = (hi_r[:, 0::2] + hi_r[:, 1::2]) * s
    hh = (hi_r[:, 0::2] - hi_r[:, 1::2]) * s
    return ll, (hl, lh, hh)


def _haar_idwt2_fast(coeffs: tuple[np.ndarray, tuple[np.ndarray, np.ndarray, np.ndarray]]) -> np.ndarray:
    """Inverse of _haar_dwt2_fast -- see that function's docstring.

    Interleaves via stack+reshape (np.stack([a, b], axis=k).reshape(...))
    rather than writing into strided slices of a pre-allocated array
    (out[0::2] = a; out[1::2] = b) -- mathematically identical, but
    measured ~2x faster: a strided write touches every other row/column
    of memory in a separate pass per stride, where stack+reshape produces
    the same interleaved layout as one contiguous block, which numpy
    fills more efficiently."""
    ll, (hl, lh, hh) = coeffs
    s = _HAAR_SCALE
    bh, bw = ll.shape
    lo_r = np.stack(((ll + lh) * s, (ll - lh) * s), axis=-1).reshape(bh, bw * 2)
    hi_r = np.stack(((hl + hh) * s, (hl - hh) * s), axis=-1).reshape(bh, bw * 2)
    return np.stack(((lo_r + hi_r) * s, (lo_r - hi_r) * s), axis=1).reshape(bh * 2, bw * 2)


def _is_even(shape: tuple[int, int]) -> bool:
    return shape[0] % 2 == 0 and shape[1] % 2 == 0


def _dwt2(y: np.ndarray):
    """Dispatch to the fast hand-rolled Haar transform for the (very
    common -- virtually every camera/screen resolution is even x even)
    even x even case, falling back to pywt for odd dimensions, where
    periodization mode's boundary handling is more involved than a plain
    pairwise sum/difference. Whichever path runs, pair it with a matching
    _idwt2(..., original_shape=y.shape) call -- the two are only
    interchangeable in the sense that they produce numerically identical
    coefficients (verified), not that either INVERSE can consume the
    other's output blind: _idwt2 needs y's original shape (odd vs. even)
    to know which inverse to run, which is why every caller here threads
    it through explicitly instead of guessing from coefficient shapes."""
    if _is_even(y.shape):
        return _haar_dwt2_fast(y)
    return pywt.dwt2(y, WAVELET, mode=DWT_MODE)


def _idwt2(coeffs, original_shape: tuple[int, int]) -> np.ndarray:
    if _is_even(original_shape):
        return _haar_idwt2_fast(coeffs)
    return pywt.idwt2(coeffs, WAVELET, mode=DWT_MODE)


def _ll_band(y: np.ndarray) -> np.ndarray:
    ll, _ = _dwt2(y)
    return ll


def capacity_bits(width: int, height: int) -> int:
    """How many 8x8 blocks are available in the LL sub-band (== max raw
    bits with zero redundancy, before Reed-Solomon overhead). LL is half
    resolution per axis, so this is roughly a quarter of what a
    pixel-domain scheme could hold at the same image size -- see the
    module docstring for why that trade-off is worth it."""
    ll_h = pywt.dwt_coeff_len(height, pywt.Wavelet(WAVELET).dec_len, mode=DWT_MODE)
    ll_w = pywt.dwt_coeff_len(width, pywt.Wavelet(WAVELET).dec_len, mode=DWT_MODE)
    return (ll_h // BLOCK) * (ll_w // BLOCK)


def _blocks_from_grid(grid: np.ndarray, bh: int, bw: int) -> np.ndarray:
    """Slice `grid` into the top-left (bh*BLOCK, bw*BLOCK) region and
    reshape it into a (bh*bw, BLOCK, BLOCK) stack, ordered the same as the
    row-major (by, bx) loop this replaces -- i.e. flat index by*bw+bx.
    Pure reshape/transpose (no copy of values changed), so this is exactly
    the same blocks the old per-block loop indexed, just batched."""
    cropped = grid[: bh * BLOCK, : bw * BLOCK]
    return cropped.reshape(bh, BLOCK, bw, BLOCK).transpose(0, 2, 1, 3).reshape(bh * bw, BLOCK, BLOCK)


def _grid_from_blocks(blocks: np.ndarray, bh: int, bw: int) -> np.ndarray:
    """Inverse of _blocks_from_grid: (bh*bw, BLOCK, BLOCK) back to a
    (bh*BLOCK, bw*BLOCK) grid."""
    return blocks.reshape(bh, bw, BLOCK, BLOCK).transpose(0, 2, 1, 3).reshape(bh * BLOCK, bw * BLOCK)


def _dct2_batch(blocks: np.ndarray) -> np.ndarray:
    """Same 2D DCT as _dct2, applied to every block in a (N, BLOCK, BLOCK)
    stack in one vectorized call instead of N Python-level calls -- see
    "why this is fast" in the module docstring's performance note."""
    return dct(dct(blocks, axis=-1, norm="ortho"), axis=-2, norm="ortho")


def _idct2_batch(blocks: np.ndarray) -> np.ndarray:
    return idct(idct(blocks, axis=-1, norm="ortho"), axis=-2, norm="ortho")


def _embed_bits(y: np.ndarray, stream: list[int], bh: int, bw: int, strength: float) -> np.ndarray:
    ll, detail = _dwt2(y)
    ll_out = ll.copy()
    n_blocks = bh * bw

    # Batched equivalent of the old "for by, for bx: dct one 8x8 block"
    # loop -- see PERFORMANCE NOTE in the module docstring. Mathematically
    # identical (verified to ~1e-13 against the old per-block loop); this
    # is purely a "do it in one vectorized call" change, not an algorithm
    # change.
    blocks = _blocks_from_grid(ll_out, bh, bw)
    coeffs = _dct2_batch(blocks)
    a = coeffs[:, COORD_A[0], COORD_A[1]]
    b = coeffs[:, COORD_B[0], COORD_B[1]]

    bits = np.asarray(stream[:n_blocks], dtype=np.float64)
    # bit==1 wants a-b >= strength; bit==0 wants b-a >= strength. Each
    # np.maximum(0, ...) reproduces the old "only push if under strength"
    # guard -- it's 0 (no-op) exactly when the original `if ... < strength`
    # check would have been False.
    push_for_1 = np.maximum(0.0, (strength - (a - b)) / 2)
    push_for_0 = np.maximum(0.0, (strength - (b - a)) / 2)
    is_one = bits == 1
    a_new = np.where(is_one, a + push_for_1, a - push_for_0)
    b_new = np.where(is_one, b - push_for_1, b + push_for_0)

    coeffs[:, COORD_A[0], COORD_A[1]] = a_new
    coeffs[:, COORD_B[0], COORD_B[1]] = b_new
    blocks_out = _idct2_batch(coeffs)
    ll_out[: bh * BLOCK, : bw * BLOCK] = _grid_from_blocks(blocks_out, bh, bw)

    y_out = _idwt2((ll_out, detail), y.shape)
    if y_out.shape != y.shape:
        # periodization mode round-trips exactly for even dimensions; odd
        # dimensions can come back 1px short in an axis -- pad by edge
        # replication rather than silently mismatch shapes.
        pad_h = max(y.shape[0] - y_out.shape[0], 0)
        pad_w = max(y.shape[1] - y_out.shape[1], 0)
        y_out = np.pad(y_out, ((0, pad_h), (0, pad_w)), mode="edge")
        y_out = y_out[: y.shape[0], : y.shape[1]]
    return y_out


def _decode_votes(y: np.ndarray, n_bits: int) -> tuple[list[list[int]], int]:
    ll = _ll_band(y)
    bh, bw = ll.shape[0] // BLOCK, ll.shape[1] // BLOCK
    n_blocks = bh * bw

    # Batched equivalent of the old per-block decode loop -- see
    # _embed_bits' comment and the module docstring's PERFORMANCE NOTE.
    blocks = _blocks_from_grid(ll, bh, bw)
    coeffs = _dct2_batch(blocks)
    a = coeffs[:, COORD_A[0], COORD_A[1]]
    b = coeffs[:, COORD_B[0], COORD_B[1]]
    decided = (a > b).astype(np.int64)  # 1 where original coded "votes[bit_pos][1] += 1"

    bit_positions = np.arange(n_blocks) % n_bits
    tally = np.zeros((n_bits, 2), dtype=np.int64)
    np.add.at(tally, (bit_positions, decided), 1)  # unbuffered add -- safe with repeated indices
    return tally.tolist(), n_blocks


def embed(image: Image.Image, payload: bytes, strength: float = 20.0) -> Image.Image:
    """Embed `payload` (bytes) into `image`. Internally wraps it with
    Reed-Solomon parity first, then repeats the RS-encoded bytes to fill
    every available DWT LL block.

    strength: separation forced between the coefficient pair, in the DWT
    LL domain. NOT numerically comparable to a plain pixel-domain DCT
    scheme's strength values -- a change to an LL coefficient affects a
    2x2 pixel region on reconstruction. 20.0 is the calibrated default,
    re-verified against real re-save/resize/repost
    simulations (an earlier pass at this number used easier test
    scenarios than that script's real ones and got a falsely low
    answer -- see pipeline.py's MODE_PRESETS comment for the full story).
    """
    rgb = image.convert("RGB")
    rgb_arr = np.asarray(rgb, dtype=np.float64)
    y = np.asarray(rgb.convert("YCbCr"), dtype=np.float64)[:, :, 0]

    encoded = bytes(_rs.encode(payload))
    bits = _bytes_to_bits(encoded)
    if not bits:
        raise ValueError("Payload must be at least 1 byte.")

    ll_h = pywt.dwt_coeff_len(y.shape[0], pywt.Wavelet(WAVELET).dec_len, mode=DWT_MODE)
    ll_w = pywt.dwt_coeff_len(y.shape[1], pywt.Wavelet(WAVELET).dec_len, mode=DWT_MODE)
    bh, bw = ll_h // BLOCK, ll_w // BLOCK
    n_blocks = bh * bw
    if n_blocks < len(bits):
        raise ValueError(
            f"Image too small to watermark this payload reliably: {n_blocks} DWT blocks available, "
            f"{len(bits)} bits needed (payload + Reed-Solomon parity, before any redundancy)."
        )

    reps = (n_blocks + len(bits) - 1) // len(bits)
    stream = (bits * reps)[:n_blocks]

    y_out = _embed_bits(y, stream, bh, bw, strength)
    # Add the luma change straight onto R, G and B instead of writing Y
    # back and converting YCbCr -> RGB. The full round trip quantizes
    # twice and measurably darkened images (about -1.4 mean luma);
    # adding the same delta to every channel leaves chroma alone and
    # has no systematic brightness shift. Decoding re-derives Y from the
    # resulting RGB, which still sees (almost exactly) this delta.
    delta = (y_out - y)[:, :, None]
    out = np.clip(np.rint(rgb_arr + delta), 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")


def _extract_raw(image: Image.Image, encoded_len_bytes: int) -> bytes:
    """Majority-vote decode of the RS-encoded byte stream, no RS
    correction applied yet."""
    ycbcr = np.array(image.convert("YCbCr"), dtype=np.float64)
    y = ycbcr[:, :, 0]
    n_bits = encoded_len_bytes * 8
    votes, n_blocks = _decode_votes(y, n_bits)
    if n_blocks < n_bits:
        raise ValueError("Image too small / low-res to recover a watermark this size.")
    bits = [1 if v1 >= v0 else 0 for v0, v1 in votes]
    return _bits_to_bytes(bits)


def extract(image: Image.Image, payload_len_bytes: int) -> bytes:
    """Recover a `payload_len_bytes`-byte payload: majority-vote decode
    across every repeated copy in the DWT LL sub-band, then Reed-Solomon
    error-correct the result. Blind -- no original needed.

    Never raises on a bad match (matches v1's contract, since callers
    compare the result against an expected value rather than expecting
    an exception): if Reed-Solomon can't correct the errors, falls back
    to the uncorrected majority-vote bytes, which is very likely wrong
    in that case, but keeps this function's return type predictable."""
    encoded_len = payload_len_bytes + RS_PARITY_BYTES
    raw = _extract_raw(image, encoded_len)
    try:
        corrected = bytes(_rs.decode(raw)[0])
        if len(corrected) == payload_len_bytes:
            return corrected
    except ReedSolomonError:
        pass
    return raw[:payload_len_bytes]


def extract_checked(image: Image.Image, payload_len_bytes: int) -> bytes | None:
    """Like extract(), but returns None instead of falling back to the
    uncorrected bytes when Reed-Solomon can't recover a valid payload
    (or the image has no room for one). Use this to *identify* an
    unknown image, where "no watermark" must be distinguishable from a
    garbage decode."""
    try:
        raw = _extract_raw(image, payload_len_bytes + RS_PARITY_BYTES)
        corrected = bytes(_rs.decode(raw)[0])
    except (ReedSolomonError, ValueError):
        return None
    return corrected if len(corrected) == payload_len_bytes else None


def extract_auto(image: Image.Image, payload_len_bytes: int, original_size: tuple[int, int] | None) -> bytes:
    """Like extract(), but if `original_size` is provided and the image
    has been resized since watermarking, resample back to that size
    first -- the watermark is tied to the DWT LL grid at the size it was
    embedded at, so this is what lets a resized, re-shared copy still be
    recognized."""
    if original_size and image.size != original_size:
        image = image.resize(original_size, Image.LANCZOS)
    return extract(image, payload_len_bytes)


def confidence(image: Image.Image, expected_payload: bytes) -> float:
    """Fraction of repeated copies (of the RS-encoded byte stream) that
    agree with the majority-vote result, as a rough 0..1 diagnostic
    score -- deliberately reports the *raw* bit-agreement rate, before
    Reed-Solomon correction, so it stays a smooth, informative signal
    of how close to failing a given extraction was (RS correction
    itself is all-or-nothing, which makes a poor confidence metric)."""
    encoded = bytes(_rs.encode(expected_payload))
    bits = _bytes_to_bits(encoded)
    ycbcr = np.array(image.convert("YCbCr"), dtype=np.float64)
    y = ycbcr[:, :, 0]
    votes, n_blocks = _decode_votes(y, len(bits))
    if n_blocks < len(bits):
        return 0.0
    agree = 0
    for bit_pos, (v0, v1) in enumerate(votes):
        decoded = 1 if v1 >= v0 else 0
        if decoded == bits[bit_pos]:
            agree += 1
    return agree / len(bits) if bits else 0.0


# Candidate center-crop percentages tried by recover_from_crop() -- see
# that function's docstring. Tuned empirically: fine enough to catch
# crops up to ~7% (where Reed-Solomon can still bridge the residual
# misalignment error), coarse enough that a verify call stays around
# 3-4 seconds even in the fallback path, not tens of seconds.
_CROP_RECOVERY_CANDIDATES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30]


def recover_from_crop(
    image: Image.Image, expected_payload: bytes, original_size: tuple[int, int]
) -> tuple[bytes | None, float]:
    """Fallback for when the fast path (extract_auto/confidence, which
    only corrects for a uniform resize) comes back with low confidence:
    try assuming the suspect image is a *centered* crop of the original
    at each of several candidate percentages, undo that hypothesis, and
    check confidence against the known expected payload for each one.

    Why this can work at all: there's no true geometric re-synchronization
    here (see the module docstring's "honest limits") -- this is a
    bounded brute-force search over ONE specific, common real-world crop
    pattern (symmetric center-crop, e.g. a platform's "crop to square" or
    a user trimming even borders), not a general fix for arbitrary crops,
    rotation, or perspective changes. Measured against
    center crops: recovers cleanly up to
    ~5-7% cropped from each side (worse for asymmetric crops -- an
    off-center or single-edge crop won't match any of these symmetric
    hypotheses well). Costs roughly 3-4 seconds (this function runs a
    full extraction pass per candidate) -- only call this after the fast
    path's confidence looks bad, not as the default path.

    Returns (best_decoded_payload_or_None, best_confidence). Caller
    decides what confidence counts as a match.
    """
    w, h = original_size
    best_conf = 0.0
    best_decoded = None
    for pct in _CROP_RECOVERY_CANDIDATES:
        inner_w = int(w * (1 - 2 * pct / 100))
        inner_h = int(h * (1 - 2 * pct / 100))
        off_x = int(w * pct / 100)
        off_y = int(h * pct / 100)
        if inner_w < 32 or inner_h < 32:
            continue
        resized = image.resize((inner_w, inner_h), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (128, 128, 128))
        canvas.paste(resized, (off_x, off_y))
        conf = confidence(canvas, expected_payload)
        if conf > best_conf:
            best_conf = conf
            best_decoded = extract(canvas, len(expected_payload))
    return best_decoded, best_conf

