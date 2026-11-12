import os
import secrets

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _get_or_create_secret_key() -> str:
    """SECRET_KEY, in order: explicit env var > a random key persisted
    per-install > generate and persist a new one.

    Deliberately never falls back to a hardcoded default. A shared,
    identical SECRET_KEY baked into every copy of a publicly distributed
    app is a real, well-known vulnerability (anyone who reads the source
    -- which anyone can, since it's public -- would have every install's
    key), not just a "change this before deploying" reminder easy to
    skip. This needs zero user action to avoid: first run generates a
    random key and writes it to instance/.secret_key (not checked into
    git -- see .gitignore); every run after that reuses it."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    os.makedirs(_INSTANCE_DIR, exist_ok=True)
    key_path = os.path.join(_INSTANCE_DIR, ".secret_key")
    if os.path.exists(key_path):
        with open(key_path, "r") as f:
            existing = f.read().strip()
        if existing:
            return existing

    new_key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(new_key)
    try:
        os.chmod(key_path, 0o600)  # best-effort; not all filesystems support POSIX perms
    except OSError:
        pass
    return new_key


class Config:
    SECRET_KEY = _get_or_create_secret_key()

    INSTANCE_DIR = _INSTANCE_DIR
    # The images Ward is tracking, kept so "Check for sightings" can be
    # re-run later, plus a small JPEG preview of each for the gallery.
    STORAGE_DIR = os.path.join(BASE_DIR, "images")
    THUMB_DIR = os.path.join(BASE_DIR, "thumbnails")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'ward.db')}"
    )

    # 25MB upload cap -- generous for artwork, small enough to keep a
    # single-process dev server responsive. Raise if you need to.
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024

    ALLOWED_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "bmp", "tiff"})

    SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()

    HOST = "127.0.0.1"
    PORT = int(os.environ.get("WARD_PORT", "5000") or 5000)
