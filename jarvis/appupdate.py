"""
In-app updates for the Android app, with the signing key kept on YOUR server.

Pipeline:
  1. GitHub Actions builds an UNSIGNED release APK and publishes it as a GitHub Release
     (tag app-<versionCode>) together with apksigner.jar and SHA-256 files.
  2. This server checks the latest release every few hours, downloads it, verifies the SHA-256s,
     and signs the APK with a key generated here on first use (stored in /data, password encrypted
     in the database). The key never leaves the server, so nobody else can produce an APK that the
     phone will accept as an update.
  3. The app asks /api/app/update, downloads /api/app/apk (paired devices only), checks the signature
     matches the installed app, and hands it to Android's installer: one "Update" tap.

Android always shows that final confirmation for apps installed outside a store; no app can skip it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Optional

import httpx

log = logging.getLogger("jarvis.appupdate")

UNSIGNED = "jarvis-unsigned.apk"
SIGNER = "apksigner.jar"
ALIAS = "jarvis"


class UpdateError(Exception):
    pass


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class AppUpdater:
    def __init__(self, db, data_dir: str, repo: str, client: Optional[httpx.Client] = None):
        self.db = db
        self.dir = os.path.join(data_dir, "app")
        self.repo = repo
        self.http = client or httpx.Client(timeout=120, follow_redirects=True,
                                           headers={"User-Agent": "jarvis-updater", "Accept": "application/vnd.github+json"})
        self._lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    # ------------------------------------------------------------------ state
    def current(self) -> Optional[dict]:
        info = self.db.kv_get("app_update")
        if info and os.path.exists(os.path.join(self.dir, info.get("file", ""))):
            return info
        return None

    def apk_path(self) -> Optional[str]:
        info = self.current()
        return os.path.join(self.dir, info["file"]) if info else None

    def status(self) -> dict:
        return {"available": self.current(), "last_check": self.db.kv_get("app_update_checked"),
                "last_error": self.db.kv_get("app_update_error"), "java": shutil.which("java") is not None}

    # ------------------------------------------------------------------ signing key (created once, stays here)
    def _keystore(self) -> tuple[str, str]:
        path = os.path.join(self.dir, "release.p12")
        pw = self.db.secret_get("apk_keystore_password")
        if os.path.exists(path) and pw:
            return path, pw
        if not shutil.which("keytool"):
            raise UpdateError("Java (keytool) isn't installed on the server; rebuild the server image.")
        pw = secrets.token_urlsafe(24)
        if os.path.exists(path):
            os.remove(path)
        subprocess.run(["keytool", "-genkeypair", "-keystore", path, "-storetype", "PKCS12", "-alias", ALIAS,
                        "-keyalg", "EC", "-groupname", "secp256r1", "-validity", "10000",
                        "-storepass", pw, "-keypass", pw, "-dname", "CN=JARVIS personal app"],
                       check=True, capture_output=True, timeout=60)
        os.chmod(path, 0o600)
        self.db.secret_set("apk_keystore_password", pw)
        self.db.audit("system", "app.keystore", {}, "created")
        log.warning("created the app signing key at %s (keep backups of /data)", path)
        return path, pw

    # ------------------------------------------------------------------ GitHub release
    def _latest_release(self) -> dict:
        r = self.http.get(f"https://api.github.com/repos/{self.repo}/releases/latest")
        if r.status_code == 404:
            raise UpdateError("No app release published yet.")
        if r.status_code >= 400:
            raise UpdateError(f"GitHub returned HTTP {r.status_code}")
        rel = r.json()
        m = re.fullmatch(r"app-(\d+)", rel.get("tag_name", ""))
        if not m:
            raise UpdateError(f"Unexpected release tag {rel.get('tag_name')!r}")
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        for need in (UNSIGNED, UNSIGNED + ".sha256", SIGNER, SIGNER + ".sha256"):
            if need not in assets:
                raise UpdateError(f"Release is missing {need}")
        return {"code": int(m.group(1)), "name": rel.get("name") or rel["tag_name"],
                "notes": (rel.get("body") or "")[:1500], "assets": assets}

    def _download(self, url: str, dest: str, expected_sha: Optional[str] = None) -> str:
        tmp = dest + ".part"
        with self.http.stream("GET", url) as r:
            if r.status_code >= 400:
                raise UpdateError(f"Download failed (HTTP {r.status_code})")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        if expected_sha and _sha256(tmp) != expected_sha:
            os.remove(tmp)
            raise UpdateError("Downloaded file doesn't match its published checksum; refusing it.")
        os.replace(tmp, dest)
        return dest

    def _text(self, url: str) -> str:
        r = self.http.get(url)
        if r.status_code >= 400:
            raise UpdateError(f"Download failed (HTTP {r.status_code})")
        return r.text.split()[0].strip().lower()

    # ------------------------------------------------------------------ check + prepare
    def check(self) -> dict:
        """Fetch, verify and sign the newest release if it's newer than what we have. Safe to call often."""
        with self._lock:
            self.db.kv_set("app_update_checked", time.time())
            try:
                rel = self._latest_release()
                have = self.current()
                if have and have["code"] >= rel["code"]:
                    self.db.kv_set("app_update_error", None)
                    return self.status()
                if not shutil.which("java"):
                    raise UpdateError("Java isn't installed on the server; rebuild the server image to enable app updates.")
                a = rel["assets"]
                signer_sha = self._text(a[SIGNER + ".sha256"])
                signer = os.path.join(self.dir, SIGNER)
                if not (os.path.exists(signer) and _sha256(signer) == signer_sha):
                    self._download(a[SIGNER], signer, signer_sha)
                apk_sha = self._text(a[UNSIGNED + ".sha256"])
                unsigned = self._download(a[UNSIGNED], os.path.join(self.dir, UNSIGNED), apk_sha)
                ks, pw = self._keystore()
                out_name = f"jarvis-{rel['code']}.apk"
                out = os.path.join(self.dir, out_name)
                env = {**os.environ, "JARVIS_KS_PASS": pw}
                subprocess.run(["java", "-jar", signer, "sign", "--ks", ks, "--ks-type", "PKCS12",
                                "--ks-pass", "env:JARVIS_KS_PASS", "--ks-key-alias", ALIAS,
                                "--min-sdk-version", "26", "--out", out, unsigned],
                               check=True, capture_output=True, timeout=300, env=env)
                v = subprocess.run(["java", "-jar", signer, "verify", "--min-sdk-version", "26", out],
                                   capture_output=True, timeout=120, text=True)
                if v.returncode != 0:
                    raise UpdateError(f"Signed APK failed verification: {v.stderr[:200]}")
                os.remove(unsigned)
                for f in os.listdir(self.dir):           # keep only the newest signed APK
                    if f.startswith("jarvis-") and f.endswith(".apk") and f != out_name:
                        os.remove(os.path.join(self.dir, f))
                info = {"code": rel["code"], "name": rel["name"], "notes": rel["notes"], "file": out_name,
                        "sha256": _sha256(out), "size": os.path.getsize(out), "ready_at": time.time()}
                self.db.kv_set("app_update", info)
                self.db.kv_set("app_update_error", None)
                self.db.audit("system", "app.update_ready", {"code": rel["code"]}, "done")
                return self.status()
            except subprocess.CalledProcessError as e:
                msg = (e.stderr or b"").decode(errors="ignore")[-300:] if isinstance(e.stderr, bytes) else str(e)[-300:]
                self.db.kv_set("app_update_error", f"Signing failed: {msg}")
            except (UpdateError, httpx.HTTPError, OSError) as e:
                self.db.kv_set("app_update_error", str(e)[:300])
            return self.status()
