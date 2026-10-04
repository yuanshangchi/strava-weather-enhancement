"""Connect a personal Strava account using a local OAuth callback."""

import argparse
import json
import os
from pathlib import Path
import secrets
import ssl
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
REQUIRED_SCOPES = {"activity:read_all"}


def load_credentials():
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values["STRAVA_CLIENT_ID"], values["STRAVA_CLIENT_SECRET"]


def validate_callback(query, expected_state, required_scopes=None):
    state = query.get("state", [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise ValueError("Invalid login state. Restart the connection script.")
    if "error" in query:
        raise ValueError("Authorization was declined. No credentials were saved.")
    scopes = set(query.get("scope", [""])[0].replace(",", " ").split())
    if not (required_scopes or REQUIRED_SCOPES).issubset(scopes):
        raise ValueError("Requested activity permissions are required. Restart and enable them.")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("Authorization code is missing. Restart the script.")
    return code, scopes


def save_tokens(tokens):
    fd, filename = tempfile.mkstemp(prefix=".tokens-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(tokens, output, indent=2)
            output.write("\n")
        os.replace(filename, ROOT / "tokens.json")
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Also request permission to update activity descriptions")
    args = parser.parse_args()
    required_scopes = REQUIRED_SCOPES | ({"activity:write"} if args.write else set())
    if not ssl.create_default_context().get_ca_certs():
        raise SystemExit(
            'Python has no trusted HTTPS certificates. On macOS, run '
            '"/Applications/Python 3.13/Install Certificates.command", '
            'then restart this script.'
        )
    try:
        client_id, client_secret = load_credentials()
    except (OSError, KeyError, ValueError):
        raise SystemExit("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")
    state = secrets.token_urlsafe(32)
    result = {"done": False, "success": False}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs contain an authorization code; don't log them.

        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path != "/callback":
                self.send_error(404)
                return
            try:
                code, scopes = validate_callback(parse_qs(parts.query), state, required_scopes)
            except ValueError as error:
                self.respond(400, str(error))
                return
            try:
                body = urlencode({
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                }).encode()
                request = Request("https://www.strava.com/oauth/token", data=body)
                with urlopen(request, timeout=30) as response:
                    tokens = json.load(response)
                if not all(tokens.get(k) for k in ("access_token", "refresh_token", "expires_at")):
                    raise ValueError("Incomplete token response")
                tokens["scope"] = sorted(scopes)
                save_tokens(tokens)
            except HTTPError as error:
                message = f"Strava returned HTTP {error.code}. Check your credentials and restart the script."
            except URLError as error:
                if isinstance(error.reason, ssl.SSLCertVerificationError):
                    message = ('Python could not verify Strava\'s HTTPS certificate. Run '
                               '"/Applications/Python 3.13/Install Certificates.command", then restart.')
                else:
                    message = "Could not reach Strava. Check your network or VPN, then restart."
            except (OSError, ValueError):
                message = "Could not save the Strava connection. Restart the script."
            else:
                result["success"] = True
                message = "Connected to Strava! You can close this tab. No activities have been changed."
            result["done"] = True
            print(message)
            self.respond(200 if result["success"] else 502, message)

        def respond(self, status, message):
            content = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)

    try:
        server = HTTPServer(("127.0.0.1", 8000), Callback)
    except OSError:
        raise SystemExit("Port 8000 is busy. Stop the other local server and try again.")
    with server:
        server.timeout = 1
        url = "https://www.strava.com/oauth/authorize?" + urlencode({
            "client_id": client_id,
            "redirect_uri": "http://localhost:8000/callback",
            "response_type": "code",
            "approval_prompt": "force",
            "scope": ",".join(sorted(required_scopes)),
            "state": state,
        })
        print("Opening Strava. Authorize activity read access (including Only You activities).")
        if args.write:
            print("Also requesting activity write access for description updates.")
        print("If your browser doesn't open, visit this URL:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while not result["done"] and time.monotonic() < deadline:
            server.handle_request()
    if not result["done"]:
        raise SystemExit("Login timed out after 10 minutes. Run the script again.")
    if not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nConnection cancelled.")
