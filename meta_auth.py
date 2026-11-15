"""Local Meta Login for Business callback for Instagram API development."""

from __future__ import annotations
from markupsafe import escape

import json
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, session


ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "meta-token.json"
SESSION_SECRET_FILE = ROOT / "meta-session-secret.txt"
WEBHOOK_EVENTS_FILE = ROOT / "meta-webhook-events.jsonl"
load_dotenv(ROOT / ".env")


def load_session_secret() -> str:
    configured_secret = os.getenv("META_SESSION_SECRET", "").strip()
    if configured_secret:
        return configured_secret
    try:
        return SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(48)
        SESSION_SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
        return secret


app = Flask(__name__)
app.secret_key = load_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # Localhost development only; use HTTPS when deployed.
)
# Werkzeug's normal access log includes the full callback query string, which contains
# a one-time OAuth authorization code. Suppress those request lines in this local app.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def setting(name: str) -> str:
    return os.getenv(name, "").strip()


def required_settings() -> tuple[str, str, str, str, str]:
    names = (
        "META_APP_ID",
        "META_APP_SECRET",
        "META_LOGIN_CONFIG_ID",
        "META_REDIRECT_URI",
        "META_GRAPH_VERSION",
    )
    values = tuple(setting(name) for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError("Add these settings to .env: " + ", ".join(missing))
    return values  # type: ignore[return-value]


def save_oauth_state(state: str) -> None:
    now = time.time()
    states = session.get("meta_oauth_states", {})
    if not isinstance(states, dict):
        states = {}
    states = {
        key: created
        for key, created in states.items()
        if isinstance(created, (int, float)) and now - created < 600
    }
    states[state] = now
    session["meta_oauth_states"] = states


def consume_oauth_state(state: str) -> bool:
    states = session.get("meta_oauth_states", {})
    if not isinstance(states, dict):
        return False
    created = states.pop(state, None)
    session["meta_oauth_states"] = states
    return isinstance(created, (int, float)) and time.time() - created < 600


@app.get("/")
def index():
    return (
        "<h1>Daily Viral Reels — Meta connection</h1>"
        '<p><a href="/meta/login">Connect the Instagram account</a></p>'
        "<p>This local page does not publish anything.</p>"
    )


def valid_meta_signature(raw_body: bytes) -> bool:
    app_secret = setting("META_APP_SECRET")
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not app_secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@app.get("/meta/webhook")
def verify_meta_webhook():
    expected_token = setting("META_WEBHOOK_VERIFY_TOKEN")
    mode = request.args.get("hub.mode", "")
    supplied_token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if (
        mode == "subscribe"
        and expected_token
        and hmac.compare_digest(supplied_token, expected_token)
        and challenge
    ):
        return challenge, 200, {"Content-Type": "text/plain"}
    return "Webhook verification failed", 403


@app.post("/meta/webhook")
def receive_meta_webhook():
    # Keep diagnostics useful without logging message text, media URLs, IDs, or tokens.
    app.logger.info("Meta webhook POST received")
    raw_body = request.get_data(cache=True)
    if not valid_meta_signature(raw_body):
        app.logger.warning("Meta webhook rejected: signature check failed")
        return "Invalid Meta webhook signature", 403
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        app.logger.warning("Meta webhook rejected: invalid JSON")
        return "Invalid JSON payload", 400

    captured = []
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        app.logger.warning("Meta webhook payload has an unexpected entry format")
        entries = []
    app.logger.info(
        "Verified Meta webhook payload: object=%s entries=%d",
        str(payload.get("object", ""))[:40],
        len(entries),
    )
    for entry in entries:
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            if message.get("is_echo"):
                continue
            sender = event.get("sender") or {}
            recipient = event.get("recipient") or {}
            urls = []
            for attachment in message.get("attachments", []) or []:
                attachment_payload = attachment.get("payload") or {}
                url = attachment_payload.get("url")
                if url:
                    urls.append(str(url))
            captured.append(
                {
                    "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "sender_id": str(sender.get("id", "")),
                    "recipient_id": str(recipient.get("id", "")),
                    "message_id": str(message.get("mid", "")),
                    "text": str(message.get("text", "")),
                    "shared_media_urls": urls,
                }
            )

    if captured:
        with WEBHOOK_EVENTS_FILE.open("a", encoding="utf-8") as events_file:
            for event in captured:
                events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
    app.logger.info("Meta webhook handled: saved %d incoming message event(s)", len(captured))
    if captured:
        try:
            from app import import_webhook_events

            _checked, added, skipped = import_webhook_events()
            app.logger.info(
                "Google Sheet sync completed: added %d Reel(s), skipped %d event(s)",
                added,
                skipped,
            )
        except Exception as exc:  # Keep credentials and message data out of logs.
            app.logger.error("Google Sheet sync failed (%s)", type(exc).__name__)

    # Acknowledge quickly; Meta retries webhook deliveries that do not get a 2xx.
    return jsonify({"status": "received", "events": len(captured)}), 200


@app.get("/meta/login")
def start_login():
    try:
        app_id, _app_secret, config_id, redirect_uri, graph_version = required_settings()
    except RuntimeError as exc:
        return str(exc), 500

    state = secrets.token_urlsafe(32)
    save_oauth_state(state)
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "config_id": config_id,
        "response_type": "code",
        "state": state,
    }
    login_url = f"https://www.facebook.com/{graph_version}/dialog/oauth?{urlencode(params)}"
    return redirect(login_url)


@app.get("/meta/callback")
def finish_login():
    returned_state = request.args.get("state", "")
    if not returned_state or not consume_oauth_state(returned_state):
        return "Login state did not match. Return to the local app and try again.", 400

    if request.args.get("error"):
        # Show only Meta's short user-facing error; never echo tokens or the full query.
        description = request.args.get("error_description", "Login was cancelled or denied.")
        return f"Meta login did not complete: {escape(description)}", 400

    code = request.args.get("code", "")
    if not code:
        return "Meta did not return an authorization code.", 400

    try:
        app_id, app_secret, _config_id, redirect_uri, graph_version = required_settings()
        response = requests.get(
            f"https://graph.facebook.com/{graph_version}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=30,
        )
        response.raise_for_status()
        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return "Meta's response did not include an access token. Check the app configuration.", 502

        # Store locally and privately; do not print or send the token in the web page.
        TOKEN_FILE.write_text(
            json.dumps(token_data, indent=2) + "\n",
            encoding="utf-8",
        )
    except requests.RequestException as exc:
        # Keep credentials and request URLs out of the response.
        detail = "Meta token exchange failed. Check the app settings and try again."
        if getattr(exc, "response", None) is not None:
            try:
                detail += " Meta said: " + str(exc.response.json().get("error", {}).get("message", "request rejected"))
            except ValueError:
                pass
        return detail, 502
    except (RuntimeError, ValueError) as exc:
        return str(exc), 500

    return (
        "<h1>Instagram connection received</h1>"
        "<p>The access token was saved locally in the ignored file "
        "<code>meta-token.json</code>. It was not displayed here.</p>"
        "<p>This only connects the account; it does not publish a reel.</p>"
    )


if __name__ == "__main__":
    # Local-only listener. Do not expose the development server to the public internet.
    app.run(host="127.0.0.1", port=5000, debug=False)
