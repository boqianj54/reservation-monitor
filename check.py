"""Core availability checker for a SevenRooms venue.

Queries the SevenRooms public availability endpoint across a date range and
returns only genuinely bookable openings that fall within the desired time window.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

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
# How wide a slice of the day the endpoint returns, in 15-minute steps around an
# implicit anchor time. This MUST be large enough to cover the whole service day:
# at the default of 16 the response stops at ~15:00, so an evening time window
# would match nothing and the monitor would silently never fire. 64 returns the
# venue's full 11:00-20:45 service day in a single request.
HALO_SIZE_INTERVAL = 64


@dataclass(frozen=True)
class Opening:
    date: str          # YYYY-MM-DD
    time_iso: str      # "2026-09-01 18:30:00"
    time_label: str    # "6:30 PM"
    venue_slug: str
    party_size: int

    def key(self) -> str:
        # Party size and venue are part of the identity: the same time for a
        # different party size is a different opening, and must alert again.
        return f"{self.venue_slug}|{self.party_size}|{self.time_iso}"


@dataclass
class ScanResult:
    """Outcome of one pass over the configured date range.

    `scanned_dates` lists only the days we actually heard back about, so callers
    can tell "no opening on that day" apart from "we never managed to look".
    """
    openings: list[Opening] = field(default_factory=list)
    scanned_dates: set[str] = field(default_factory=set)
    failed_dates: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed_dates


def _fetch(venue: str, party_size: int, start_date: str, num_days: int) -> dict:
    params = {
        "venue": venue,
        "party_size": str(party_size),
        "halo_size_interval": str(HALO_SIZE_INTERVAL),
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
    return slot.get("type") == "book"


def find_openings(config: dict) -> ScanResult:
    venue = config["venue_slug"]
    party = int(config["party_size"])
    earliest = config.get("earliest_time", "00:00")
    latest = config.get("latest_time", "23:59")

    result = ScanResult()
    chunks = list(_daterange_chunks(
        config["date_start"], config["date_end"], MAX_DAYS_PER_REQUEST
    ))
    for i, (start_date, num_days) in enumerate(chunks):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            payload = _fetch(venue, party, start_date, num_days)
        except Exception as exc:
            # One bad day must not discard the days that did work — a persistent
            # 400 on a single date would otherwise blank out the whole range.
            result.failed_dates[start_date] = f"{type(exc).__name__}: {exc}"
            continue

        result.scanned_dates.add(start_date)
        avail = (payload.get("data") or {}).get("availability") or {}
        for date_str, shifts in avail.items():
            if date_str < config["date_start"] or date_str > config["date_end"]:
                continue
            for shift in shifts:
                # Both flags live at the shift level, not on individual slots.
                if shift.get("is_closed") or shift.get("is_forced_empty_availability"):
                    continue
                for slot in shift.get("times", []):
                    if not _is_bookable(slot):
                        continue
                    time_iso = slot.get("time_iso") or ""
                    if len(time_iso) < 16:
                        continue  # malformed entry; skip rather than crash
                    hhmm = time_iso[11:16]
                    if hhmm < earliest or hhmm > latest:
                        continue
                    result.openings.append(
                        Opening(
                            date=date_str,
                            time_iso=time_iso,
                            time_label=slot.get("time", hhmm),
                            venue_slug=venue,
                            party_size=party,
                        )
                    )
    result.openings.sort(key=lambda o: o.time_iso)
    return result


def _load_config(path: str = "config.json") -> dict:
    """Load search criteria, preferring the MONITOR_CONFIG environment variable.

    The repo is public, so the real venue/date window is not committed: CI passes
    it in as a secret and only config.example.json is tracked. Locally, an
    untracked config.json is used instead.
    """
    raw = os.environ.get("MONITOR_CONFIG", "").strip()
    if raw:
        return json.loads(raw)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"No configuration found: set MONITOR_CONFIG or create {path} "
            "(copy config.example.json)."
        )


if __name__ == "__main__":
    cfg = _load_config()
    scan = find_openings(cfg)
    if scan.failed_dates:
        print(f"WARNING: {len(scan.failed_dates)} day(s) could not be checked:")
        for date_str, err in sorted(scan.failed_dates.items()):
            print(f"  {date_str}: {err}")
    if not scan.openings:
        print(f"No bookable openings for {cfg['venue_name']} "
              f"({cfg['date_start']} to {cfg['date_end']}, party of {cfg['party_size']}).")
    else:
        print(f"Found {len(scan.openings)} opening(s) for {cfg['venue_name']}:")
        for o in scan.openings:
            print(f"  {o.date} — {o.time_label}")
