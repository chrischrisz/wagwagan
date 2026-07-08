"""
WagForex Scorecard Fetcher (GitHub Actions version)
====================================================

Runs on a schedule (see .github/workflows/fetch.yml). Each run:
  1. Logs into Telegram using your saved StringSession (no phone code needed)
  2. Reads the last-processed message ID from a "State" tab in your Sheet
  3. Fetches any new messages in the group since that ID
  4. Parses any that match the WAGFOREX SCORECARD format
  5. Writes the raw D1/H4/H1 scores to a "RawScores" tab
  6. Updates the "State" tab with the newest message ID processed

No live connection needed - it connects briefly, does its work, disconnects.
This is intentionally fetch-only (no bias/rule computation) - that part can
live in Apps Script inside the Sheet itself, working off this raw data.
"""

import os
import re
from datetime import datetime

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

import gspread
from google.oauth2.service_account import Credentials

# =============================================================================
# CONFIG - values are pulled from environment variables (set via GitHub Secrets)
# =============================================================================

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION"]
_raw_group = os.environ["TG_GROUP"]  # @username or numeric chat id
try:
    # Telethon only treats this as a real chat ID if it's an int.
    # A numeric string (even "-1003781098958") gets misread as a
    # username/phone lookup and fails, so convert when possible.
    GROUP_IDENTIFIER = int(_raw_group)
except ValueError:
    GROUP_IDENTIFIER = _raw_group

SPREADSHEET_ID = os.environ["SHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # raw JSON contents

SHEET_TAB_RAW = "RawScores"
SHEET_TAB_STATE = "State"

# =============================================================================
# PARSER (same format as before)
# =============================================================================

CURRENCY_BLOCK_RE = re.compile(
    r"(AUD|CAD|EUR|GBP|JPY|NZD|USD)\s*\n"
    r"\s*D1\s*:\s*([^\|\n]+)\|?\s*H4\s*:\s*([^\|\n]+)\|?\s*H1\s*:\s*([^\n]+)",
    re.IGNORECASE,
)


def resolve_value(raw: str) -> int:
    """
    Some timeframes occasionally show more than one value, e.g. 'H1: -1/+2'.
    In that case, keep whichever has the larger absolute magnitude.
    If magnitudes tie, the first value listed wins.
    """
    numbers = [int(n) for n in re.findall(r"[+-]?\d+", raw)]
    if not numbers:
        raise ValueError(f"No numeric value found in '{raw}'")
    return max(numbers, key=lambda v: abs(v))


def parse_scorecard(text: str) -> dict:
    matches = CURRENCY_BLOCK_RE.findall(text)
    if not matches:
        return {}
    scores = {}
    for currency, d1, h4, h1 in matches:
        scores[currency.upper()] = {
            "D1": resolve_value(d1),
            "H4": resolve_value(h4),
            "H1": resolve_value(h1),
        }
    return scores


# =============================================================================
# GOOGLE SHEETS HELPERS
# =============================================================================

def get_sheet_client():
    import json
    import tempfile

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    # Service account JSON comes in as a raw string from the GitHub Secret;
    # write it to a temp file since Credentials expects a file path.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(SERVICE_ACCOUNT_JSON)
        temp_path = f.name

    creds = Credentials.from_service_account_file(temp_path, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_tab(sh, title, header_row):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(6, len(header_row)))
        ws.append_row(header_row)
    return ws


def get_last_processed_id(ws_state) -> int:
    try:
        value = ws_state.acell("B1").value
        return int(value) if value else 0
    except Exception:
        return 0


def set_last_processed_id(ws_state, message_id: int):
    ws_state.update_acell("A1", "last_processed_message_id")
    ws_state.update_acell("B1", str(message_id))


# =============================================================================
# MAIN
# =============================================================================

def main():
    gc = get_sheet_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    ws_raw = get_or_create_tab(
        sh, SHEET_TAB_RAW,
        ["Timestamp", "MessageID", "Currency", "D1", "H4", "H1"],
    )
    ws_state = get_or_create_tab(sh, SHEET_TAB_STATE, ["key", "value"])

    last_id = get_last_processed_id(ws_state)
    highest_id_seen = last_id

    with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        # GitHub Actions runners are fresh every run, so Telethon's entity
        # cache is always empty. Loading dialogs first populates that cache
        # so numeric chat IDs (not just @usernames) resolve correctly below.
        client.get_dialogs()

        # min_id fetches only messages newer than last_id
        messages = list(client.iter_messages(GROUP_IDENTIFIER, min_id=last_id))

    if not messages:
        print("No new messages since last run.")
        return

    # iter_messages returns newest-first; process oldest-first so Sheet rows
    # stay in chronological order
    messages.reverse()

    new_rows = []
    for msg in messages:
        highest_id_seen = max(highest_id_seen, msg.id)
        text = msg.raw_text or ""
        parsed = parse_scorecard(text)
        if not parsed:
            continue

        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S UTC")
        for currency, vals in parsed.items():
            new_rows.append([
                timestamp, msg.id, currency, vals["D1"], vals["H4"], vals["H1"]
            ])

    if new_rows:
        ws_raw.append_rows(new_rows)
        print(f"Wrote {len(new_rows)} currency rows from {len(messages)} new message(s).")
    else:
        print(f"Checked {len(messages)} new message(s), none matched the scorecard format.")

    set_last_processed_id(ws_state, highest_id_seen)


if __name__ == "__main__":
    main()
