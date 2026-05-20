"""
Kanlaya Check-in Mailer — 90-Min-Follow-up

Reads Kanlaya CRM (Notion), finds check-ins that happened 90+ min ago,
sends bonus mail via Brevo Transactional API, marks row as sent.

Idempotent over "Mail gesendet" checkbox. Failsafe window: last 24h only.

Env:
    NOTION_TOKEN          — Notion integration token
    NOTION_DB_ID          — Kanlaya CRM database ID
    BREVO_API_KEY         — Brevo Transactional API key (KANLAYA_BREVO_API_KEY)
    BREVO_SENDER_EMAIL    — default: hallo@kanlaya-massagepraxis.berlin
    BREVO_SENDER_NAME     — default: Kanlaya Thai Massage
    ALERT_EMAIL           — recipient for 3-strike-failure alerts
    TEST_RECIPIENT        — if set, all mails redirect here (for live testing)
    DRY_RUN               — if "1", no mail sent, no Notion update

CLI:
    python mailer.py             # normal run
    python mailer.py --dry-run   # same as DRY_RUN=1
"""
from __future__ import annotations
import os
import sys
import json
import secrets
import urllib.parse
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dateutil import parser as dateparser
from jinja2 import Environment, FileSystemLoader, select_autoescape

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
BREVO_API = "https://api.brevo.com/v3/smtp/email"

FOLLOWUP_DELAY_MIN = int(os.environ.get("FOLLOWUP_DELAY_MIN", "90"))
# Hard rule: Check-ins, die länger her sind als (DELAY + WINDOW) bekommen KEINE Mail
# mehr — sonst kommt die "90 Min später"-Mail Stunden später an, was unprofessionell wirkt.
FAILSAFE_WINDOW_HOURS = 3
MAX_RETRIES = 3

FRESHA_LINK = "https://www.fresha.com/p/kanlaya-moller-6281320?share=true&pId=2852347"
FRESHA_REVIEW_LINK = "https://www.fresha.com/p/kanlaya-moller-6281320?share=true&pId=2852347#reviews"
GOOGLE_REVIEW_LINK = "https://g.page/r/CZS7MhobLjedEBM/review"
KANLAYA_HOMEPAGE = "https://kanlaya-massagepraxis.berlin/"
PORTRAIT_URL = "https://kanlaya-massagepraxis.berlin/assets/kanlaya-portrait.jpg"
UNSUBSCRIBE_LINK = "https://kanlaya-massagepraxis.berlin/unsubscribe.php?email={email}"
INSTAGRAM_URL = "https://www.instagram.com/kanlaya_massage_berlin/"
FACEBOOK_URL = "https://www.facebook.com/profile.php?id=61573274353769"
INFO_MAIL = "info@kanlaya-massagepraxis.berlin"

CODE_SELF = "ICHKOMMEWIEDER"           # 5 € für Mail-Empfänger bei Fresha-Buchung
CODE_REFERRAL = "EMPFEHLUNG05"         # 5 € für den eingeladenen Freund

REFERRAL_TEXT = (
    "Sawadee Khâ! Ich war gerade bei Kanlaya Thai Massage in Charlottenburg. "
    "Echte traditionelle Thai-Massage, sehr empfehlenswert. "
    f"Mit dem Code {CODE_REFERRAL} bekommst du 5 € auf deine erste Behandlung. "
    f"Hier buchen: {FRESHA_LINK}"
)


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        print(f"ERROR: missing env var {key}", file=sys.stderr)
        sys.exit(1)
    return val or ""


def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def query_pending(token: str, db_id: str, now_utc: datetime) -> list[dict]:
    """Find check-ins eligible for 90-min follow-up."""
    cutoff_old = (now_utc - timedelta(hours=FAILSAFE_WINDOW_HOURS)).isoformat()
    cutoff_new = (now_utc - timedelta(minutes=FOLLOWUP_DELAY_MIN)).isoformat()
    # Hard cutoff: never send to check-ins from BEFORE this timestamp.
    # Used to "skip everything that already exists" when the mailer goes live.
    earliest = os.environ.get("EARLIEST_CHECKIN_ISO", "").strip()

    date_filters = [
        {"property": "Letzter Besuch", "date": {"on_or_after": cutoff_old}},
        {"property": "Letzter Besuch", "date": {"on_or_before": cutoff_new}},
    ]
    if earliest:
        date_filters.append({"property": "Letzter Besuch", "date": {"on_or_after": earliest}})

    filter_body = {
        "filter": {
            "and": [
                {"property": "Marketing-Consent", "checkbox": {"equals": True}},
                {"property": "Mail gesendet", "checkbox": {"equals": False}},
                *date_filters,
            ]
        },
        "page_size": 50,
    }

    resp = requests.post(
        f"{NOTION_API}/databases/{db_id}/query",
        headers=notion_headers(token),
        json=filter_body,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def extract_row(page: dict) -> dict | None:
    """Pull fields we need. Skip rows without parseable check-in datetime or email."""
    p = page.get("properties", {})

    email = (p.get("Email") or {}).get("email")
    if not email:
        return None

    vorname_arr = (p.get("Vorname") or {}).get("title") or []
    vorname = vorname_arr[0]["plain_text"] if vorname_arr else "lieber Gast"

    letzter_besuch_obj = (p.get("Letzter Besuch") or {}).get("date") or {}
    raw_start = letzter_besuch_obj.get("start")
    if not raw_start:
        return None

    # Only proceed if datetime (not date-only). intake.php must write full ISO.
    if "T" not in raw_start:
        return None

    try:
        checkin_dt = dateparser.isoparse(raw_start)
    except (ValueError, TypeError):
        return None

    retry_count = (p.get("Retry-Count") or {}).get("number") or 0

    return {
        "page_id": page["id"],
        "email": email,
        "vorname": vorname.strip(),
        "checkin_dt": checkin_dt,
        "retry_count": int(retry_count),
    }


def render_mail(env_jinja: Environment, vorname: str, email: str) -> tuple[str, str]:
    """Return (subject, html)."""
    template = env_jinja.get_template("bonus_mail.html")
    wa_text = urllib.parse.quote(REFERRAL_TEXT)
    mail_subject = urllib.parse.quote("Empfehlung: Kanlaya Thai Massage Berlin")
    mail_body = urllib.parse.quote(REFERRAL_TEXT)
    feedback_subject = urllib.parse.quote(f"Feedback von {vorname or 'einem Gast'}")
    feedback_body = urllib.parse.quote(
        f"Sawadee Khâ Kanlaya,\n\nmein Feedback zu meinem Besuch:\n\n"
    )
    mailto_feedback = f"mailto:{INFO_MAIL}?subject={feedback_subject}&body={feedback_body}"

    html = template.render(
        vorname=vorname or "lieber Gast",
        code_self=CODE_SELF,
        code_referral=CODE_REFERRAL,
        fresha_link=FRESHA_LINK,
        fresha_review_link=FRESHA_REVIEW_LINK,
        google_review_link=GOOGLE_REVIEW_LINK,
        portrait_url=PORTRAIT_URL,
        kanlaya_homepage=KANLAYA_HOMEPAGE,
        instagram_url=INSTAGRAM_URL,
        facebook_url=FACEBOOK_URL,
        whatsapp_share_link=f"https://wa.me/?text={wa_text}",
        mail_share_link=f"mailto:?subject={mail_subject}&body={mail_body}",
        mailto_feedback=mailto_feedback,
        unsubscribe_link=UNSUBSCRIBE_LINK.format(email=email),
        datum_de=datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y"),
    )
    name = (vorname or "").strip()
    subject = (
        f"{name}, eine Bitte. Ein Geschenk. Ein Sawadee Khâ."
        if name else "Eine Bitte. Ein Geschenk. Ein Sawadee Khâ."
    )
    return subject, html


def send_brevo(api_key: str, sender_email: str, sender_name: str,
               to_email: str, to_name: str, subject: str, html: str) -> None:
    body = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html,
        "replyTo": {"email": sender_email, "name": sender_name},
        "tags": ["checkin-followup"],
    }
    resp = requests.post(
        BREVO_API,
        headers={"api-key": api_key, "content-type": "application/json", "accept": "application/json"},
        json=body,
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo {resp.status_code}: {resp.text[:300]}")


def mark_sent(token: str, page_id: str, now_utc: datetime) -> None:
    body = {
        "properties": {
            "Mail gesendet": {"checkbox": True},
            "Gesendet am": {"date": {"start": now_utc.isoformat()}},
            "Fehler": {"rich_text": []},
        }
    }
    resp = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=notion_headers(token),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


def mark_failed(token: str, page_id: str, err_msg: str, new_count: int) -> None:
    body = {
        "properties": {
            "Fehler": {"rich_text": [{"text": {"content": err_msg[:1900]}}]},
            "Retry-Count": {"number": new_count},
        }
    }
    requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=notion_headers(token),
        json=body,
        timeout=15,
    )


def send_alert(api_key: str, sender_email: str, sender_name: str,
               alert_email: str, subject: str, body_text: str) -> None:
    if not alert_email:
        return
    try:
        requests.post(
            BREVO_API,
            headers={"api-key": api_key, "content-type": "application/json", "accept": "application/json"},
            json={
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": alert_email}],
                "subject": subject,
                "textContent": body_text,
                "tags": ["checkin-mailer-alert"],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"alert mail failed: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No mail send, no Notion update")
    args = parser.parse_args()

    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    notion_token = env("NOTION_TOKEN", required=True)
    notion_db_id = env("NOTION_DB_ID", required=True)
    brevo_key = env("BREVO_API_KEY", required=not dry_run)
    sender_email = env("BREVO_SENDER_EMAIL", "hallo@kanlaya-massagepraxis.berlin")
    sender_name = env("BREVO_SENDER_NAME", "Kanlaya Thai Massage")
    alert_email = env("ALERT_EMAIL", "lukaskuth@gmail.com")
    test_recipient = env("TEST_RECIPIENT", "")

    env_jinja = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )

    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc.isoformat()}] checking pending check-ins (dry_run={dry_run})")

    pages = query_pending(notion_token, notion_db_id, now_utc)
    rows = [r for r in (extract_row(p) for p in pages) if r]
    print(f"found {len(pages)} matching pages → {len(rows)} actionable rows")

    sent = 0
    skipped_retry = 0
    failed = 0

    for row in rows:
        if row["retry_count"] >= MAX_RETRIES:
            skipped_retry += 1
            print(f"  SKIP {row['email']} — retry_count={row['retry_count']} (max reached)")
            continue

        recipient = test_recipient or row["email"]
        try:
            subject, html = render_mail(env_jinja, row["vorname"], row["email"])
            if dry_run:
                print(f"  DRY  {recipient} ({row['vorname']}) — would send '{subject}'")
            else:
                send_brevo(brevo_key, sender_email, sender_name,
                           recipient, row["vorname"], subject, html)
                mark_sent(notion_token, row["page_id"], now_utc)
                print(f"  SENT {recipient} ({row['vorname']}) → page {row['page_id']}")
            sent += 1
        except Exception as e:
            failed += 1
            err_msg = f"{type(e).__name__}: {e}"
            print(f"  FAIL {row['email']} — {err_msg}", file=sys.stderr)
            if not dry_run:
                new_count = row["retry_count"] + 1
                mark_failed(notion_token, row["page_id"], err_msg, new_count)
                if new_count >= MAX_RETRIES and alert_email and brevo_key:
                    send_alert(
                        brevo_key, sender_email, sender_name, alert_email,
                        f"Kanlaya Check-in Mailer: {row['email']} 3× gescheitert",
                        f"Page-ID: {row['page_id']}\nLetzter Fehler: {err_msg}\n\nBitte manuell prüfen.",
                    )

    summary = {
        "sent": sent,
        "skipped_retry": skipped_retry,
        "failed": failed,
        "dry_run": dry_run,
    }
    print(json.dumps(summary))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
