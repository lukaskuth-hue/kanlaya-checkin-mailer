# Kanlaya Check-in Mailer

90-Min-Follow-up nach iPad-Check-in in der Praxis.

```
Check-in (intake.php)
    → Notion CRM
        → GitHub Actions (alle 5 Min)
            → Brevo Transactional API
                → Mail an Gast
                    → Notion-Update (Mail gesendet=true)
```

**Source of Truth:** Notion-DB `34e37961-1495-8130-97b6-eb74efb3fef9`
**Spec:** `../Checkin-Mailer.md` im Kanlaya-Projektordner

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NOTION_TOKEN=…
export NOTION_DB_ID=34e37961-1495-8130-97b6-eb74efb3fef9
export BREVO_API_KEY=…
export ALERT_EMAIL=lukaskuth@gmail.com

python mailer.py --dry-run    # zeigt was rausginge
python mailer.py              # echter Versand
```

## Env-Vars

| Var | Pflicht | Default | Zweck |
|---|---|---|---|
| `NOTION_TOKEN` | ja | — | Notion-Integration-Token |
| `NOTION_DB_ID` | ja | — | Kanlaya-CRM-DB-ID |
| `BREVO_API_KEY` | ja (außer Dry-Run) | — | Brevo Transactional Key |
| `BREVO_SENDER_EMAIL` | nein | `hallo@kanlaya-massagepraxis.berlin` | Absender |
| `BREVO_SENDER_NAME` | nein | `Kanlaya Thai Massage` | Absender-Name |
| `ALERT_EMAIL` | nein | `lukaskuth@gmail.com` | Empfänger für 3×-Fehler-Alerts |
| `TEST_RECIPIENT` | nein | — | Wenn gesetzt: ALLE Mails an diese Adresse (Live-Test) |
| `DRY_RUN` | nein | — | `1` = nur loggen, kein Versand |

## Cron

GitHub Actions Workflow `.github/workflows/checkin-mailer.yml` läuft alle 5 Min.

## Verhalten

- Pickt nur Notion-Rows mit `Marketing-Consent=true`, `Mail gesendet=false`, `Letzter Besuch` zwischen `now-24h` und `now-90min`.
- Rows ohne Uhrzeit auf `Letzter Besuch` werden übersprungen (intake.php schreibt seit 2026-05-20 vollen ISO-Timestamp).
- Bei Brevo-Fehler: `Fehler`-Property + `Retry-Count +1` in Notion. Nach 3× → Alert-Mail an `ALERT_EMAIL`, Row wird skipped.
- Idempotent durch `Mail gesendet`-Flag.

## Template

`templates/bonus_mail.html` — Newsletter-v2-Stil (Forest + Gold + Sand). Email-safe (Tabellen, Inline-CSS, MSO-Conditional). Variablen: `vorname`, `bonus_code`, `fresha_link`, `google_review_link`, `unsubscribe_link`, `datum_de`.
