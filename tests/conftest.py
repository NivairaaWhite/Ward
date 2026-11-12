import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from config import Config


@pytest.fixture()
def app(tmp_path):
    class TestConfig(Config):
        TESTING = True
        INSTANCE_DIR = str(tmp_path / "instance")
        STORAGE_DIR = str(tmp_path / "images")
        THUMB_DIR = str(tmp_path / "thumbs")
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'test.db'}"
        SERPAPI_API_KEY = ""

    return create_app(TestConfig)


@pytest.fixture()
def client(app):
    return app.test_client()


def png_bytes(size=(120, 80), color="red"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    buf.seek(0)
    return buf


@pytest.fixture()
def uploaded_id(client):
    r = client.post("/api/images", data={"image": (png_bytes(), "x.png")}, content_type="multipart/form-data")
    assert r.status_code == 201
    return r.get_json()["id"]
