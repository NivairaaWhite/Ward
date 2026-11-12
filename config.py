import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


class Config:
    INSTANCE_DIR = _INSTANCE_DIR
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
    OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
    # Exact copies of uploaded originals, kept only so a user can get an
    # unmarked file back later -- see models.db.Settings.keep_originals and
    # /api/images/<id>/original. Deliberately separate from OUTPUT_DIR/
    # UPLOAD_DIR so it's never accidentally served by a route meant for
    # protected deliverables.
    ORIGINALS_DIR = os.path.join(BASE_DIR, "originals")
    # Locally-captured copies of match thumbnails (see core/evidence.py),
    # kept separate from the three dirs above since these are copies of
    # *other people's* pages, not anything Ward produced or was given by
    # the user to protect.
    EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'ward.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Per-file cap, checked per uploaded file in app.py. MAX_CONTENT_LENGTH
    # is Flask's whole-request cap, so it has to cover a full batch of
    # files (the old single 25MB cap rejected any multi-file batch whose
    # total went over it).
    MAX_FILE_BYTES = 25 * 1024 * 1024
    MAX_BATCH_FILES = 20
    MAX_CONTENT_LENGTH = MAX_FILE_BYTES * MAX_BATCH_FILES

    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}

    SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()

    # Optional: your own CA-issued C2PA signing certificate + key (PEM
    # files). See core/provenance.py's module docstring for why Ward
    # can't include one itself and what happens without it (a clearly-
    # labeled local-only provenance record instead of real C2PA).
    C2PA_CERT_PATH = os.environ.get("C2PA_CERT_PATH", "").strip() or None
    C2PA_KEY_PATH = os.environ.get("C2PA_KEY_PATH", "").strip() or None
