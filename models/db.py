from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()


def _now():
    return datetime.now(timezone.utc)


def iso(dt):
    """ISO-8601 with an explicit UTC offset. SQLite drops tzinfo, so a naive
    value coming back from the DB is UTC by construction -- without the
    offset, browsers would parse it as local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class TrackedImage(db.Model):
    """An image Ward is watching. "Check for sightings" runs a reverse-image
    search against it and files the results as Match rows."""

    __tablename__ = "tracked_images"

    id = db.Column(db.String(32), primary_key=True)  # the tracking code shown to the user
    original_filename = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_now)

    width = db.Column(db.Integer)
    height = db.Column(db.Integer)

    stored_filename = db.Column(db.String(255))
    thumb_filename = db.Column(db.String(255))

    # Updated only when the user clicks "Check for sightings" -- Ward has no
    # background scanning.
    last_scanned_at = db.Column(db.DateTime, nullable=True)
    scan_count = db.Column(db.Integer, default=0)
    last_new_count = db.Column(db.Integer, default=0)  # matches first seen in the latest scan

    matches = db.relationship(
        "Match", backref="image", cascade="all, delete-orphan", order_by="desc(Match.found_at)"
    )

    def to_summary(self, match_count: int | None = None) -> dict:
        """`match_count` lets list views pass in a precomputed count so the
        matches relationship isn't loaded once per image."""
        if match_count is None:
            match_count = len(self.matches)
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "created_at": iso(self.created_at),
            "thumb_url": f"/api/images/{self.id}/thumb" if self.thumb_filename else None,
            "width": self.width,
            "height": self.height,
            "match_count": match_count,
            "new_count": (self.last_new_count or 0) if (self.scan_count or 0) > 1 else 0,
            "last_scanned_at": iso(self.last_scanned_at),
            "scan_count": self.scan_count or 0,
        }

    def to_detail(self) -> dict:
        d = self.to_summary()
        repeat_scan = (self.scan_count or 0) > 1
        d.update(
            {
                "file_url": f"/api/images/{self.id}/file" if self.stored_filename else None,
                "matches": [
                    m.to_dict(is_new=repeat_scan and m.found_at == self.last_scanned_at)
                    for m in self.matches
                ],
            }
        )
        return d


class Match(db.Model):
    __tablename__ = "matches"
    __table_args__ = (db.UniqueConstraint("image_id", "link", name="uq_match_image_link"),)

    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.String(32), db.ForeignKey("tracked_images.id"), nullable=False)

    title = db.Column(db.String(500))
    link = db.Column(db.Text)
    source = db.Column(db.String(255))
    thumbnail = db.Column(db.Text)
    image_url = db.Column(db.Text)
    # Rank-derived confidence (15–100). Higher = Google Lens ranked it earlier.
    confidence = db.Column(db.Integer, nullable=True)
    found_at = db.Column(db.DateTime, default=_now)  # first time this link was seen
    last_seen_at = db.Column(db.DateTime, default=_now)  # most recent scan that still listed it

    def to_dict(self, is_new: bool = False) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "thumbnail": self.thumbnail,
            "image_url": self.image_url,
            "confidence": self.confidence,
            "found_at": iso(self.found_at),
            "last_seen_at": iso(self.last_seen_at),
            "is_new": is_new,
        }


class Settings(db.Model):
    """Singleton row (id is always 1): an optional SerpApi key entered in the
    UI instead of .env."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True, default=1)
    serpapi_key_override = db.Column(db.String(255), nullable=True)

    @classmethod
    def get(cls) -> "Settings":
        row = db.session.get(cls, 1)
        if row is None:
            row = cls(id=1)
            db.session.add(row)
            db.session.commit()
        return row

    def to_dict(self) -> dict:
        return {"has_serpapi_override": bool(self.serpapi_key_override)}


def upgrade_schema() -> None:
    """Bring a database created by an older Ward up to date, in place.

    `create_all()` never alters existing tables, so older installs would crash
    on the new columns. This adds them, removes duplicate matches that earlier
    versions stored on every scan, and then enforces uniqueness. Idempotent and
    safe to run on every start; it keeps the user's data.
    """
    engine = db.engine
    insp = inspect(engine)
    tables = insp.get_table_names()

    with engine.begin() as conn:
        if "tracked_images" in tables:
            cols = {c["name"] for c in insp.get_columns("tracked_images")}
            if "last_new_count" not in cols:
                conn.execute(text("ALTER TABLE tracked_images ADD COLUMN last_new_count INTEGER DEFAULT 0"))

        if "matches" in tables:
            cols = {c["name"] for c in insp.get_columns("matches")}
            if "last_seen_at" not in cols:
                conn.execute(text("ALTER TABLE matches ADD COLUMN last_seen_at DATETIME"))
                conn.execute(text("UPDATE matches SET last_seen_at = found_at"))
            if "confidence" not in cols:
                conn.execute(text("ALTER TABLE matches ADD COLUMN confidence INTEGER"))
            # Keep the earliest row per (image, link); drop repeats and link-less rows.
            conn.execute(text("DELETE FROM matches WHERE link IS NULL OR link = ''"))
            conn.execute(text(
                "DELETE FROM matches WHERE id NOT IN "
                "(SELECT MIN(id) FROM matches GROUP BY image_id, link)"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_match_image_link ON matches (image_id, link)"
            ))
