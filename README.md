# Ward — Image Tracking

**Ward watches images for you.**

Upload an image, then click **Check for sightings** whenever you want to run a reverse-image search through **SerpApi / Google Lens** and see where else it appears online.

> **Upload → Store → Search → Track**

## What it does

* 📤 Upload PNG/JPG/WEBP/BMP/TIFF images up to **25 MB**
* 💾 Store originals and thumbnails locally
* 🔎 Manually search an image using Google Lens **exact matches** only (no similar/visual results)
* 📊 Rank-based **confidence** score (15–100%) on each match, derived from Google Lens ordering
* 🗂️ Save and view previous matches — each page is listed once, with **"new since last scan"** badges
* 🔗 View match sources, links and thumbnails
* 📄 Export an image's matches to CSV (includes confidence)
* ⬇️ Download your original image
* 🗑️ Delete tracked images whenever you want
* 🔒 No background or scheduled scanning

Ward does **not** include watermarking, cloaking, face detection, or takedown generation.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Add your SerpApi key:

```env
SERPAPI_API_KEY=your_key_here
```

Or enter it through Ward's **Settings** panel.

Get a SerpApi key at:

https://serpapi.com/

You can run Ward without a key, but image tracking requires one.

## Run

Easiest: double-click `start.bat` (Windows) or run `bash start.sh` (macOS/Linux). They set everything up on first run, only reinstall when `requirements.txt` changes (so they work offline afterwards), and open Ward for you.

Or run it yourself:

```bash
python desktop.py   # picks a free port and opens a window/browser tab
python app.py       # dev server on http://127.0.0.1:5000 (set WARD_PORT to change)
```

`desktop.py` uses `pywebview` for a native window if you `pip install pywebview`; otherwise it opens your browser. Upgrading from an older Ward? Your existing database is migrated automatically and repeated matches are merged — nothing to delete.

## Privacy & safety

* Ward only answers requests addressed to `localhost` / `127.0.0.1`, and rejects writes from other websites.
* No third-party fonts or scripts are loaded; the only outbound traffic is to SerpApi when you click **Check for sightings**.
* Your SerpApi key is stored in the local database in plain text — fine for a personal machine; don't expose Ward on a network.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

## Local Storage

Ward keeps its data locally:

```text
images/              Original uploads
thumbnails/          Gallery previews
instance/ward.db     SQLite database
instance/.secret_key Local application secret
```

**Nothing is sent to a remote Ward server.**

When you click **Check for sightings**, the selected image is sent to SerpApi for the requested search. External services are subject to their own terms and policies.

## Project Structure

```text
app.py
config.py
desktop.py

core/
  images.py
  tracker.py

models/
  db.py

tests/

templates/
  index.html

static/
  js/app.js
  css/style.css
```

## The Larger Ward

Ward Image Tracking is the **small, focused version** of a larger Ward project.

The larger Ward model is designed as a broader **image-protection and provenance platform**, with capabilities such as:

* Watermarking
* Perceptual-hash cloaking/disruption
* Face-aware protection
* Metadata/provenance
* Takedown case tracking
* Image tracking and sightings

This repository intentionally focuses only on the **tracking layer**, keeping it lightweight and easy to run.

## License

Copyright © 2026 **Mahasvin S S**.

Ward is free to use for personal and commercial purposes under the **Ward Software License**.

See [`LICENSE`](LICENSE) for the complete terms.

Third-party dependencies remain under their respective licenses.
