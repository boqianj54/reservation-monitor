"""Core availability checker for a SevenRooms venue.

Queries the SevenRooms public availability endpoint across a date range and
returns only genuinely bookable openings that fall within the desired time window.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

AVAILABILITY_URL = "https://www.sevenrooms.com/api-yoa/availability/widget/range"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
# SevenRooms' range endpoint only reliably accepts num_days=1 for this venue
# (num_days=2 returns HTTP 400 deterministically), so we page one day at a time.
MAX_DAYS_PER_REQUEST = 1
# Polite delay between per-day requests so a wide date range doesn't hammer the API.
REQUEST_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class Opening:
    date: str          # YYYY-MM-DD
    time_iso: str      # "2026-09-01 18:30:00"
    time_label: str    # "6:30 PM"
    shift: str         # e.g. "DINNER"

    def key(self) -> str:
        return f"{self.time_iso}"


def _fetch(venue: str, party_size: int, start_date: str, num_days: int) -> dict:
    params = {
        "venue": venue,
        "party_size": str(party_size),
        "halo_size_interval": "16",
        "start_date": start_date,
        "num_days": str(num_days),
        "channel": "SEVENROOMS_WIDGET",
    }
    url = AVAILABILITY_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _daterange_chunks(start: str, end: str, chunk: int):
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    cur = d0
    while cur <= d1:
        span = min(chunk, (d1 - cur).days + 1)
        yield cur.isoformat(), span
        cur += dt.timedelta(days=span)


def _is_bookable(slot: dict) -> bool:
    """A real, reservable opening — not a request-only/waitlist placeholder."""
    if slot.get("type") != "book":
        return False
    # Defensive: some payloads also carry an access id on real slots.
    return not slot.get("is_forced_empty_availability", False)


def find_openings(config: dict) -> list[Opening]:
    venue = config["venue_slug"]
    party = int(config["party_size"])
    earliest = config.get("earliest_time", "00:00")
    latest = config.get("latest_time", "23:59")

    openings: list[Opening] = []
    chunks = list(_daterange_chunks(
        config["date_start"], config["date_end"], MAX_DAYS_PER_REQUEST
    ))
    for i, (start_date, num_days) in enumerate(chunks):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        payload = _fetch(venue, party, start_date, num_days)
        avail = (payload.get("data") or {}).get("availability") or {}
        for date_str, shifts in avail.items():
            if date_str < config["date_start"] or date_str > config["date_end"]:
                continue
            for shift in shifts:
                if shift.get("is_closed"):
                    continue
                shift_cat = shift.get("shift_category", "")
                for slot in shift.get("times", []):
                    if not _is_bookable(slot):
                        continue
                    hhmm = slot["time_iso"][11:16]  # "18:30"
                    if hhmm < earliest or hhmm > latest:
                        continue
                    openings.append(
                        Opening(
                            date=date_str,
                            time_iso=slot["time_iso"],
                            time_label=slot.get("time", hhmm),
                            shift=shift_cat,
                        )
                    )
    openings.sort(key=lambda o: o.time_iso)
    return openings


def _load_config(path: str = "config.json") -> dict:
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    cfg = _load_config()
    found = find_openings(cfg)
    if not found:
        print(f"No bookable openings for {cfg['venue_name']} "
              f"({cfg['date_start']} to {cfg['date_end']}, party of {cfg['party_size']}).")
    else:
        print(f"Found {len(found)} opening(s) for {cfg['venue_name']}:")
        for o in found:
            print(f"  {o.date} ({o.shift}) — {o.time_label}")
