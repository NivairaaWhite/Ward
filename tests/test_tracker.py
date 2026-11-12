import os

import pytest
from PIL import Image

from core import tracker


def test_compress_noisy_large_image_fits_limit():
    noisy = Image.frombytes("RGB", (4000, 3000), os.urandom(4000 * 3000 * 3))
    assert len(tracker._compress_for_upload(noisy)) <= tracker.MAX_UPLOAD_BYTES


def test_compress_handles_rgba_and_tiny_images():
    assert tracker._compress_for_upload(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))


def test_parse_exact_matches_dedupes_and_drops_linkless():
    data = {"exact_matches": [
        {"position": 1, "title": "a", "link": "http://a"},
        {"position": 2, "title": "a again", "link": "http://a"},
        {"position": 3, "title": "no link"},
        {"position": 4, "title": "b", "link": "http://b", "source": "B"},
    ]}
    result = tracker._parse_exact_matches(data)
    assert [m.link for m in result.matches] == ["http://a", "http://b"]
    assert result.matches[0].confidence == 100  # position 1
    assert result.matches[1].confidence == tracker._confidence_from_position(4)


def test_parse_raises_on_serpapi_error():
    with pytest.raises(tracker.TrackerError):
        tracker._parse_exact_matches({"error": "Invalid API key"})


def test_search_requires_key():
    with pytest.raises(tracker.TrackerError):
        tracker.search_by_upload(Image.new("RGB", (4, 4)), "")


def test_confidence_from_position():
    assert tracker._confidence_from_position(1) == 100
    assert tracker._confidence_from_position(2) == 93
    assert tracker._confidence_from_position(15) == 15  # floored
    assert tracker._confidence_from_position(0) == 100  # treated as 1
