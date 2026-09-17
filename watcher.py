#!/usr/bin/env python3
"""
Plaza Resident Services housing watcher.

Polls the public listings feed of plaza.newnewnew.space, keeps track of which
listings it has already seen, and sends a WhatsApp message to every configured
recipient when a new listing appears in the regions/cities being watched.

Standard library only - no pip install needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration (everything can be overridden with environment variables)
# --------------------------------------------------------------------------

API_URL = os.environ.get(
    "PLAZA_URL",
    "https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json",
)
DETAIL_BASE = "https://plaza.newnewnew.space/aanbod/huurwoningen/details/"
OVERVIEW_URL = "https://plaza.newnewnew.space/aanbod/wonen"

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))

# A listing matches if its region starts with one of these...
WATCH_REGIONS = [
    r.strip().lower()
    for r in os.environ.get("WATCH_REGIONS", "Nederland - Zuid-Holland").split(",")
    if r.strip()
]
# ...or its city is one of these (kept separate so you can add cities in other
# provinces without widening the whole region filter).
WATCH_CITIES = [
    c.strip().lower()
    for c in os.environ.get("WATCH_CITIES", "Delft").split(",")
    if c.strip()
]
# Cities that get the loud treatment in the message.
URGENT_CITIES = [
    c.strip().lower()
    for c in os.environ.get("URGENT_CITIES", "Delft").split(",")
    if c.strip()
]
# dwellingType.categorie values to ignore (parking spots, storage boxes...).
EXCLUDE_CATEGORIES = [
    c.strip().lower()
    for c in os.environ.get("EXCLUDE_CATEGORIES", "voorVoertuig").split(",")
    if c.strip()
]
# 0 = no cap. Compared against totalRent (rent incl. service costs).
MAX_RENT = float(os.environ.get("MAX_RENT", "0") or 0)

# "Name:+31612345678:apikey, Name:+48...:apikey" - one entry per recipient.
RECIPIENTS_RAW = os.environ.get("WHATSAPP_RECIPIENTS", "")

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
GIT_PERSIST = os.environ.get("GIT_PERSIST", "").lower() in {"1", "true", "yes"}

POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "0") or 0)
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "60") or 60)

MAX_LISTINGS_IN_MESSAGE = 5
USER_AGENT = "Mozilla/5.0 (compatible; plaza-watcher/1.0)"


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Fetching and filtering
# --------------------------------------------------------------------------


def fetch_listings() -> list[dict]:
    """Return the raw list of currently published objects."""
    req = urllib.request.Request(
        API_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.load(resp)
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError("unexpected payload: no 'result' list")
    return result


def _name(obj: dict, key: str) -> str:
    value = obj.get(key)
    if isinstance(value, dict):
        return (value.get("name") or value.get("localizedName") or "").strip()
    return (value or "").strip()


def is_interesting(obj: dict) -> bool:
    if not obj.get("isGepubliceerd", True):
        return False

    city = _name(obj, "city").lower()
    region = _name(obj, "regio").lower()

    in_region = any(region.startswith(r) for r in WATCH_REGIONS)
    in_city = city in WATCH_CITIES
    if not (in_region or in_city):
        return False

    dwelling = obj.get("dwellingType") or {}
    categorie = (dwelling.get("categorie") or "").lower()
    if categorie in EXCLUDE_CATEGORIES:
        return False

    if MAX_RENT:
        try:
            if float(obj.get("totalRent") or 0) > MAX_RENT:
                return False
        except (TypeError, ValueError):
            pass

    return True


def listing_id(obj: dict) -> str:
    return str(obj.get("id") or obj.get("urlKey") or "")


def describe(obj: dict) -> str:
    """One compact block of text per listing, for the WhatsApp message."""
    dwelling = obj.get("dwellingType") or {}
    kind = (dwelling.get("localizedName") or "Woning").strip()

    address = " ".join(
        part
        for part in (
            (obj.get("street") or "").strip(),
            str(obj.get("houseNumber") or "").strip(),
            (obj.get("houseNumberAddition") or "").strip(),
        )
        if part
    )
    city = _name(obj, "city") or _name(obj, "regio")
    urgent = city.lower() in URGENT_CITIES

    lines = []
    head = f"{kind} - {address}, {city}" if address else f"{kind} - {city}"
    lines.append(("*** " + head + " ***") if urgent else head)

    try:
        rent = float(obj.get("totalRent") or 0)
    except (TypeError, ValueError):
        rent = 0
    if rent:
        lines.append(f"Rent: EUR {rent:.0f}/month")

    area = obj.get("areaDwelling") or 0
    if area:
        lines.append(f"Size: {area} m2")

    if obj.get("availableFromDate"):
        lines.append(f"Available from: {obj['availableFromDate']}")

    closing = (obj.get("closingDate") or "").strip()
    if closing and not closing.startswith("0000"):
        lines.append(f"Responses close: {closing}")

    url_key = (obj.get("urlKey") or "").strip()
    if url_key:
        lines.append(DETAIL_BASE + url_key)

    return "\n".join(lines)


def build_message(new_objects: list[dict]) -> str:
    urgent = [o for o in new_objects if _name(o, "city").lower() in URGENT_CITIES]
    header = (
        f"PLAZA ALERT: {len(new_objects)} new listing"
        f"{'s' if len(new_objects) != 1 else ''}"
    )
    if urgent:
        header += f" - {len(urgent)} in {_name(urgent[0], 'city')}!"

    ordered = urgent + [o for o in new_objects if o not in urgent]
    blocks = [describe(o) for o in ordered[:MAX_LISTINGS_IN_MESSAGE]]

    body = "\n\n".join(blocks)
    if len(ordered) > MAX_LISTINGS_IN_MESSAGE:
        body += f"\n\n(+{len(ordered) - MAX_LISTINGS_IN_MESSAGE} more: {OVERVIEW_URL})"

    return f"{header}\n\n{body}"


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------


def parse_recipients(raw: str) -> list[tuple[str, str, str]]:
    """Parse 'Name:+31...:apikey, Name:+48...:apikey' into tuples."""
    people = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 3 or not all(parts):
            log(f"skipping malformed recipient entry: {entry!r}")
            continue
        people.append((parts[0], parts[1], parts[2]))
    return people


def send_whatsapp(name: str, phone: str, apikey: str, text: str) -> bool:
    """Send one message through the CallMeBot free WhatsApp relay."""
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey}
    )
    if DRY_RUN:
        log(f"DRY RUN - would WhatsApp {name} ({phone}):\n{text}\n")
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read(400).decode("utf-8", "replace")
        log(f"sent to {name} ({phone}): HTTP {resp.status} {body[:120]!r}")
        return True
    except urllib.error.HTTPError as exc:
        log(f"FAILED to send to {name}: HTTP {exc.code} {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - never let a send crash the watcher
        log(f"FAILED to send to {name}: {exc}")
    return False


def notify_all(text: str) -> None:
    people = parse_recipients(RECIPIENTS_RAW)
    if not people:
        log("no recipients configured (WHATSAPP_RECIPIENTS is empty)")
        log(text)
        return
    for index, (name, phone, apikey) in enumerate(people):
        if index:
            time.sleep(8)  # CallMeBot throttles rapid-fire requests
        send_whatsapp(name, phone, apikey, text)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log(f"state file unreadable ({exc}); starting fresh")
        return {}


def save_state(seen: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seen": seen[-500:],  # keep the file small
            },
            indent=1,
        )
        + "\n"
    )


def git_persist_state() -> None:
    """Commit the state file back to the repo so the next run remembers."""
    if not GIT_PERSIST:
        return
    try:
        subprocess.run(
            ["git", "config", "user.name", "plaza-watcher"], check=True, timeout=30
        )
        subprocess.run(
            ["git", "config", "user.email", "plaza-watcher@users.noreply.github.com"],
            check=True,
            timeout=30,
        )
        subprocess.run(["git", "add", str(STATE_FILE)], check=True, timeout=30)
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], timeout=30
        ).returncode
        if status == 0:
            return  # nothing changed
        subprocess.run(
            ["git", "commit", "-m", "watcher: update seen listings"],
            check=True,
            timeout=30,
        )
        subprocess.run(["git", "pull", "--rebase", "--autostash"], timeout=120)
        subprocess.run(["git", "push"], check=True, timeout=120)
        log("state committed to repo")
    except Exception as exc:  # noqa: BLE001
        log(f"could not persist state to git: {exc}")


# --------------------------------------------------------------------------
# One check
# --------------------------------------------------------------------------


def check_once() -> bool:
    """Returns True if the state changed."""
    try:
        listings = fetch_listings()
    except Exception as exc:  # noqa: BLE001
        log(f"fetch failed: {exc}")
        return False

    interesting = [o for o in listings if is_interesting(o)]
    log(
        f"feed: {len(listings)} listings total, "
        f"{len(interesting)} matching the filter"
    )

    state = load_state()
    seen = list(state.get("seen") or [])
    first_run = not seen and "seen" not in state

    current_ids = [listing_id(o) for o in interesting if listing_id(o)]
    new_objects = [
        o for o in interesting if listing_id(o) and listing_id(o) not in set(seen)
    ]

    if first_run:
        save_state(current_ids)
        git_persist_state()
        notify_all(
            "Plaza watcher is live. Now tracking "
            f"{len(current_ids)} matching listing(s); you will get a message here "
            "the moment a new one appears."
        )
        return True

    if not new_objects:
        return False

    log(f"NEW: {[listing_id(o) for o in new_objects]}")
    notify_all(build_message(new_objects))
    save_state(seen + [listing_id(o) for o in new_objects])
    git_persist_state()
    return True


def main() -> int:
    if POLL_MINUTES <= 0:
        check_once()
        return 0

    deadline = time.monotonic() + POLL_MINUTES * 60
    log(f"polling every {POLL_SECONDS:.0f}s for {POLL_MINUTES:.0f} minutes")
    while time.monotonic() < deadline:
        check_once()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_SECONDS, remaining))
    log("poll window finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
