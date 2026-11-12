import json
import sqlite3
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """WAL lets the background protect workers write while the UI reads,
    instead of 'database is locked' errors under batch uploads."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def _now():
    return datetime.now(timezone.utc)


class ProtectedImage(db.Model):
    __tablename__ = "protected_images"

    id = db.Column(db.String(32), primary_key=True)  # also the tracking code shown to the user
    original_filename = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_now)

    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    has_alpha = db.Column(db.Boolean, default=False)

    status = db.Column(db.String(20), default="queued")  # queued -> processing -> protected | failed
    error_message = db.Column(db.Text, nullable=True)
    mode = db.Column(db.String(20), default="art")  # "art" or "portrait" -- see core/pipeline.py MODE_PRESETS

    protected_filename = db.Column(db.String(255), nullable=True)
    thumb_filename = db.Column(db.String(255), nullable=True)

    watermark_hex = db.Column(db.String(32), nullable=True)
    watermark_bytes_len = db.Column(db.Integer, nullable=True)

    cloak_hamming = db.Column(db.Integer, nullable=True)
    cloak_bits_total = db.Column(db.Integer, nullable=True)
    cloak_mean_delta = db.Column(db.Float, nullable=True)
    cloak_max_delta = db.Column(db.Float, nullable=True)

    faces_detected = db.Column(db.Integer, default=0)
    face_boxes_json = db.Column(db.Text, nullable=True)  # JSON list -- see to_detail()

    gps_stripped = db.Column(db.Boolean, default=False)  # True iff the source file had GPS EXIF that was dropped
    ai_training_opt_out_tagged = db.Column(db.Boolean, default=False)  # True iff the IPTC/PLUS Data Mining tag was embedded
    original_stored_filename = db.Column(db.String(255), nullable=True)  # set iff Settings.keep_originals was on

    # Set whenever "Check for sightings" runs (manual, user-triggered --
    # see /api/images/<id>/track). Ward never scans in the background.
    last_scanned_at = db.Column(db.DateTime, nullable=True)

    # Provenance (see core/provenance.py). provenance_kind is "c2pa" or
    # "local" -- which path was actually used for THIS image, since
    # whether a real cert was configured can change between images
    # protected at different times. provenance_json holds the local-
    # record dict (kind == "local") or a small pointer/summary for the
    # c2pa case (the real manifest lives inside the protected file
    # itself, not duplicated here).
    provenance_kind = db.Column(db.String(10), nullable=True)  # "c2pa" | "local" | None (not generated)
    provenance_json = db.Column(db.Text, nullable=True)

    matches = db.relationship(
        "Match", backref="image", cascade="all, delete-orphan", order_by="desc(Match.found_at)"
    )
    cases = db.relationship(
        "Case", backref="image", cascade="all, delete-orphan", order_by="desc(Case.created_at)"
    )

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "status": self.status,
            "error_message": self.error_message,
            "mode": self.mode,
            "thumb_url": f"/api/images/{self.id}/thumb" if self.thumb_filename else None,
            "cloak_hamming": self.cloak_hamming,
            "cloak_bits_total": self.cloak_bits_total,
            "match_count": len(self.matches) if self.matches is not None else 0,
            "open_case_count": sum(1 for c in self.cases if c.status not in ("removed", "rejected")) if self.cases is not None else 0,
            "faces_detected": self.faces_detected or 0,
            "has_original": bool(self.original_stored_filename),
            "provenance_kind": self.provenance_kind,
        }

    def to_detail(self) -> dict:
        d = self.to_summary()
        d.update(
            {
                "width": self.width,
                "height": self.height,
                "watermark_hex": self.watermark_hex,
                "cloak_mean_delta": self.cloak_mean_delta,
                "cloak_max_delta": self.cloak_max_delta,
                "face_boxes": json.loads(self.face_boxes_json) if self.face_boxes_json else [],
                "gps_stripped": bool(self.gps_stripped),
                "ai_training_opt_out_tagged": bool(self.ai_training_opt_out_tagged),
                "last_scanned_at": self.last_scanned_at.isoformat() if self.last_scanned_at else None,
                "download_url": f"/api/images/{self.id}/download" if self.protected_filename else None,
                "original_url": f"/api/images/{self.id}/original" if self.original_stored_filename else None,
                "provenance": json.loads(self.provenance_json) if self.provenance_json else None,
                "matches": [m.to_dict() for m in self.matches],
                "cases": [c.to_dict() for c in self.cases],
            }
        )
        return d


class Match(db.Model):
    __tablename__ = "matches"

    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.String(32), db.ForeignKey("protected_images.id"), nullable=False)

    title = db.Column(db.String(500))
    link = db.Column(db.Text)
    source = db.Column(db.String(255))
    thumbnail = db.Column(db.Text)
    image_url = db.Column(db.Text)
    found_at = db.Column(db.DateTime, default=_now)

    # Match confidence (see app.py's track route): how close this
    # match's own thumbnail's perceptual hash is to the protected file's
    # -- 0..1, None if never computed (e.g. evidence capture failed, so
    # there was nothing to hash). Ward computes this itself against the
    # file it actually protected; SerpApi supplies no such score.
    confidence_score = db.Column(db.Float, nullable=True)

    # Evidence (see core/evidence.py) -- a locally-stored copy of this
    # match's thumbnail, captured at a point in time, independent of
    # whether the live link still shows the same thing later.
    evidence_filename = db.Column(db.String(255), nullable=True)
    evidence_sha256 = db.Column(db.String(64), nullable=True)
    evidence_fetched_at = db.Column(db.DateTime, nullable=True)

    cases = db.relationship("Case", backref="match", order_by="desc(Case.created_at)")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "thumbnail": self.thumbnail,
            "image_url": self.image_url,
            "found_at": self.found_at.isoformat() if self.found_at else None,
            "confidence_score": round(self.confidence_score, 3) if self.confidence_score is not None else None,
            "evidence_url": f"/api/matches/{self.id}/evidence" if self.evidence_filename else None,
            "evidence_sha256": self.evidence_sha256,
            "evidence_fetched_at": self.evidence_fetched_at.isoformat() if self.evidence_fetched_at else None,
        }


# Case lifecycle. Deliberately linear/small rather than a generic
# workflow engine -- these are the states a takedown report actually
# passes through, matching what a person filing one needs to track:
# did I send it, did anyone confirm receipt, did the content actually
# come down, or did they say no.
CASE_STATUSES = ("drafted", "sent", "acknowledged", "removed", "rejected")


class Case(db.Model):
    """One takedown report you're pursuing against one piece of
    infringing content. Usually created from a specific Match (the
    normal flow: find a sighting, open a case on it), but match_id is
    nullable so you can also log a case for something you found outside
    Ward's own search (e.g. someone tipped you off directly)."""

    __tablename__ = "cases"

    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.String(32), db.ForeignKey("protected_images.id"), nullable=False)
    match_id = db.Column(db.Integer, db.ForeignKey("matches.id"), nullable=True)

    case_type = db.Column(db.String(12), default="copyright")  # see core/notices.VALID_CASE_TYPES
    status = db.Column(db.String(12), default="drafted")  # see CASE_STATUSES
    target_platform = db.Column(db.String(255), nullable=True)
    target_url = db.Column(db.Text, nullable=True)  # kept even if match_id is set, for freeform cases

    notice_text = db.Column(db.Text, nullable=True)  # the generated notice, frozen at creation time
    notes = db.Column(db.Text, nullable=True)  # your own freeform follow-up/escalation notes

    created_at = db.Column(db.DateTime, default=_now)
    sent_at = db.Column(db.DateTime, nullable=True)
    last_followup_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)  # set when status becomes removed/rejected

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "image_id": self.image_id,
            "match_id": self.match_id,
            "case_type": self.case_type,
            "status": self.status,
            "target_platform": self.target_platform,
            "target_url": self.target_url,
            "notice_text": self.notice_text,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "last_followup_at": self.last_followup_at.isoformat() if self.last_followup_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class Settings(db.Model):
    """Singleton row (id is always 1) holding local, single-user settings:
    the artist's own profile (used to fill in takedown notices) and an
    optional SerpApi key stored via the UI instead of .env. Ward has no
    plan/license concept -- every feature below is free for everyone."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True, default=1)

    # Used to fill in the "Complaining Party" / contact section of a
    # generated notice -- never sent anywhere by Ward itself.
    artist_name = db.Column(db.String(255), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    portfolio_url = db.Column(db.String(500), nullable=True)
    mailing_address = db.Column(db.Text, nullable=True)

    # Optional override for SERPAPI_API_KEY from .env -- lets the app be
    # configured entirely from the Settings UI instead of editing files.
    serpapi_key_override = db.Column(db.String(255), nullable=True)

    # If True, protect jobs stash an exact copy of the uploaded original so
    # the user can always get an unmarked file back later (see
    # /api/images/<id>/original). If False, only the protected file is kept
    # -- smaller disk footprint, but "restore original" won't be available
    # for images protected while this was off.
    keep_originals = db.Column(db.Boolean, default=True)

    # If True, protected outputs get the real IPTC/PLUS "Data Mining"
    # XMP tag declaring them prohibited for AI/ML training -- see
    # core/metadata_tags.py for exactly what this does and doesn't
    # guarantee (it's a declared preference, not a technical barrier).
    tag_no_ai_training = db.Column(db.Boolean, default=True)

    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    @classmethod
    def get(cls) -> "Settings":
        row = db.session.get(cls, 1)
        if row is None:
            row = cls(id=1)
            db.session.add(row)
            db.session.commit()
        return row

    def to_dict(self) -> dict:
        return {
            "artist_name": self.artist_name,
            "contact_email": self.contact_email,
            "portfolio_url": self.portfolio_url,
            "mailing_address": self.mailing_address,
            "has_serpapi_override": bool(self.serpapi_key_override),
            "keep_originals": self.keep_originals if self.keep_originals is not None else True,
            "tag_no_ai_training": self.tag_no_ai_training if self.tag_no_ai_training is not None else True,
        }
