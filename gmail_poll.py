"""Gmail poller (CLI-only)
Fixes logging placement & label-ID lookup.
Assumes .env with LABEL_ID_REVIEW and TextBlob installed.
"""
from __future__ import annotations

import base64
import email
import html
import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Dict, List
import pickle
import logging
from pathlib import Path
from textblob import TextBlob
from dotenv import load_dotenv, dotenv_values
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# env + logging -------------------------------------------------------------
load_dotenv()
LOG = logging.getLogger("gmail_poll")
LOG.setLevel(logging.INFO)
fh = logging.FileHandler("gmail_analysis.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
if not LOG.handlers:
    LOG.addHandler(fh)

LABEL_ID_REVIEW = os.getenv("LABEL_ID_REVIEW")
if not LABEL_ID_REVIEW:
    LOG.warning("LABEL_ID_REVIEW not set; review labels will be skipped")

# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN = pathlib.Path("token.pickle")


def get_service():
    creds = None
    if TOKEN.exists():
        creds = pickle.loads(TOKEN.read_bytes())
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_bytes(pickle.dumps(creds))
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# helpers -------------------------------------------------------------------

def unread_message_ids(service):
    resp = service.users().messages().list(userId="me", q="is:unread").execute()
    return [m["id"] for m in resp.get("messages", [])]


def get_message(service, msg_id):
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def _b64_to_str(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")


def _strip_html(htm: str) -> str:
    return html.unescape(re.sub("<[^>]+>", "", htm))


def plain_text_from_msg(msg: Dict) -> str:
    payload = msg["payload"]
    if payload.get("body", {}).get("data"):
        data = _b64_to_str(payload["body"]["data"])
        return data if payload["mimeType"] == "text/plain" else _strip_html(data)

    stack = payload.get("parts", [])
    while stack:
        part = stack.pop(0)
        body = part.get("body", {})
        if body.get("data"):
            data = _b64_to_str(body["data"])
            if part.get("mimeType") == "text/plain":
                return data
            return _strip_html(data)
        stack.extend(part.get("parts", []))
    return ""


def classify(text: str) -> str:
    polarity = TextBlob(text).sentiment.polarity
    text_low = text.lower()
    if "invoice" in text_low or "payment due" in text_low or "monthly statement" in text_low:
        return "necessary"
    if polarity > 0.4 or "thank you" in text_low:
        return "important"
    return "neither"


def act(service, msg_id: str, category: str, review_id: str | None):
    if category == "necessary" and review_id:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": ["STARRED", review_id]},
        ).execute()
    elif category == "important":
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": ["STARRED"]},
        ).execute()
    else:
        service.users().messages().trash(userId="me", id=msg_id).execute()


# ---------------------------------------------------------------------------
# main loop ------------------------------------------------------------------

def poll_once(service):
    for mid in unread_message_ids(service):
        try:
            msg = get_message(service, mid)
            body = plain_text_from_msg(msg)
            cat = classify(body)
            act(service, mid, cat, LABEL_ID_REVIEW)
            LOG.info("%s %s", mid, cat)
            print(f"{mid} - {cat}")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("processing %s failed: %s", mid, exc)


def main(loop_seconds: int = 600):
    
    svc = get_service()
    end = time.time() + loop_seconds
    while time.time() < end:
        poll_once(svc)
        time.sleep(30)
    LOG.info("poller finished %s seconds OK", loop_seconds)

def cli():
    import argparse

    parser = argparse.ArgumentParser(description="Autonomous Gmail Triage Daemon")
    parser.add_argument("--auth", action="store_true", help="Authenticate and exit")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon polling every interval")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying Gmail")
    parser.add_argument("--rules", type=str, help="Path to rules.yaml")
    args = parser.parse_args()

    # Load environment
    load_dotenv()
    LABEL_ID_REVIEW = os.getenv("LABEL_ID_REVIEW")
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 600))
    CACHE_DB = Path(os.getenv("GMAIL_POLL_CACHE", "~/.cache/gmail_poll/cache.db")).expanduser()

    # Auth only mode
    if args.auth:
        get_service()
        print("Authentication complete.")
        return

    # Load service
    svc = get_service()

    # Load rules (optional)
    rules = load_rules(args.rules) if args.rules else {}

    # Run once or as daemon
    if args.once:
        poll_once(svc, rules, LABEL_ID_REVIEW, dry_run=args.dry_run)
    elif args.daemon:
        run_daemon(svc, rules, LABEL_ID_REVIEW, interval=POLL_INTERVAL, dry_run=args.dry_run)
    else:
        print("No mode specified. Use --once or --daemon.")



if __name__ == "__main__":
    main()
