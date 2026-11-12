import sqlite3
from unittest import mock

from core import tracker
from tests.conftest import png_bytes


def fake_result(*links):
    return tracker.SearchResult(matches=[tracker.Match(title=l, link=l) for l in links])


def scan(client, iid, result):
    with mock.patch.object(tracker, "search_by_upload", return_value=result):
        return client.post(f"/api/images/{iid}/track")


def test_upload_list_detail_delete(client, app, uploaded_id):
    assert client.get("/api/images").get_json()[0]["id"] == uploaded_id
    assert client.get(f"/api/images/{uploaded_id}/thumb").status_code == 200
    assert client.delete(f"/api/images/{uploaded_id}").status_code == 200
    assert client.get(f"/api/images/{uploaded_id}").status_code == 404
    import os
    assert os.listdir(app.config["STORAGE_DIR"]) == [] and os.listdir(app.config["THUMB_DIR"]) == []


def test_rejects_bad_uploads(client):
    r = client.post("/api/images", data={"image": (png_bytes(), "x.exe")}, content_type="multipart/form-data")
    assert r.status_code == 400
    import io
    r = client.post("/api/images", data={"image": (io.BytesIO(b"not an image"), "x.png")}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_scan_without_key_is_clear_error(client, uploaded_id):
    r = client.post(f"/api/images/{uploaded_id}/track")
    assert r.status_code == 400 and "SerpApi" in r.get_json()["error"]


def test_repeat_scans_do_not_duplicate_and_report_new(client, uploaded_id):
    client.post("/api/settings", json={"serpapi_key_override": "k"})
    assert scan(client, uploaded_id, fake_result("http://a", "http://b")).get_json()["new_count"] == 2
    assert scan(client, uploaded_id, fake_result("http://a", "http://b")).get_json()["new_count"] == 0
    assert scan(client, uploaded_id, fake_result("http://a", "http://b", "http://c")).get_json()["new_count"] == 1

    d = client.get(f"/api/images/{uploaded_id}").get_json()
    assert len(d["matches"]) == 3 and d["scan_count"] == 3 and d["new_count"] == 1
    assert [m["link"] for m in d["matches"] if m["is_new"]] == ["http://c"]
    assert client.get("/api/images").get_json()[0]["match_count"] == 3


def test_timestamps_carry_utc_offset(client, uploaded_id):
    client.post("/api/settings", json={"serpapi_key_override": "k"})
    scan(client, uploaded_id, fake_result("http://a"))
    d = client.get(f"/api/images/{uploaded_id}").get_json()
    assert d["last_scanned_at"].endswith("+00:00") and d["created_at"].endswith("+00:00")


def test_tracker_error_surfaces_as_502(client, uploaded_id):
    client.post("/api/settings", json={"serpapi_key_override": "k"})
    with mock.patch.object(tracker, "search_by_upload", side_effect=tracker.TrackerError("quota")):
        r = client.post(f"/api/images/{uploaded_id}/track")
    assert r.status_code == 502 and "quota" in r.get_json()["error"]


def test_csv_export_neutralises_formulas(client, uploaded_id):
    client.post("/api/settings", json={"serpapi_key_override": "k"})
    res = tracker.SearchResult(matches=[tracker.Match(title="=HYPERLINK(1)", link="http://a")])
    scan(client, uploaded_id, res)
    r = client.get(f"/api/images/{uploaded_id}/export.csv")
    assert r.status_code == 200 and "'=HYPERLINK" in r.get_data(as_text=True)


def test_rejects_foreign_host_and_origin(client, uploaded_id):
    assert client.get("/api/images", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/settings", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/images", headers={"Host": "127.0.0.1:5000"}).status_code == 200
    assert client.get("/api/images", headers={"Host": "[::1]:5000"}).status_code == 200


def test_old_database_is_migrated_and_deduped(tmp_path):
    """A DB made by the previous Ward (no new columns, duplicate matches) must open cleanly."""
    from app import create_app
    from config import Config

    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE tracked_images (id VARCHAR(32) PRIMARY KEY, original_filename VARCHAR(255), created_at DATETIME,
            width INTEGER, height INTEGER, stored_filename VARCHAR(255), thumb_filename VARCHAR(255),
            last_scanned_at DATETIME, scan_count INTEGER);
        CREATE TABLE matches (id INTEGER PRIMARY KEY, image_id VARCHAR(32), title VARCHAR(500), link TEXT,
            source VARCHAR(255), thumbnail TEXT, image_url TEXT, exact_match BOOLEAN, found_at DATETIME);
        CREATE TABLE settings (id INTEGER PRIMARY KEY, serpapi_key_override VARCHAR(255), updated_at DATETIME);
        INSERT INTO tracked_images (id, scan_count) VALUES ('img1', 3);
        INSERT INTO matches (image_id, link, found_at) VALUES ('img1','http://a','2026-01-01'),('img1','http://a','2026-01-02'),('img1','http://b','2026-01-01');
    """)
    con.commit(); con.close()

    class OldCfg(Config):
        TESTING = True
        INSTANCE_DIR = str(tmp_path / "i"); STORAGE_DIR = str(tmp_path / "s"); THUMB_DIR = str(tmp_path / "t")
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"

    app = create_app(OldCfg)
    create_app(OldCfg)  # idempotent on a second start
    d = app.test_client().get("/api/images/img1").get_json()
    assert sorted(m["link"] for m in d["matches"]) == ["http://a", "http://b"]
