#!/usr/bin/env python3
"""Plaza Resident Services housing watcher."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

API_URL = os.environ.get(
    "PLAZA_URL",
    "https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json",
)
DETAIL_BASE = "https://plaza.newnewnew.space/aanbod/huurwoningen/details/"
OVERVIEW_URL = "https://plaza.newnewnew.space/aanbod/wonen"

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))

WATCH_REGIONS = [
    r.strip().lower()
    for r in os.environ.get("WATCH_REGIONS", "Nederland - Zuid-Holland").split(",")
    if r.strip()
]
WATCH_CITIES = [
    c.strip().lower()
    for c in os.environ.get("WATCH_CITIES", "Delft").split(",")
    if c.strip()
]
URGENT_CITIES = [
    c.strip().lower()
    for c in os.environ.get("URGENT_CITIES", "Delft").split(",")
    if c.strip()
]
EXCLUDE_CATEGORIES = [
    c.strip().lower()
    for c in os.environ.get("EXCLUDE_CATEGORIES", "voorVoertuig").split(",")
    if c.strip()
]
MAX_RENT = float(os.environ.get("MAX_RENT", "0") or 0)

RECIPIENTS_RAW = os.environ.get("WHATSAPP_RECIPIENTS", "")

HEARTBEAT_HOURS = sorted(
    {
        int(h.strip())
        for h in os.environ.get("HEARTBEAT_HOURS", "10,16").split(",")
        if h.strip().isdigit()
    }
)
HEARTBEAT_TZ = os.environ.get("HEARTBEAT_TZ", "Europe/Amsterdam")
HEARTBEAT_TO = os.environ.get("HEARTBEAT_TO", "first").strip().lower()

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
GIT_PERSIST = os.environ.get("GIT_PERSIST", "").lower() in {"1", "true", "yes"}

POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "0") or 0)
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "60") or 60)

MAX_LISTINGS_IN_MESSAGE = 5
USER_AGENT = "Mozilla/5.0 (compatible; plaza-watcher/1.1)"


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def local_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(HEARTBEAT_TZ))
        except Exception as exc:
            log(f"timezone {HEARTBEAT_TZ} unavailable ({exc}); using UTC")
    return datetime.now(timezone.utc)


def fetch_listings() -> list[dict]:
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


def due_heartbeat_slot(now: datetime) -> str | None:
    if not HEARTBEAT_HOURS:
        return None
    passed = []
    for days_back in (0, 1):
        day = (now - timedelta(days=days_back)).date()
        for hour in HEARTBEAT_HOURS:
            slot = datetime.combine(day, time(hour), tzinfo=now.tzinfo)
            if slot <= now:
                passed.append(slot)
    if not passed:
        return None
    return max(passed).strftime("%Y-%m-%dT%H")


def heartbeat_text(matching: int, state: dict, now: datetime) -> str:
    where = ", ".join(w.title() for w in WATCH_CITIES) or "the watched area"
    lines = [
        f"Plaza watcher check-in, {now:%a %d %b %H:%M} ({HEARTBEAT_TZ}).",
        f"Still running. Tracking {matching} listing(s) in {where} and the "
        "watched region.",
    ]
    last_new = state.get("last_new")
    lines.append(
        f"Last new listing alert: {last_new}."
        if last_new
        else "No new listing has appeared since the watcher started."
    )
    return "\n".join(lines)


def parse_recipients(raw: str) -> list[tuple[str, str, str]]:
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
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey}
    )
    if DRY_RUN:
        log(f"DRY RUN - would WhatsApp {name} ({phone}):\n{text}\n")
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=90) as resp:
            status = resp.status
            raw = resp.read(8000).decode("utf-8", "replace")

        reply = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
        reply = re.sub(
            r"Text to send:.*?(?=(Message queued|APIKEY|$))", "", reply, flags=re.I
        )
        reply = reply.strip() or "(empty reply)"

        bad = any(
            marker in reply.lower()
            for marker in (
                "not valid",
                "invalid",
                "not activated",
                "error",
                "wrong",
                "denied",
                "not allowed",
            )
        )
        if status != 200 or bad:
            log(
                f"WARNING - {name} ({phone}) not delivered: "
                f"HTTP {status} :: {reply[:400]}"
            )
            return False

        log(f"sent to {name} ({phone}): HTTP {status} :: {reply[:400]}")
        return True
    except urllib.error.HTTPError as exc:
        log(f"FAILED to send to {name}: HTTP {exc.code} {exc.reason}")
    except Exception as exc:
        log(f"FAILED to send to {name}: {exc}")
    return False


def notify(text: str, people: list[tuple[str, str, str]] | None = None) -> None:
    if people is None:
        people = parse_recipients(RECIPIENTS_RAW)
    if not people:
        log("no recipients configured (WHATSAPP_RECIPIENTS is empty)")
        log(text)
        return
    for index, (name, phone, apikey) in enumerate(people):
        if index:
            time_module.sleep(8)
        send_whatsapp(name, phone, apikey, text)


def notify_all(text: str) -> None:
    notify(text)


def notify_heartbeat(text: str) -> None:
    people = parse_recipients(RECIPIENTS_RAW)
    if HEARTBEAT_TO != "all":
        people = people[:1]
    notify(text, people)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log(f"state file unreadable ({exc}); starting fresh")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["seen"] = list(payload.get("seen") or [])[-500:]
    STATE_FILE.write_text(json.dumps(payload, indent=1) + "\n")


def git_persist_state() -> None:
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
            return
        subprocess.run(
            ["git", "commit", "-m", "watcher: update seen listings"],
            check=True,
            timeout=30,
        )
        subprocess.run(["git", "pull", "--rebase", "--autostash"], timeout=120)
        subprocess.run(["git", "push"], check=True, timeout=120)
        log("state committed to repo")
    except Exception as exc:
        log(f"could not persist state to git: {exc}")


def check_once() -> bool:
    try:
        listings = fetch_listings()
    except Exception as exc:
        log(f"fetch failed: {exc}")
        return False

    interesting = [o for o in listings if is_interesting(o)]
    log(
        f"feed: {len(listings)} listings total, "
        f"{len(interesting)} matching the filter"
    )

    state = load_state()
    seen = list(state.get("seen") or [])
    first_run = "seen" not in state
    now = local_now()
    slot = due_heartbeat_slot(now)

    current_ids = [listing_id(o) for o in interesting if listing_id(o)]
    new_objects = [
        o for o in interesting if listing_id(o) and listing_id(o) not in set(seen)
    ]

    if first_run:
        state["seen"] = current_ids
        state["last_heartbeat"] = slot
        save_state(state)
        git_persist_state()
        notify_all(
            "Plaza watcher is live. Now tracking "
            f"{len(current_ids)} matching listing(s); you will get a message here "
            "the moment a new one appears."
        )
        return True

    changed = False

    if new_objects:
        log(f"NEW: {[listing_id(o) for o in new_objects]}")
        notify_all(build_message(new_objects))
        state["seen"] = seen + [listing_id(o) for o in new_objects]
        state["last_new"] = now.strftime("%d %b %H:%M")
        changed = True

    if slot and state.get("last_heartbeat") != slot:
        log(f"heartbeat due for slot {slot}")
        notify_heartbeat(heartbeat_text(len(interesting), state, now))
        state["last_heartbeat"] = slot
        changed = True

    if changed:
        save_state(state)
        git_persist_state()
    return changed


def main() -> int:
    if POLL_MINUTES <= 0:
        check_once()
        return 0

    deadline = time_module.monotonic() + POLL_MINUTES * 60
    log(
        f"polling every {POLL_SECONDS:.0f}s for {POLL_MINUTES:.0f} minutes; "
        f"heartbeat at {HEARTBEAT_HOURS} {HEARTBEAT_TZ} to "
        f"{'everyone' if HEARTBEAT_TO == 'all' else 'the first recipient'}"
    )
    while time_module.monotonic() < deadline:
        check_once()
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        time_module.sleep(min(POLL_SECONDS, remaining))
    log("poll window finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
