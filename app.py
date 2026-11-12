import logging
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func

from config import Config
from core import images, tracker
from models.db import Match, Settings, TrackedImage, db, upgrade_schema

log = logging.getLogger(__name__)

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
IMAGE_ERRORS = (UnidentifiedImageError, OSError, Image.DecompressionBombError)


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    for path in (app.config["INSTANCE_DIR"], app.config["STORAGE_DIR"], app.config["THUMB_DIR"]):
        os.makedirs(path, exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        upgrade_schema()

    register_security(app)
    register_routes(app)
    return app


def _hostname(netloc: str) -> str:
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):  # IPv6 literal, e.g. [::1]:5000
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def register_security(app: Flask):
    """Ward only ever talks to a browser on this machine. Rejecting any other
    Host header blocks DNS-rebinding pages, and rejecting foreign Origins on
    writes blocks other websites from POSTing to localhost."""

    @app.before_request
    def _local_only():
        if _hostname(request.host) not in LOCAL_HOSTNAMES:
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin != "null" and _hostname(urlparse(origin).netloc) not in LOCAL_HOSTNAMES:
                abort(403)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp


def _allowed_file(filename: str, app: Flask) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def _current_api_key(app: Flask) -> str:
    """The key saved in Settings wins over the one in .env."""
    return Settings.get().serpapi_key_override or app.config["SERPAPI_API_KEY"]


def register_routes(app: Flask):
    @app.get("/")
    def index():
        return render_template("index.html", tracker_configured=bool(_current_api_key(app)))

    @app.get("/api/config")
    def api_config():
        return jsonify({"tracker_configured": bool(_current_api_key(app))})

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def api_get_settings():
        return jsonify(Settings.get().to_dict())

    @app.post("/api/settings")
    def api_update_settings():
        settings = Settings.get()
        data = request.get_json(silent=True) or {}
        if "serpapi_key_override" in data:
            value = (data["serpapi_key_override"] or "").strip()
            settings.serpapi_key_override = value or None
        db.session.commit()
        return jsonify(settings.to_dict())

    # -------------------------------------------------------------- images
    @app.post("/api/images")
    def api_add_image():
        if "image" not in request.files:
            return jsonify({"error": "No image file provided (field name must be 'image')."}), 400
        file = request.files["image"]
        if not file.filename:
            return jsonify({"error": "No file selected."}), 400
        if not _allowed_file(file.filename, app):
            return jsonify({"error": "Unsupported file type. Use PNG, JPG, WEBP, BMP, or TIFF."}), 400

        db_id = uuid.uuid4().hex[:12]
        try:
            saved = images.save_tracked_image(file, app.config["STORAGE_DIR"], app.config["THUMB_DIR"], db_id)
        except IMAGE_ERRORS as e:
            return jsonify({"error": f"Could not read that image: {e}"}), 400

        record = TrackedImage(
            id=db_id,
            original_filename=file.filename[:255],
            width=saved["width"],
            height=saved["height"],
            stored_filename=saved["stored_filename"],
            thumb_filename=saved["thumb_filename"],
        )
        try:
            db.session.add(record)
            db.session.commit()
        except Exception:
            db.session.rollback()
            images.remove_files(
                app.config["STORAGE_DIR"], app.config["THUMB_DIR"],
                saved["stored_filename"], saved["thumb_filename"],
            )
            log.exception("Could not save tracked image record")
            return jsonify({"error": "Could not save the image. Please try again."}), 500
        return jsonify(record.to_detail()), 201

    @app.get("/api/images")
    def api_list_images():
        counts = dict(db.session.query(Match.image_id, func.count(Match.id)).group_by(Match.image_id).all())
        records = TrackedImage.query.order_by(TrackedImage.created_at.desc()).all()
        return jsonify([r.to_summary(match_count=counts.get(r.id, 0)) for r in records])

    @app.get("/api/images/<image_id>")
    def api_image_detail(image_id):
        record = db.session.get(TrackedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        return jsonify(record.to_detail())

    @app.delete("/api/images/<image_id>")
    def api_delete_image(image_id):
        record = db.session.get(TrackedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        stored, thumb = record.stored_filename, record.thumb_filename
        db.session.delete(record)
        db.session.commit()  # database first: a failure here leaves everything intact
        images.remove_files(app.config["STORAGE_DIR"], app.config["THUMB_DIR"], stored, thumb)
        return jsonify({"deleted": True})

    @app.get("/api/images/<image_id>/file")
    def api_image_file(image_id):
        record = db.session.get(TrackedImage, image_id)
        if record is None or not record.stored_filename:
            return jsonify({"error": "Not found."}), 404
        return send_from_directory(
            app.config["STORAGE_DIR"], record.stored_filename,
            download_name=record.original_filename or record.stored_filename,
        )

    @app.get("/api/images/<image_id>/thumb")
    def api_image_thumb(image_id):
        record = db.session.get(TrackedImage, image_id)
        if record is None or not record.thumb_filename:
            return jsonify({"error": "Not found."}), 404
        return send_from_directory(app.config["THUMB_DIR"], record.thumb_filename)

    @app.get("/api/images/<image_id>/export.csv")
    def api_export_matches(image_id):
        import csv
        import io

        record = db.session.get(TrackedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["title", "source", "link", "confidence", "first_seen_utc", "last_seen_utc"])
        for m in record.matches:
            # Neutralise spreadsheet formula injection from third-party text.
            def safe(v):
                v = v or ""
                return "'" + v if v[:1] in ("=", "+", "-", "@") else v
            writer.writerow([
                safe(m.title), safe(m.source), m.link,
                m.confidence if m.confidence is not None else "",
                m.found_at, m.last_seen_at,
            ])
        return app.response_class(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="ward-{record.id}-matches.csv"'},
        )

    # ------------------------------------------------------------- tracking
    @app.post("/api/images/<image_id>/track")
    def api_track_image(image_id):
        record = db.session.get(TrackedImage, image_id)
        if record is None or not record.stored_filename:
            return jsonify({"error": "Not found."}), 404

        api_key = _current_api_key(app)
        if not tracker.is_configured(api_key):
            return jsonify({"error": "No SerpApi key configured. Add one under Settings, "
                                     "or set SERPAPI_API_KEY in your .env -- see README."}), 400

        img_path = os.path.join(app.config["STORAGE_DIR"], record.stored_filename)
        try:
            with Image.open(img_path) as image:
                result = tracker.search_by_upload(image, api_key)
        except tracker.TrackerError as e:
            return jsonify({"error": str(e)}), 502
        except IMAGE_ERRORS as e:
            return jsonify({"error": f"Could not open the stored image: {e}"}), 500

        # Upsert by link: repeat sightings only refresh last_seen_at, so the
        # count means distinct pages and "new" means genuinely new.
        now = datetime.now(timezone.utc)
        existing = {m.link: m for m in record.matches}
        new_count = 0
        for m in result.matches:
            row = existing.get(m.link)
            if row is not None:
                row.last_seen_at = now
                # Refresh rank-derived confidence on every sighting
                if m.confidence:
                    row.confidence = m.confidence
                continue
            row = Match(
                image_id=record.id,
                title=m.title[:500],
                link=m.link,
                source=m.source[:255],
                thumbnail=m.thumbnail,
                image_url=m.image,
                confidence=m.confidence or None,
                found_at=now,
                last_seen_at=now,
            )
            db.session.add(row)
            existing[m.link] = row
            new_count += 1

        record.last_scanned_at = now
        record.scan_count = (record.scan_count or 0) + 1
        record.last_new_count = new_count
        db.session.commit()

        return jsonify({"new_count": new_count, "total_matches": len(existing)})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "File too large (max 25MB)."}), 413

    @app.errorhandler(403)
    def forbidden(_e):
        return jsonify({"error": "Forbidden: Ward only accepts requests from this computer."}), 403


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip() == "1"
    flask_app = create_app()
    flask_app.run(host=Config.HOST, port=Config.PORT, debug=debug_mode)
