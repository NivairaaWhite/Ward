"""
provenance.py
-------------
Attaches a provenance record to a protected image: who made it, when,
and a hash tying the record to this exact file.

TWO PATHS, BE HONEST ABOUT WHICH ONE YOU'RE ON
    "c2pa"  -- a real C2PA (Coalition for Content Provenance and
        Authenticity) manifest, via the official c2pa-python bindings.
        This is the actual current standard Adobe/Microsoft/Google/
        OpenAI implement, and it's the one that shows up as verified
        "Content Credentials" on contentcredentials.org and in tools
        that check for it.

        BUT: C2PA's trust model requires the signing certificate to
        chain to a Certificate Authority on the official C2PA Trust
        List (as of writing: SSL.com, Trufo, DigiCert). A self-signed
        certificate is deliberately rejected by the C2PA library itself
        at signing time -- this was verified directly against the
        installed library, not assumed. That means real, externally-
        verifiable C2PA provenance requires buying a certificate from
        one of those CAs; Ward can't include one, and doesn't try to
        fake being one. If you have your own CA-issued cert + key, set
        C2PA_CERT_PATH and C2PA_KEY_PATH in .env (PEM files) and Ward
        will sign real, trust-list-verifiable manifests with them.

    "local" -- the fallback, used automatically when no real cert is
        configured. A small JSON record (creator name, tracking ID,
        the file's own SHA-256, a timestamp) signed with an Ed25519
        keypair Ward generates and keeps for you on first use
        (instance/.provenance_key). This is NOT a C2PA manifest and
        will NOT show up as verified on contentcredentials.org or in
        any C2PA-aware tool -- it's a private, tamper-evident record
        you control: if you publish the public key (Settings shows it),
        anyone can verify a given record was signed by *this specific
        Ward install* and hasn't been altered, without needing to trust
        a certificate authority. Call this what it is when showing it
        in the UI: "Ward-signed local record," never "C2PA" or
        "Content Credentials."
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:
    import c2pa

    _C2PA_AVAILABLE = True
except ImportError:
    _C2PA_AVAILABLE = False


class ProvenanceError(Exception):
    pass


# ------------------------------------------------------- local fallback
def _get_or_create_local_key(instance_dir: str) -> Ed25519PrivateKey:
    """One Ed25519 keypair per Ward install, generated on first use and
    reused after -- generated once per install rather than baked into the
    source, since a shared key would defeat the point."""
    key_path = os.path.join(instance_dir, ".provenance_key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    os.makedirs(instance_dir, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def local_public_key_b64(instance_dir: str) -> str:
    """The public half, safe to publish (e.g. on your portfolio) so
    someone else can verify a record independently of Ward itself."""
    key = _get_or_create_local_key(instance_dir)
    pub_bytes = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(pub_bytes).decode()


def create_local_record(
    image_path: str, instance_dir: str, tracking_id: str, creator_name: str = ""
) -> dict:
    """Build and sign a local provenance record for the file at
    image_path. Returns a dict ready to store as JSON."""
    key = _get_or_create_local_key(instance_dir)
    with open(image_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()

    record = {
        "kind": "ward-local-record",  # never call this "c2pa" -- see module docstring
        "tracking_id": tracking_id,
        "creator_name": creator_name or None,
        "sha256": sha256,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": "Ward",
    }
    payload = json.dumps(record, sort_keys=True).encode()
    signature = key.sign(payload)
    record["signature_b64"] = base64.b64encode(signature).decode()
    record["public_key_b64"] = local_public_key_b64(instance_dir)
    return record


# ------------------------------------------------------------ real C2PA
def c2pa_available() -> bool:
    return _C2PA_AVAILABLE


def create_c2pa_manifest(
    image_path: str,
    output_path: str,
    cert_pem_path: str,
    key_pem_path: str,
    tracking_id: str,
    creator_name: str = "",
    signing_alg: str = "es256",
) -> bytes:
    """Sign `image_path` with a real C2PA manifest using your own
    CA-issued certificate, writing the signed copy to output_path.
    Raises ProvenanceError (wrapping the library's own error) if the
    cert isn't on the C2PA trust list -- that's the library enforcing
    the real standard's trust model, not a Ward limitation to work
    around. See module docstring for where to get a cert."""
    if not _C2PA_AVAILABLE:
        raise ProvenanceError("c2pa-python is not installed.")
    if not (os.path.exists(cert_pem_path) and os.path.exists(key_pem_path)):
        raise ProvenanceError("C2PA_CERT_PATH / C2PA_KEY_PATH not found on disk.")

    with open(cert_pem_path, "r") as f:
        cert_pem = f.read()
    with open(key_pem_path, "r") as f:
        key_pem = f.read()

    manifest = {
        "claim_generator": "Ward/1.0",
        "title": tracking_id,
        "assertions": [
            {
                "label": "stds.schema-org.CreativeWork",
                "data": {
                    "@context": "https://schema.org",
                    "@type": "CreativeWork",
                    "author": [{"@type": "Person", "name": creator_name or "Unknown"}],
                },
            }
        ],
    }

    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")

    try:
        signer_info = c2pa.C2paSignerInfo(
            alg=signing_alg.encode(), sign_cert=cert_pem.encode(), private_key=key_pem.encode(), ta_url=None
        )
        signer = c2pa.Signer.from_info(signer_info)
        builder = c2pa.Builder(json.dumps(manifest))
        builder.add_action(json.dumps({"action": "c2pa.created"}))
        with open(image_path, "rb") as src, open(output_path, "wb") as dst:
            manifest_bytes = builder.sign(signer, mime, src, dst)
        return manifest_bytes
    except Exception as e:
        raise ProvenanceError(f"C2PA signing failed: {e}") from e
