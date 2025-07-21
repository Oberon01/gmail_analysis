"""Gmail poller (CLI-only)
Fixes logging placement & label-ID lookup.
Assumes .env with LABEL_ID_REVIEW and TextBlob installed.
"""

from __future__ import annotations

import base64
import email
import html
import os
import pathlib
import re
import time
import pickle
import logging
from pathlib import Path
from typing import Dict, List
from textblob import TextBlob
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import sqlite3

# --------------------------------------------------------------------------
# sqlite db
def init_cache_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# env + logging 
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
# helpers 

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
# extended logic 

def poll_once(service, rules=None, review_id=None, dry_run=False, conn=None) -> List[Dict]:
    digest = []
    seen = set(row[0] for row in conn.execute("SELECT id FROM seen").fetchall())

    for mid in unread_message_ids(service):
        try:
            msg = get_message(service, mid)
            body = plain_text_from_msg(msg)
            cat = classify(body)

            # passover if already parsed
            if mid in seen:
                continue

            # Rules override
            if rules:
                sender = next((h['value'] for h in msg['payload'].get('headers', []) if h['name'] == 'From'), "")
                if any(w in sender for w in rules.get("whitelist", [])):
                    cat = "important"
                elif any(b in sender for b in rules.get("blacklist", [])):
                    cat = "neither"
            else:
                sender = "unknown"

            subject = next((h['value'] for h in msg['payload'].get('headers', []) if h['name'] == 'Subject'), "No Subject")

            if dry_run:
                LOG.info("[DRY RUN] %s → %s", mid, cat)
            else:
                act(service, mid, cat, review_id)

            print(f"{mid} - {cat}")

            digest.append({
                "id": mid,
                "category": cat,
                "subject": subject,
                "sender": sender,
            })

            conn.execute("INSERT OR IGNORE INTO seen (id) VALUES (?)", (mid,))
            conn.commit()

        except Exception as exc:
            LOG.exception("processing %s failed: %s", mid, exc)

    return digest


def run_daemon(service, rules, review_id, interval=600, dry_run=False, conn=None):
    end = time.time() + interval
    while time.time() < end:
        poll_once(service, rules=rules, review_id=review_id, dry_run=dry_run, conn=conn)
        time.sleep(30)
    LOG.info("poller finished %s seconds OK", interval)

def export_digest_to_md(digest: List[Dict], out_path: Path):
    from datetime import datetime

    if not digest:
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    outfile = out_path / f"triage_{date_str}.md"

    grouped = {"important": [], "necessary": [], "neither": []}
    for item in digest:
        grouped[item["category"]].append(item)

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(f"# Gmail Triage Digest — {date_str}\n\n")

        for category in ["important", "necessary", "neither"]:
            if not grouped[category]:
                continue
            f.write(f"## {category.capitalize()}\n\n")
            for item in grouped[category]:
                subj = item['subject']
                sndr = item['sender']
                f.write(f"- **{subj}** — _{sndr}_\n")
            f.write("\n")

    print(f"✓ Digest written to {outfile}")


def load_rules(path):
    import yaml
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        LOG.warning("Failed to load rules from %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# CLI entrypoint 

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
    label_id_review = os.getenv("LABEL_ID_REVIEW")
    poll_interval = int(os.getenv("POLL_INTERVAL", 600))
    cache_path = Path(os.getenv("GMAIL_POLL_CACHE", "~/.cache/gmail_poll/cache.db")).expanduser()

    # Auth only
    if args.auth:
        get_service()
        print("Authentication complete.")
        return

    service = get_service()
    rules = load_rules(args.rules) if args.rules else {}
    conn = init_cache_db(cache_path)

    if args.once:
        digest = poll_once(service, rules=rules, review_id=label_id_review, dry_run=args.dry_run, conn=conn)
        export_digest_to_md(digest, Path("gmail_logs"))
    elif args.daemon:
        run_daemon(service, rules=rules, review_id=label_id_review, interval=poll_interval, dry_run=args.dry_run)
    else:
        print("No execution mode specified. Use --once or --daemon.")


if __name__ == "__main__":
    cli()
