# Ward

A local, self-hosted tool for marking your artwork or photos before you post
them, tracking where they turn up later, and filing a takedown when they
turn up somewhere they shouldn't. Upload a piece, Ward embeds a hidden
tracking code and shifts its digital fingerprint, and hands you back a file
that looks the same to the eye but reads differently to a machine.

Runs entirely on your own computer — no account, no cloud, no bill, no
license key. **Every feature is free**, including batch upload and case
tracking. The only optional cost is a free SerpApi key if you want the
"search the web for this" feature (see below).

## Quickstart

**Windows:** double-click `start.bat`. **Mac/Linux:** double-click
`start.sh`, or run `bash start.sh` from a terminal. Either script creates a
virtual environment on first run, installs dependencies, and opens Ward in
your browser automatically.

If you'd rather do it by hand (or you're on a headless server):

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # optional -- see "Settings" below
python app.py            # or: python desktop.py (picks a free port, opens a window/tab)
```

Either way: open the address Ward prints (normally **http://127.0.0.1:5000**) and drop an image on the page.
Nothing to configure — Ward runs fully with zero setup. Runs on CPU only;
no GPU, no multi-gigabyte model download, no internet connection required
for protection itself.

## How it works

Every protected image gets these applied to it, all running locally on
ordinary CPU:

**1. An invisible tracking watermark** (`core/watermark.py`) — a short code
is embedded using a DWT-DCT scheme (a wavelet transform isolates the
image's low-frequency band, the part that survives resizing and
re-compression, and the code is written into that band via DCT
coefficient modulation, wrapped in Reed-Solomon error correction). The
payload *is* the image's tracking ID, so **Check an image you found**
can identify a copy from its watermark alone (no code needed) when it's
still at its original size, or test it against a specific code to also
handle resized and centre-cropped copies.

**2. A perceptual-hash disruption, or "cloak"** (`core/cloak.py`) — many
large-scale scrapers don't run a full neural network on every image; they
use cheap perceptual hashes (pHash/aHash/dHash-style fingerprints) to match
and cluster images instead. Ward nudges your image's pixels just enough to
push its perceptual hash far from the original, while keeping the visible
change small. Every protect result reports the actual before/after Hamming
distance and average pixel shift, so this is a number you can check, not a
claim to take on faith. (This is deliberately the lightweight, dependency-
light version of "AI-training protection" — see Disadvantages for what it
isn't.)

**3. A face-aware pass**, if a face is detected (`core/faces.py`) — a
cropped-out face gets reverse-searched on its own by profile-picture
scrapers and dating-app tools, and the whole-image cloak above doesn't
carry over to a tight crop's hash. Ward runs a local Haar-cascade face
detector (ships inside OpenCV, no download) and gives each detected face its own
localized cloak pass. This finds *where* faces are, not *whose* they are —
it has nothing to do with defeating face recognition (see Disadvantages).

**4. A provenance record** (`core/provenance.py`) — either a real C2PA
manifest (if you've configured your own CA-issued certificate) or, by
default, a local record Ward signs itself: your name, the tracking ID, and
the file's own hash, timestamped and signed with a keypair generated on
first use. The UI always labels which one you got — a local record is
useful (it's tamper-evident and independently checkable against its own
published public key) but it is **not** the same thing as a real,
externally-verifiable C2PA "Content Credential," and Ward never calls it
that. See that module's docstring for exactly why self-signed certificates
are rejected by the C2PA standard itself, not just by Ward.

Alongside protection, Ward can also:

- **Track sightings** — "Check for sightings" searches the web via
  [SerpApi](https://serpapi.com)'s Google Lens API for anywhere a protected
  image shows up (needs your own free key — see below). Each match also
  gets a locally-captured copy of its thumbnail (`core/evidence.py`) and an
  independent confidence score — Ward re-hashes that captured thumbnail
  itself and compares it to your protected file's own hash. Re-scanning
  never duplicates sightings you've already recorded.
- **Open and track takedown cases** — one click on any match drafts a
  notice (copyright/DMCA, privacy/NCII, or a generic policy-violation
  report — see `core/notices.py`) filled in from that sighting plus your
  profile, and files it as a Case you can move through drafted → sent →
  acknowledged → removed/rejected, with follow-up logging and downloadable
  notice text at every stage. Freeform cases (no prior match needed) work
  too, if someone tips you off directly.
- **Strip metadata** — every output is re-encoded from pixel data only, so
  EXIF, GPS coordinates, and camera serials never carry into the protected
  file. Phone-photo rotation is applied to the pixels first, and the
  colour profile (ICC) is kept so wide-gamut art doesn't shift colour.
- **Tag images "no AI training"** — embeds the real IPTC/PLUS "Data Mining"
  opt-out property (on by default, toggle in Settings).
- **Keep your original** — on by default; lets you download the exact
  unmarked file back later, or switch to a different tool entirely.

## Advantages

- **Genuinely free and local.** No account, no server, no recurring cost,
  nothing phoning home. Your images never leave your machine except
  through the reverse-image search you explicitly trigger (and, per-match,
  the one-time evidence-thumbnail fetch that comes with it).
- **Runs on any computer.** CPU-only, lightweight dependencies (Pillow,
  NumPy, SciPy, OpenCV, PyWavelets) — no GPU, no large model download, no
  dedicated hardware. (C2PA support is the one optional dependency that
  isn't needed unless you configure a real certificate — see Settings.)
- **Numbers, not just claims.** Every protect result reports the actual
  Hamming distance, pixel shift, and face count; every match reports an
  independently-recomputed confidence score, not just an inherited flag.
- **Batch-friendly.** Protect up to 20 images in one upload.
- **You keep control.** Nothing runs in the background — no scheduled
  scanning, no silent re-protection, no case ever sent anywhere by Ward
  itself (it drafts and tracks; you send).

## Disadvantages / limitations

Being direct about this matters more than sounding impressive:

- **Not adversarial-ML-strength.** The cloak does not poison what a
  diffusion model learns the way real [Glaze](https://glaze.cs.uchicago.edu)
  or [Nightshade](https://nightshade.cs.uchicago.edu) do — those run
  gradient-based optimization against an actual vision-encoder network,
  which needs a several-hundred-megabyte model this build deliberately
  doesn't bundle. If you want that specifically, WebGlaze or
  [Mist](https://github.com/psyker-team/mist) are real options.
- **Does not defeat facial recognition.** Ward's face pass only *finds*
  faces to cloak them the same way the rest of the image is cloaked — it
  is not [Fawkes](https://sandlab.cs.uchicago.edu/fawkes/)-style identity
  protection, and there's no mode pretending otherwise.
- **Does not protect against AI video or deepfake generation.**
- **Watermark has real limits.** It survives typical social-media
  re-compression and moderate resizing, but below roughly 300px
  (thumbnail territory) recovery gets unreliable, and it isn't built to
  survive aggressive cropping or a screenshot of a screenshot.
- **Face detection can miss faces.** It's a classical Haar cascade, not a
  neural detector — it will miss faces at extreme angles, low light, or
  very small in frame.
- **The AI-training opt-out tag is a declared preference, not
  enforcement** — like robots.txt, nothing stops a platform from ignoring
  or stripping it on upload.
- **The default provenance record is not C2PA.** It's a real, independently
  verifiable signature, but only against its own published key — it will
  not show up as a verified "Content Credential" on contentcredentials.org
  or in any C2PA-aware tool. Getting that requires buying a certificate
  from an actual C2PA Trust List CA; Ward can use one if you have it, but
  can't include one.
- **Evidence capture is a thumbnail, not a full-page screenshot.** The
  original design for this also wanted an automated full-page-screenshot
  capture (via a headless browser); that specifically needs an unrestricted
  environment to install and wasn't something this build could verify
  actually works, so it isn't included. What's here — a locally-stored copy
  of the matched thumbnail plus its hash and fetch time — is fully working
  and tested, just a smaller form of evidence than a screenshot would be.
- **Case notices are templates, not legal filings.** Every generated notice
  says so at the bottom: review before sending, and confirm the right
  reporting channel for the specific platform. Ward doesn't send anything
  on your behalf.
- **Single-user, single-machine by design.** No multi-user accounts, no
  distributed processing, no cloud database — see "Security & scope" below.
- **If you're dealing with non-consensual intimate imagery already shared
  without consent**, Ward's cloak is the wrong tool for *preventing* that
  (it deters *future* scraping) — the "Privacy / NCII" case type will help
  you draft and track a report, and points you at
  [StopNCII.org](https://stopncii.org) and NCMEC's
  [Take It Down](https://takeitdown.ncmec.org) alongside it, which are the
  actual dedicated services for this.

## Settings

- **Artist profile** (name, email, portfolio, mailing address) — only used
  to fill in generated case notices; never sent anywhere by Ward itself.
- **Reverse-image search** — optional SerpApi key. Get a free one at
  [serpapi.com](https://serpapi.com/manage-api-key) (their free tier covers
  a limited number of searches per month) and paste it under Settings, or
  set `SERPAPI_API_KEY` in `.env`. Without a key, everything else still
  works — you just won't see "Check for sightings" results.
- **Keep unmarked originals** (default on) — lets you get your original
  file back later, at the cost of a second copy on disk per image.
- **Tag "no AI training"** (default on) — embeds the IPTC opt-out property.
- **Provenance** — shows the public half of Ward's local signing key (safe
  to publish alongside your work). Set `C2PA_CERT_PATH`/`C2PA_KEY_PATH` in
  `.env` instead if you have your own real C2PA certificate.

## Security & scope

Ward is a **single-user, local-only application** — no login system, no
per-user accounts. Anyone who can reach the server can see, protect, or
delete every image on it.

- **Running it as `start.bat`/`start.sh`/`python app.py` on your own
  computer** is fine as-is — it's bound to `127.0.0.1` (localhost) by
  default, so nothing outside your machine can reach it.
- **Don't expose one instance to a network or the public internet**
  without adding your own authentication layer first (e.g. a reverse
  proxy with HTTP basic auth) — Ward has none built in, on purpose, to
  stay a zero-config local tool.
- Ward checks the `Host` and `Origin` headers, so other websites open in
  your browser can't send requests to it and DNS-rebinding pages are
  refused. The provenance signing key (`instance/.provenance_key`) is
  generated per install. Don't commit `.env` or `instance/` to version
  control.

## Limits

- 25MB per file (up to 20 files per batch), 8000px max dimension
  (adjustable in `config.py`).
- If port 5000 is taken (macOS AirPlay Receiver uses it), `start.sh`/
  `start.bat` pick another free port automatically.
- Minimum image size roughly 300×300 for reliable watermarking.
- Single-machine, single-process by design.

## Project layout

```
app.py                    Flask routes
config.py                 Configuration (.env, upload limits, paths)
desktop.py                launcher: starts the server, opens it for you
start.bat, start.sh       double-click entry points (Windows / Mac-Linux)
core/
  watermark.py            DWT-DCT watermark + Reed-Solomon (embed/extract)
  cloak.py                perceptual-hash disruption (+ face-local pass)
  faces.py                Haar-cascade face detection (OpenCV's cascades)
  metadata_tags.py        IPTC/PLUS "Data Mining" AI opt-out XMP tag
  pipeline.py             wires protection together: transparency, originals, metadata
  provenance.py           C2PA manifest (real cert) or local signed record (fallback)
  tracker.py              SerpApi Google Lens integration
  evidence.py             downloads + hashes a match's thumbnail as local evidence
  notices.py              copyright/NCII/generic takedown notice templates
  jobs.py                 background thread-pool job runner
models/db.py              SQLAlchemy models (ProtectedImage, Match, Case, Settings)
templates/, static/       the dashboard UI
```

## License

See `LICENSE` — free for personal and commercial use of your own copy;
redistributing or reselling Ward itself isn't permitted. Not a substitute
for an attorney if you need one — every generated notice says this too.

## Why this exists

Glaze, Nightshade, and Fawkes are real, valuable academic projects — but as
of this writing they ship as GPU-hungry desktop tools or an invite-only
hosted service. Full commercial image-protection suites (the kind with
adversarial ML, cloud monitoring, and enterprise case management) are
another real category, aimed at teams and budgets this isn't. Ward isn't a
replacement for either — it's what's honestly buildable as a single
lightweight, local-first app that runs on whatever computer you already
have: real watermarking and hash-disruption, real reverse-image search, and
a real (if template-based, human-reviewed) path from "I found my art
somewhere it shouldn't be" to a filed, tracked case. Where it can't
honestly do what the heavier tools do — adversarial ML, trust-list C2PA,
automated screenshot evidence — it says so instead of pretending, and
says so plainly.
