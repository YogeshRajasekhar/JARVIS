"""
One-time Google sign-in for Calendar + Gmail (least-privilege scopes, see jarvis/integrations/google_auth.py).

1. Google Cloud Console -> APIs & Services: enable "Google Calendar API" and "Gmail API".
2. OAuth consent screen: External, add yourself as a test user.
3. Credentials -> Create OAuth client ID -> "Desktop app" -> download JSON.
4. Set GOOGLE_CREDENTIALS_PATH in .env to that file, then run:  python scripts/google_auth.py
   (run it on your laptop; copy the resulting data/jarvis.db or set the same JARVIS_DATA_KEY on the VM
    and run it there with --no-browser)
"""

import argparse
import hashlib
import base64
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

sys.path.insert(0, ".")
from jarvis.config import Settings  # noqa: E402
from jarvis.core.storage import Database  # noqa: E402
from jarvis.integrations.google_auth import AUTH_URL, SCOPES, TOKEN_URL, GoogleAuth  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()
    s = Settings()
    if not s.google_credentials_path:
        sys.exit("Set GOOGLE_CREDENTIALS_PATH in .env first.")
    auth = GoogleAuth.from_credentials_file(Database(s.db_path, s.data_key or None), s.google_credentials_path)
    redirect = f"http://127.0.0.1:{a.port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": auth.client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                msg = b"JARVIS is connected to Google. You can close this tab."
            else:
                msg = b"Sign-in failed or was cancelled."
            self.send_response(200); self.end_headers(); self.wfile.write(msg)
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", a.port), H)
    print("Open this URL to sign in:\n", url)
    if not a.no_browser:
        webbrowser.open(url)
    srv.serve_forever()
    if "code" not in got:
        sys.exit("No authorisation code received.")
    r = httpx.post(TOKEN_URL, data={"client_id": auth.client_id, "client_secret": auth.client_secret, "code": got["code"],
                                    "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier})
    r.raise_for_status()
    tokens = r.json()
    if "refresh_token" not in tokens:
        sys.exit("Google did not return a refresh token; remove JARVIS at myaccount.google.com/permissions and retry.")
    auth.store_tokens(tokens)
    print("Saved (encrypted). Restart the JARVIS server; calendar and mail now use your Google account.")


if __name__ == "__main__":
    main()
