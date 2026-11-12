import json
import os
import uuid
from datetime import datetime, timezone

from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory
from PIL import Image
from werkzeug.utils import secure_filename

from config import Config
from core import cloak, evidence, jobs, notices, pipeline, provenance, tracker
from models.db import CASE_STATUSES, Case, Match, ProtectedImage, Settings, db


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    for path in (
        app.config["INSTANCE_DIR"], app.config["UPLOAD_DIR"], app.config["OUTPUT_DIR"],
        app.config["ORIGINALS_DIR"], app.config["EVIDENCE_DIR"],
    ):
        os.makedirs(path, exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _fail_stale_records(app)

    register_routes(app)
    return app


_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _fail_stale_records(app: Flask) -> None:
    """Jobs live in memory, so anything still "processing" at boot lost
    its worker in a crash or restart and would otherwise show as
    processing forever. Also clears orphaned upload temp files."""
    stale = ProtectedImage.query.filter(ProtectedImage.status.in_(("queued", "processing"))).all()
    for record in stale:
        record.status = "failed"
        record.error_message = "Interrupted when Ward was closed. Upload it again."
    if stale:
        db.session.commit()
    upload_dir = app.config["UPLOAD_DIR"]
    for name in os.listdir(upload_dir):
        try:
            os.remove(os.path.join(upload_dir, name))
        except OSError:
            pass


def _display_name(filename: str) -> str:
    """The uploaded name for display only (never used as a path): path
    components dropped, length capped."""
    return os.path.basename((filename or "").replace("\\", "/"))[:255] or "upload"


def _save_upload(file, app: Flask, prefix: str):
    """Save an uploaded file under a sanitized name. Returns
    (temp_path, error). Enforces the per-file size cap here, since
    Flask's own limit applies to the whole request."""
    safe_name = secure_filename(file.filename or "") or "upload"
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > app.config["MAX_FILE_BYTES"]:
        return None, f"File too large (max {app.config['MAX_FILE_BYTES'] // (1024 * 1024)}MB each)."
    if size == 0:
        return None, "File is empty."
    temp_path = os.path.join(app.config["UPLOAD_DIR"], f"{prefix}_{safe_name}")
    file.save(temp_path)
    return temp_path, None


def _allowed_file(filename: str, app: Flask) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def _current_api_key(app: Flask, request_key: str = "") -> str:
    """Precedence: a key passed for this one request > the key saved in
    Settings > the key in .env."""
    return (request_key or "").strip() or Settings.get().serpapi_key_override or app.config["SERPAPI_API_KEY"]


def _apply_protect_result(record: ProtectedImage, result: dict) -> None:
    """Writes a pipeline.run_protect() result onto a ProtectedImage row.
    Does not commit; caller does, so it can be batched with other changes."""
    record.width = result["width"]
    record.height = result["height"]
    record.has_alpha = result["has_alpha"]
    record.protected_filename = result["protected_filename"]
    record.thumb_filename = result["thumb_filename"]
    record.watermark_hex = result["watermark_hex"]
    record.watermark_bytes_len = result["watermark_bytes_len"]
    record.cloak_hamming = result["cloak_hamming"]
    record.cloak_bits_total = result["cloak_bits_total"]
    record.cloak_mean_delta = result["cloak_mean_delta"]
    record.cloak_max_delta = result["cloak_max_delta"]
    record.faces_detected = result["faces_detected"]
    record.face_boxes_json = json.dumps(result["face_boxes"]) if result["face_boxes"] else None
    record.gps_stripped = result["gps_stripped"]
    record.ai_training_opt_out_tagged = result["ai_training_opt_out_tagged"]
    if result["original_stored_filename"]:
        record.original_stored_filename = result["original_stored_filename"]
    record.status = "protected"


def _apply_provenance(app: Flask, record: ProtectedImage, protected_path: str, creator_name: str) -> None:
    """Best-effort: attaches provenance to a just-protected image, real
    C2PA if a cert is configured, else a local-only record (see
    core/provenance.py). Never raises -- provenance is additive, and a
    failure here (e.g. a misconfigured cert) shouldn't fail the whole
    protect job when the watermark/cloak already succeeded."""
    cert_path, key_path = app.config.get("C2PA_CERT_PATH"), app.config.get("C2PA_KEY_PATH")
    try:
        if cert_path and key_path and provenance.c2pa_available():
            signed_path = protected_path + ".c2pa_tmp"
            provenance.create_c2pa_manifest(
                protected_path, signed_path, cert_path, key_path, record.id, creator_name=creator_name,
            )
            os.replace(signed_path, protected_path)  # embed manifest in place
            record.provenance_kind = "c2pa"
            record.provenance_json = json.dumps({"note": "Real C2PA manifest embedded in the protected file."})
        else:
            local_record = provenance.create_local_record(
                protected_path, app.config["INSTANCE_DIR"], record.id, creator_name=creator_name,
            )
            record.provenance_kind = "local"
            record.provenance_json = json.dumps(local_record)
    except provenance.ProvenanceError:
        pass  # leave provenance_kind/json as None -- UI shows "not generated"


def _run_protect_job(job_id: str, app: Flask, temp_path: str, db_id: str, strength: float, mode: str):
    """Runs in a background thread -- needs its own app context to touch
    the database or read app.config."""
    with app.app_context():
        jobs.set_message(job_id, "Detecting faces and applying watermark + cloak...")
        try:
            settings = Settings.get()
            result = pipeline.run_protect(
                temp_path, app.config["OUTPUT_DIR"], tracking_id=db_id, strength=strength, mode=mode,
                max_dimension=pipeline.MAX_DIMENSION,
                keep_original=settings.keep_originals,
                originals_dir=app.config["ORIGINALS_DIR"],
                tag_no_ai_training=settings.tag_no_ai_training,
            )
            record = db.session.get(ProtectedImage, db_id)
            _apply_protect_result(record, result)
            jobs.set_message(job_id, "Attaching provenance record...")
            protected_path = os.path.join(app.config["OUTPUT_DIR"], result["protected_filename"])
            _apply_provenance(app, record, protected_path, settings.artist_name or "")
            db.session.commit()
            jobs.set_message(job_id, "Done.")
            return {"image_id": db_id}
        except Exception as e:
            record = db.session.get(ProtectedImage, db_id)
            if record:
                record.status = "failed"
                record.error_message = str(e)
                db.session.commit()
            raise
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


def _track_one_image(app: Flask, record: ProtectedImage, api_key: str) -> dict:
    """Run a reverse-image search on `record`'s protected file, file any
    matches not already recorded (matched by link, so re-scanning doesn't
    duplicate them), best-effort capture evidence + a confidence score
    for each new one, and update last_scanned_at."""
    img_path = os.path.join(app.config["OUTPUT_DIR"], record.protected_filename)
    with Image.open(img_path) as opened:
        opened.load()
        image = opened.copy()
    protected_bits = cloak.phash_bits(image)
    result = tracker.search_by_upload(image, api_key)

    known_links = {m.link for m in record.matches}
    new_matches = []
    for m in result.matches:
        if m.link in known_links:
            continue
        known_links.add(m.link)
        match = Match(
            image_id=record.id, title=m.title, link=m.link, source=m.source,
            thumbnail=m.thumbnail, image_url=m.image,
        )
        db.session.add(match)
        db.session.flush()  # assigns match.id, needed for the evidence filename below

        try:
            ev = evidence.capture_evidence(
                m.thumbnail or m.image, app.config["EVIDENCE_DIR"], record.id, match.id,
            )
            match.evidence_filename = ev.stored_filename
            match.evidence_sha256 = ev.sha256
            match.evidence_fetched_at = datetime.now(timezone.utc)
            evidence_path = os.path.join(app.config["EVIDENCE_DIR"], ev.stored_filename)
            with Image.open(evidence_path) as evidence_img:
                evidence_bits = cloak.phash_bits(evidence_img)
            hamming = cloak.hamming(protected_bits, evidence_bits)
            match.confidence_score = 1.0 - (hamming / len(protected_bits))
        except Exception:
            pass  # best-effort -- a dead/unreachable thumbnail link shouldn't fail the whole scan

        new_matches.append(match)

    record.last_scanned_at = datetime.now(timezone.utc)
    db.session.commit()

    return {
        "match_count": len(new_matches),
        "total_matches": len(record.matches),
        "matches": [m.to_dict() for m in new_matches],
    }


def register_routes(app: Flask):
    @app.before_request
    def _guard_local_only():
        """Ward is a local app with no login. Without this, any web page
        open in the same browser could POST to http://127.0.0.1:<port>
        (cross-site request), and a DNS-rebinding page could read the
        API. Require a loopback Host header, and for state-changing
        requests an Origin (when the browser sends one) on that same host."""
        hostname = urlsplit("//" + (request.host or "")).hostname
        if hostname not in _LOCAL_HOSTNAMES:
            abort(400, description="Ward only answers on localhost.")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403, description="Cross-origin request blocked.")

    @app.get("/")
    def index():
        settings = Settings.get()
        return render_template(
            "index.html",
            tracker_configured=bool(settings.serpapi_key_override or app.config["SERPAPI_API_KEY"]),
        )

    @app.get("/api/config")
    def api_config():
        settings = Settings.get()
        return jsonify({
            "tracker_configured": bool(settings.serpapi_key_override or app.config["SERPAPI_API_KEY"]),
            "max_dimension": pipeline.MAX_DIMENSION,
            "case_types": list(notices.VALID_CASE_TYPES),
            "case_statuses": list(CASE_STATUSES),
            "c2pa_configured": bool(app.config.get("C2PA_CERT_PATH") and app.config.get("C2PA_KEY_PATH")),
        })

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def api_get_settings():
        return jsonify(Settings.get().to_dict())

    @app.post("/api/settings")
    def api_update_settings():
        settings = Settings.get()
        data = request.get_json(silent=True) or {}
        for field in ("artist_name", "contact_email", "portfolio_url", "mailing_address", "serpapi_key_override"):
            if field in data:
                value = (data[field] or "").strip()
                setattr(settings, field, value or None)
        if "keep_originals" in data:
            settings.keep_originals = bool(data["keep_originals"])
        if "tag_no_ai_training" in data:
            settings.tag_no_ai_training = bool(data["tag_no_ai_training"])
        db.session.commit()
        return jsonify(settings.to_dict())

    @app.get("/api/settings/provenance-key")
    def api_provenance_key():
        """The local provenance signing key's PUBLIC half, safe to share
        (e.g. post it on your portfolio) -- see core/provenance.py."""
        return jsonify({"public_key_b64": provenance.local_public_key_b64(app.config["INSTANCE_DIR"])})

    # ---------------------------------------------------------------- protect
    @app.post("/api/protect")
    def api_protect():
        if "image" not in request.files:
            return jsonify({"error": "No image file provided (field name must be 'image')."}), 400
        file = request.files["image"]
        if not file.filename:
            return jsonify({"error": "No file selected."}), 400
        if not _allowed_file(file.filename, app):
            return jsonify({"error": "Unsupported file type. Use PNG, JPG, WEBP, BMP, or TIFF."}), 400

        strength = request.form.get("strength", default=1.0, type=float) or 1.0
        strength = max(0.4, min(strength, 2.0))

        mode = request.form.get("mode", default=pipeline.DEFAULT_MODE)
        if mode not in pipeline.MODE_PRESETS:
            return jsonify({"error": f"Invalid mode. Must be one of {list(pipeline.MODE_PRESETS)}."}), 400

        db_id = pipeline.new_tracking_id()
        temp_path, error = _save_upload(file, app, db_id)
        if error:
            return jsonify({"error": error}), 413 if "too large" in error else 400

        record = ProtectedImage(id=db_id, original_filename=_display_name(file.filename), status="processing", mode=mode)
        db.session.add(record)
        db.session.commit()

        job_id = jobs.submit(_run_protect_job, app, temp_path, db_id, strength, mode)
        return jsonify({"job_id": job_id, "image_id": db_id}), 202

    @app.post("/api/protect/bulk")
    def api_protect_bulk():
        """Upload and protect several images in one request instead of
        one-at-a-time -- free for everyone. Each file gets its own
        job/record -- this just saves the repeated round trips of the
        single-image flow."""
        files = request.files.getlist("images")
        if not files:
            return jsonify({"error": "No image files provided (field name must be 'images')."}), 400

        strength = request.form.get("strength", default=1.0, type=float) or 1.0
        strength = max(0.4, min(strength, 2.0))
        mode = request.form.get("mode", default=pipeline.DEFAULT_MODE)
        if mode not in pipeline.MODE_PRESETS:
            return jsonify({"error": f"Invalid mode. Must be one of {list(pipeline.MODE_PRESETS)}."}), 400

        if len(files) > app.config["MAX_BATCH_FILES"]:
            return jsonify({"error": f"Up to {app.config['MAX_BATCH_FILES']} images per batch."}), 400

        submitted = []
        errors = []
        for file in files:
            if not file.filename:
                continue
            if not _allowed_file(file.filename, app):
                errors.append({"filename": file.filename, "error": "Unsupported file type."})
                continue
            db_id = pipeline.new_tracking_id()
            temp_path, error = _save_upload(file, app, db_id)
            if error:
                errors.append({"filename": file.filename, "error": error})
                continue
            record = ProtectedImage(id=db_id, original_filename=_display_name(file.filename), status="processing", mode=mode)
            db.session.add(record)
            db.session.commit()
            job_id = jobs.submit(_run_protect_job, app, temp_path, db_id, strength, mode)
            submitted.append({"job_id": job_id, "image_id": db_id, "filename": file.filename})

        return jsonify({"submitted": submitted, "errors": errors}), 202

    @app.get("/api/jobs/<job_id>")
    def api_job_status(job_id):
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown job id."}), 404
        return jsonify(job)

    # ----------------------------------------------------------------- images
    @app.get("/api/images")
    def api_list_images():
        records = ProtectedImage.query.order_by(ProtectedImage.created_at.desc()).all()
        return jsonify([r.to_summary() for r in records])

    @app.get("/api/images/<image_id>")
    def api_image_detail(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        return jsonify(record.to_detail())

    @app.delete("/api/images/<image_id>")
    def api_delete_image(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        for fname in (record.protected_filename, record.thumb_filename):
            if fname:
                fpath = os.path.join(app.config["OUTPUT_DIR"], fname)
                if os.path.exists(fpath):
                    os.remove(fpath)
        if record.original_stored_filename:
            opath = os.path.join(app.config["ORIGINALS_DIR"], record.original_stored_filename)
            if os.path.exists(opath):
                os.remove(opath)
        for match in record.matches:
            if match.evidence_filename:
                epath = os.path.join(app.config["EVIDENCE_DIR"], match.evidence_filename)
                if os.path.exists(epath):
                    os.remove(epath)
        db.session.delete(record)
        db.session.commit()
        return jsonify({"deleted": True})

    @app.get("/api/images/<image_id>/download")
    def api_download_image(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None or not record.protected_filename:
            abort(404)
        # The protected file may be a different format than the upload
        # (non-transparent images are saved as JPEG), so name the download
        # after what's actually inside it.
        stem = os.path.splitext(secure_filename(record.original_filename or "") or "protected")[0]
        ext = os.path.splitext(record.protected_filename)[1]
        return send_from_directory(
            app.config["OUTPUT_DIR"], record.protected_filename, as_attachment=True,
            download_name=f"ward_{record.id}_{stem}{ext}",
        )

    @app.get("/api/images/<image_id>/thumb")
    def api_thumb_image(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None or not record.thumb_filename:
            abort(404)
        return send_from_directory(app.config["OUTPUT_DIR"], record.thumb_filename)

    @app.get("/api/images/<image_id>/original")
    def api_download_original(image_id):
        """"Remove protection" / "switch tools": hands back the exact
        file that was uploaded, before Ward touched it -- no watermark,
        no cloak. Only available if Settings > Keep originals was on
        when this image was protected. See pipeline.run_protect's
        docstring for why this is a stored copy rather than an attempt
        to mathematically un-cloak the protected file."""
        record = db.session.get(ProtectedImage, image_id)
        if record is None or not record.original_stored_filename:
            return jsonify({
                "error": "No original was retained for this image (Settings > Keep originals may "
                         "have been off when it was protected)."
            }), 404
        return send_from_directory(
            app.config["ORIGINALS_DIR"], record.original_stored_filename, as_attachment=True,
            download_name=record.original_filename or f"ward_{record.id}_original",
        )

    @app.get("/api/images/<image_id>/provenance")
    def api_image_provenance(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None:
            return jsonify({"error": "Not found."}), 404
        if not record.provenance_kind:
            return jsonify({"kind": None, "note": "No provenance record was generated for this image."})
        payload = json.loads(record.provenance_json) if record.provenance_json else None
        return jsonify({"kind": record.provenance_kind, "record": payload})

    # ---------------------------------------------------------------- tracking
    @app.post("/api/images/<image_id>/track")
    def api_track_image(image_id):
        record = db.session.get(ProtectedImage, image_id)
        if record is None or not record.protected_filename:
            return jsonify({"error": "Not found."}), 404

        request_key = (request.form.get("api_key") or "").strip()
        api_key = _current_api_key(app, request_key)
        if not tracker.is_configured(api_key):
            return jsonify({
                "error": "No SerpApi key configured. Add one under Settings, pass one for this "
                         "request, or set SERPAPI_API_KEY in your .env -- see README."
            }), 400

        try:
            result = _track_one_image(app, record, api_key)
        except tracker.TrackerError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify(result)

    @app.get("/api/matches/<int:match_id>/evidence")
    def api_match_evidence(match_id):
        match = db.session.get(Match, match_id)
        if match is None or not match.evidence_filename:
            return jsonify({"error": "No evidence captured for this match."}), 404
        return send_from_directory(app.config["EVIDENCE_DIR"], match.evidence_filename)

    # -------------------------------------------------------------------- cases
    @app.post("/api/cases")
    def api_create_case():
        """Open a takedown case, usually against a specific match (pass
        match_id) but freeform cases (target_url only, no match_id) are
        allowed too -- see models.db.Case's docstring."""
        data = request.get_json(silent=True) or {}
        image_id = data.get("image_id")
        record = db.session.get(ProtectedImage, image_id) if image_id else None
        if record is None:
            return jsonify({"error": "Unknown or missing image_id."}), 404

        case_type = data.get("case_type", "copyright")
        if case_type not in notices.VALID_CASE_TYPES:
            return jsonify({"error": f"case_type must be one of {notices.VALID_CASE_TYPES}."}), 400

        match = None
        match_id = data.get("match_id")
        if match_id is not None:
            match = next((m for m in record.matches if m.id == match_id), None)
            if match is None:
                return jsonify({"error": "Unknown match_id for this image."}), 404

        target_url = data.get("target_url") or (match.link or match.image_url if match else "")
        if not target_url:
            return jsonify({"error": "Provide target_url, or match_id for a match that has a link."}), 400
        target_platform = data.get("target_platform") or (match.source if match else "") or ""

        settings = Settings.get()
        notice_text = notices.build_notice(
            case_type=case_type,
            original_filename=record.original_filename or "",
            tracking_id=record.id,
            infringing_url=target_url,
            infringing_source=target_platform,
            found_at=match.found_at.strftime("%Y-%m-%d") if match and match.found_at else "",
            reporter_name=settings.artist_name or "",
            contact_email=settings.contact_email or "",
            portfolio_url=settings.portfolio_url or "",
            mailing_address=settings.mailing_address or "",
            platform=target_platform,
            violation_description=data.get("violation_description", ""),
        )

        case = Case(
            image_id=record.id, match_id=match.id if match else None, case_type=case_type,
            target_platform=target_platform, target_url=target_url, notice_text=notice_text,
            notes=data.get("notes") or None,
        )
        db.session.add(case)
        db.session.commit()
        return jsonify(case.to_dict()), 201

    @app.get("/api/cases/<int:case_id>")
    def api_get_case(case_id):
        case = db.session.get(Case, case_id)
        if case is None:
            return jsonify({"error": "Not found."}), 404
        return jsonify(case.to_dict())

    @app.post("/api/cases/<int:case_id>")
    def api_update_case(case_id):
        """Update a case's status/notes/platform, or regenerate its
        notice text (e.g. after editing your Settings profile). Status
        transitions set the matching timestamp automatically (sent_at,
        resolved_at) -- one field to change in the UI, not three."""
        case = db.session.get(Case, case_id)
        if case is None:
            return jsonify({"error": "Not found."}), 404
        data = request.get_json(silent=True) or {}

        if "status" in data:
            new_status = data["status"]
            if new_status not in CASE_STATUSES:
                return jsonify({"error": f"status must be one of {CASE_STATUSES}."}), 400
            case.status = new_status
            now = datetime.now(timezone.utc)
            if new_status == "sent" and not case.sent_at:
                case.sent_at = now
            if new_status in ("removed", "rejected"):
                case.resolved_at = now

        if "log_followup" in data and data["log_followup"]:
            case.last_followup_at = datetime.now(timezone.utc)
        if "notes" in data:
            case.notes = data["notes"] or None
        if "target_platform" in data:
            case.target_platform = data["target_platform"] or None

        db.session.commit()
        return jsonify(case.to_dict())

    @app.delete("/api/cases/<int:case_id>")
    def api_delete_case(case_id):
        case = db.session.get(Case, case_id)
        if case is None:
            return jsonify({"error": "Not found."}), 404
        db.session.delete(case)
        db.session.commit()
        return jsonify({"deleted": True})

    @app.get("/api/cases/<int:case_id>/notice")
    def api_case_notice(case_id):
        case = db.session.get(Case, case_id)
        if case is None or not case.notice_text:
            return jsonify({"error": "Not found."}), 404
        return Response(
            case.notice_text, mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{case.case_type}_notice_{case.id}.txt"'},
        )

    # -------------------------------------------------------------- verify
    @app.post("/api/verify")
    def api_verify():
        """Check a 'suspect' image against Ward's watermark. With a
        tracking_id, tests the image against that one record (this also
        copes with resized or center-cropped copies). Without one,
        identifies it from the watermark alone, since the watermark
        payload IS the tracking ID -- works for copies at original size."""
        if "image" not in request.files:
            return jsonify({"error": "No image file provided (field name must be 'image')."}), 400
        tracking_code = request.form.get("tracking_id", "").strip().lower()

        record = None
        if tracking_code:
            record = db.session.get(ProtectedImage, tracking_code)
            if record is None or not record.watermark_hex:
                return jsonify({"error": "Unknown or not-yet-protected tracking id."}), 404

        file = request.files["image"]
        temp_path, error = _save_upload(file, app, f"verify_{uuid.uuid4().hex}")
        if error:
            return jsonify({"error": error}), 413 if "too large" in error else 400
        try:
            if record is not None:
                original_size = (record.width, record.height) if record.width and record.height else None
                result = pipeline.verify_watermark(temp_path, record.watermark_hex, original_size)
                result["tracking_id"] = record.id
            else:
                found = pipeline.identify_watermark(temp_path)
                known = db.session.get(ProtectedImage, found) if found else None
                result = {
                    "identified": known is not None,
                    "tracking_id": known.id if known else None,
                    "original_filename": known.original_filename if known else None,
                }
        except pipeline.PipelineError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return jsonify(result)

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "Upload too large."}), 413


app = create_app()

if __name__ == "__main__":
    # debug=False by default: Flask's debug mode exposes an interactive
    # Python console in the browser on any unhandled exception. Set
    # FLASK_DEBUG=1 only while developing Ward itself. desktop.py is what
    # start.bat/start.sh launch and always runs with debug off.
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip() == "1"
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=debug_mode)
