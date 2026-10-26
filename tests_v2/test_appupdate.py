"""In-app updates: fetch release, verify checksums, sign with the server's own key, serve to paired devices."""

import hashlib
import os
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.appupdate import AppUpdater
from jarvis.core.storage import Database

needs_java = pytest.mark.skipif(not (shutil.which("java") and shutil.which("javac") and shutil.which("keytool")),
                                reason="needs a JDK")

FAKE_SIGNER = r"""
import java.nio.file.*;
public class FakeSigner {
  // Mimics `apksigner sign --ks K ... --out OUT IN` and `apksigner verify ... APK`.
  public static void main(String[] a) throws Exception {
    if (a[0].equals("verify")) { System.exit(Files.exists(Paths.get(a[a.length - 1])) ? 0 : 1); }
    String out = null, ks = null;
    for (int i = 0; i < a.length; i++) { if (a[i].equals("--out")) out = a[i + 1]; if (a[i].equals("--ks")) ks = a[i + 1]; }
    if (System.getenv("JARVIS_KS_PASS") == null || !Files.exists(Paths.get(ks))) System.exit(3);
    byte[] apk = Files.readAllBytes(Paths.get(a[a.length - 1]));
    Files.write(Paths.get(out), (new String(apk) + "|signed").getBytes());
  }
}
"""


@pytest.fixture(scope="module")
def signer_jar(tmp_path_factory):
    d = tmp_path_factory.mktemp("signer")
    (d / "FakeSigner.java").write_text(FAKE_SIGNER)
    subprocess.run(["javac", "FakeSigner.java"], cwd=d, check=True, capture_output=True)
    (d / "m.txt").write_text("Main-Class: FakeSigner\n")
    subprocess.run(["jar", "cfm", "signer.jar", "m.txt", "FakeSigner.class"], cwd=d, check=True, capture_output=True)
    return (d / "signer.jar").read_bytes()


def github(signer: bytes, code: int = 140, apk: bytes = b"APK-BYTES", tamper: bool = False, missing: bool = False):
    sha = lambda b: hashlib.sha256(b).hexdigest()
    files = {"jarvis-unsigned.apk": apk, "jarvis-unsigned.apk.sha256": (sha(apk) if not tamper else "0" * 64).encode(),
             "apksigner.jar": signer, "apksigner.jar.sha256": sha(signer).encode()}
    if missing:
        files.pop("apksigner.jar")
    calls = []

    def h(req):
        calls.append(str(req.url))
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": f"app-{code}", "name": f"JARVIS app 2.{code}", "body": "Fixes",
                                             "assets": [{"name": n, "browser_download_url": f"https://dl/{n}"} for n in files]})
        name = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=files[name])

    return httpx.Client(transport=httpx.MockTransport(h)), calls


@needs_java
def test_prepares_signed_update_with_server_key(tmp_path, signer_jar):
    db = Database()
    client, _ = github(signer_jar)
    up = AppUpdater(db, str(tmp_path), "me/JARVIS", client=client)
    st = up.check()
    a = st["available"]
    assert a["code"] == 140 and st["last_error"] is None
    assert open(up.apk_path(), "rb").read() == b"APK-BYTES|signed"
    ks = tmp_path / "app" / "release.p12"
    assert ks.exists() and oct(ks.stat().st_mode)[-3:] == "600"
    assert db.secret_get("apk_keystore_password")                    # password only in the encrypted store
    assert not (tmp_path / "app" / "jarvis-unsigned.apk").exists()
    first_key = ks.read_bytes()
    client2, _ = github(signer_jar, code=141, apk=b"NEWER")
    up.http = client2
    up.check()
    assert up.current()["code"] == 141 and ks.read_bytes() == first_key  # same key for every update
    assert sorted(os.listdir(tmp_path / "app")) == ["apksigner.jar", "jarvis-141.apk", "release.p12"]


@needs_java
def test_refuses_tampered_download(tmp_path, signer_jar):
    db = Database()
    client, _ = github(signer_jar, tamper=True)
    st = AppUpdater(db, str(tmp_path), "me/JARVIS", client=client).check()
    assert st["available"] is None and "checksum" in st["last_error"]


@needs_java
def test_no_work_when_up_to_date(tmp_path, signer_jar):
    db = Database()
    client, calls = github(signer_jar)
    up = AppUpdater(db, str(tmp_path), "me/JARVIS", client=client)
    up.check()
    n = len(calls)
    up.check()
    assert len(calls) == n + 1                                         # only the release lookup


def test_incomplete_release_reported(tmp_path):
    client, _ = github(b"jar", missing=True)
    st = AppUpdater(Database(), str(tmp_path), "me/JARVIS", client=client).check()
    assert "missing apksigner.jar" in st["last_error"]


def test_apk_endpoint_requires_pairing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from conftest import NOW, offline_settings
    from jarvis.integrations.device import SimulatedDevice
    from jarvis.server.app import create_app
    from jarvis.services import build_services

    s = offline_settings()
    s.device_token = "upd-token-123456789"
    s.update_repo = "me/JARVIS"
    svc = build_services(s, db=Database(), clock=lambda: NOW, seed=False, device=SimulatedDevice())
    app = create_app(s, svc=svc, enable_wakeword=False)
    c = TestClient(app)
    assert c.get("/api/app/apk").status_code == 401
    H = {"Authorization": "Bearer upd-token-123456789"}
    assert c.get("/api/app/apk", headers=H).status_code == 404        # nothing prepared yet
    up = app.state.updater
    os.makedirs(up.dir, exist_ok=True)
    with open(os.path.join(up.dir, "jarvis-150.apk"), "wb") as f:
        f.write(b"SIGNED")
    svc.db.kv_set("app_update", {"code": 150, "name": "x", "notes": "", "file": "jarvis-150.apk", "sha256": "abc", "size": 6})
    r = c.get("/api/app/apk", headers=H)
    assert r.status_code == 200 and r.content == b"SIGNED" and r.headers["x-sha256"] == "abc"
    assert c.get("/api/app/update", headers=H).json()["available"]["code"] == 150
