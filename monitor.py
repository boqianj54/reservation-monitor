"""Entrypoint for the scheduled run: check availability, notify on new openings.

Notifies once per distinct opening. Already-notified slots are recorded in
state.json, which the GitHub Actions workflow commits back to the repo so the
next run remembers them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
from zoneinfo import ZoneInfo

from check import Opening, ScanResult, find_openings, _load_config

STATE_PATH = "state.json"
# Deep link straight into the booking widget with the slot preselected, so the
# alert lands on the form with only payment left to do. The older
# /reservations/{slug} path 302s and 404s once query params are added.
BOOKING_URL = (
    "https://www.sevenrooms.com/explore/{slug}/reservations/create/search"
    "?date={date}&party_size={party}&start_time={time}"
)
# How many openings to spell out in the notification body before summarising.
MAX_LISTED = 3
# state.json is committed to a public repo, so entries are stored as salted
# digests rather than "venue|party|date time" — otherwise the file would
# publish the venue and the exact dates being watched. Without STATE_SALT the
# values are low-entropy enough to brute-force, so the salt is a secret.
STATE_SALT = os.environ.get("STATE_SALT", "")
# Entries are aged out by when they were recorded (not by slot date, which we
# deliberately no longer store). forget_vanished() does the real cleanup.
STATE_TTL_DAYS = 180
# Keep alerts alive for a day: the whole point of cloud hosting is that the
# phone may be off or offline for hours, and APNs drops anything past expiry.
ALERT_TTL_SECONDS = 24 * 3600
# Fail the run if more than this fraction of days could not be checked, so a
# widespread outage goes red instead of quietly reporting "no openings".
MAX_FAILED_FRACTION = 0.5


def venue_today(config: dict) -> dt.date:
    """Today's date *at the venue*.

    The runner is UTC and slot keys are venue-local. Using the UTC date would
    expire the current evening's slots up to 10 hours early in Hawaii, which
    makes a still-live slot look new again on the very next run.
    """
    tz = config.get("timezone")
    if not tz:
        return dt.date.today()
    return dt.datetime.now(ZoneInfo(tz)).date()


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": {}}
    state.setdefault("notified", {})
    return state


def save_state(state: dict, path: str = STATE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def state_key(opening: Opening) -> str:
    """Opaque, stable identifier for a slot, safe to publish."""
    return hmac.new(
        STATE_SALT.encode(), opening.key().encode(), hashlib.sha256
    ).hexdigest()[:32]


def prune_state(state: dict, today: dt.date) -> dict:
    """Age out old entries so state.json cannot grow forever.

    Pruning is by when the entry was recorded, because the slot's own date is
    deliberately not stored. forget_vanished() removes slots as soon as they
    stop being offered, so this is only a long-stop.
    """
    cutoff = today - dt.timedelta(days=STATE_TTL_DAYS)
    kept = {}
    for key, seen_at in state["notified"].items():
        try:
            seen_date = dt.datetime.fromisoformat(seen_at).date()
        except (TypeError, ValueError):
            continue  # unparseable entry: drop it rather than keep it forever
        if seen_date >= cutoff:
            kept[key] = seen_at
    state["notified"] = kept
    return state


def forget_vanished(state: dict, scan: ScanResult) -> list[str]:
    """Stop suppressing slots that are no longer offered.

    A table that gets booked and later cancelled is the single most valuable
    event this tool can catch, so once a slot disappears we must forget it —
    otherwise its reappearance would be silently swallowed.

    Only runs when every day in the range was reached: with slot dates no longer
    stored we cannot tell which entries a partial scan covered, so a failed day
    would otherwise look like the slot vanished and cause a spurious re-alert.
    """
    if not scan.ok:
        return []
    live = {state_key(o) for o in scan.openings}
    dropped = [key for key in state["notified"] if key not in live]
    for key in dropped:
        del state["notified"][key]
    return dropped


def booking_url(opening: Opening, config: dict) -> str:
    """Link that opens the booking form with this slot already selected."""
    d = dt.date.fromisoformat(opening.date)
    return BOOKING_URL.format(
        slug=config["venue_slug"],
        date=d.strftime("%m-%d-%Y"),   # SevenRooms wants MM-DD-YYYY
        party=config["party_size"],
        time=opening.time_iso[11:16],
    )


def format_alert(openings: list[Opening], config: dict) -> tuple[str, str]:
    count = len(openings)
    noun = "opening" if count == 1 else "openings"
    title = f"{config['venue_name']}: {count} {noun}"

    def label(o: Opening) -> str:
        day = dt.date.fromisoformat(o.date).strftime("%a %b %-d")
        return f"{day} at {o.time_label}"

    shown = [label(o) for o in openings[:MAX_LISTED]]
    body = ", ".join(shown)
    if count > MAX_LISTED:
        body += f", +{count - MAX_LISTED} more"
    body += f" — party of {config['party_size']}"
    return title, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check and diff against state, but send nothing and save nothing",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="send a single fake push to verify APNs credentials end-to-end",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print venue name, dates and slot times; omit in CI, whose logs are public",
    )
    args = parser.parse_args()

    config = _load_config()

    if args.test_notification:
        import notify

        invalid = notify.send(
            f"Test — {config['venue_name']}",
            "If you can read this, APNs delivery is working.",
            collapse_id="test",
        )
        if invalid:
            raise RuntimeError(
                f"{len(invalid)} device token(s) were rejected by APNs — "
                "the test push was not delivered to them."
            )
        print("Test notification sent.")
        return 0

    scan = find_openings(config)
    total_days = len(scan.scanned_dates) + len(scan.failed_dates)
    if scan.failed_dates:
        print(f"WARNING: {len(scan.failed_dates)}/{total_days} day(s) could not be checked:")
        for date_str, err in sorted(scan.failed_dates.items()):
            # Dates are only safe to print when we are not in the public log.
            print(f"  {date_str if args.verbose else '<day>'}: {err}")

    # CI logs are public on a public repo, so the default output names neither
    # the venue nor any date. Run locally with --verbose to see the details.
    if args.verbose:
        print(
            f"Checked {config['venue_name']} "
            f"({config['date_start']} to {config['date_end']}, "
            f"party of {config['party_size']}): {len(scan.openings)} bookable slot(s) "
            f"across {len(scan.scanned_dates)} day(s)."
        )
        for o in scan.openings:
            print(f"  {o.date} — {o.time_label}")
    else:
        print(f"Checked {len(scan.scanned_dates)} day(s): "
              f"{len(scan.openings)} bookable slot(s) in the configured window.")

    state = load_state()
    forgotten = forget_vanished(state, scan)
    if forgotten:
        print(f"{len(forgotten)} previously-notified slot(s) no longer offered "
              f"(will alert again if they return).")

    already = state["notified"]
    new = [o for o in scan.openings if state_key(o) not in already]

    today = venue_today(config)
    # A heartbeat keeps state.json changing at least daily. Without it the file
    # only changes when an opening appears, and GitHub disables scheduled
    # workflows on repos with no activity for 60 days — which for a venue that
    # books out months ahead means the monitor switches itself off unnoticed.
    state["last_checked_date"] = today.isoformat()

    if new:
        print(f"{len(new)} newly appeared opening(s).")
        if args.verbose:
            for o in new:
                print(f"  NEW  {o.date} — {o.time_label}")

    if args.dry_run:
        print("Dry run — no notification sent, state not saved.")
        return 0

    if new:
        import notify

        title, body = format_alert(new, config)
        invalid = notify.send(
            title,
            body,
            collapse_id=None,  # each alert must stand alone; see notify.send
            extra={"url": booking_url(new[0], config)},
            expiration_seconds=ALERT_TTL_SECONDS,
        )
        if invalid and len(invalid) == len(notify.device_tokens()):
            # Every device is unreachable, so nobody was told. Fail loudly and
            # do NOT record these as notified, or the alert is lost forever.
            raise RuntimeError(
                "APNs rejected every configured device token — no one was "
                "notified. The app was probably reinstalled; refresh the "
                "APNS_DEVICE_TOKENS secret from the app."
            )
        print(f"Notified: {title} — {body}" if args.verbose
              else f"Notified {len(new)} opening(s).")

        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for o in new:
            already[state_key(o)] = now
    else:
        print("Nothing new to notify.")

    save_state(prune_state(state, today))

    # Surface a widespread outage as a failed run rather than a quiet "0 slots".
    if total_days and len(scan.failed_dates) / total_days > MAX_FAILED_FRACTION:
        raise RuntimeError(
            f"{len(scan.failed_dates)} of {total_days} days could not be checked; "
            "availability results are unreliable."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - surface a clean error in CI logs
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
