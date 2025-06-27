from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import google.auth.exceptions
import pickle, os, pathlib
from textblob import TextBlob
import time, traceback, logging
from dotenv import load_dotenv, dotenv_values
import base64, email, html, re


env = dotenv_values()
LABEL_ID_REVIEW = env["LABEL_ID_REVIEW"]
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

def unread_message_ids(service):
    resp = service.users().messages().list(userId="me", q="is:unread").execute()
    return [m["id"] for m in resp.get("messages", [])]

def get_message(service, msg_id):
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def _b64_to_str(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")

def _strip_html(htm: str) -> str:
    return html.unescape(re.sub("<[^>]+>", "", htm))

def plain_text_from_msg(msg: dict) -> str:
    """
    Return best-effort plain text for any Gmail message.
    May return '' if no textual part exists (calendar invites, etc.).
    """
    payload = msg["payload"]

    # 1) Direct body (single-part)
    if payload.get("body", {}).get("data"):
        if payload["mimeType"] == "text/plain":
            return _b64_to_str(payload["body"]["data"])
        if payload["mimeType"] == "text/html":
            return _strip_html(_b64_to_str(payload["body"]["data"]))

    # 2) Walk all sub-parts (depth-first)
    stack = payload.get("parts", [])
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if body.get("data"):
            text = _b64_to_str(body["data"])
            if mime == "text/plain":
                return text
            if mime == "text/html":
                return _strip_html(text)
        # push nested parts (attachments or alternative)
        stack.extend(part.get("parts", []))

    # 3) Fallback: nothing textual found
    return ""



def classify(text):
    polarity = TextBlob(text).sentiment.polarity
    if "invoice" in text.lower() or "payment due" in text.lower():
        return "necessary"
    if polarity > 0.2 or "thank you" in text.lower():
        return "important"
    return "neither"

def get_or_create_label_id(service, label_name: str) -> str:
    # 1) check existing labels
    resp = service.users().labels().list(userId="me").execute()
    for lbl in resp["labels"]:
        if lbl["name"].lower() == label_name.lower():
            return lbl["id"]

    # 2) create it if absent
    body = {"name": label_name, "labelListVisibility": "labelShow"}
    lbl = service.users().labels().create(userId="me", body=body).execute()
    return lbl["id"]

# ---------------------------------  DIAGNOSTIC  ---------------------------------
def assert_label_ids(service, review_id):
    labels = {
        lbl["id"]: lbl["name"]
        for lbl in service.users().labels().list(userId="me").execute()["labels"]
    }
    print("DEBUG-labels:", labels)                  # shows every id→name pair

    missing = [lid for lid in ("STARRED", review_id) if lid not in labels]
    if missing:
        raise RuntimeError(f"These IDs are unknown to Gmail: {missing}")

# call it once, right after you obtain the service object and review_id

# -------------------------------------------------------------------------------

def act(service, msg_id, category):
    if category == "necessary":
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": ["STARRED", LABEL_ID_REVIEW]}
        ).execute()
    elif category == "important":
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": ["STARRED"]}
        ).execute()
    else:
        service.users().messages().trash(userId="me", id=msg_id).execute()


def main():
	svc = get_service()
	while True:
		for mid in unread_message_ids(svc):
			msg = get_message(svc, mid)
			body = plain_text_from_msg(msg)
			cat  = classify(body)
			act(svc, mid, cat)
			logging.basicConfig(
				filename="gmail_analysis.log",
				level=logging.INFO,
				format="%(asctime)s %(levelname)s %(message)s",
			)
		time.sleep(30)   # 5-minute poll

if __name__ == "__main__":
     main()