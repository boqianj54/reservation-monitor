"""Entrypoint for the scheduled run: check availability, notify on new openings.

Notifies once per distinct opening. Already-notified slots are recorded in
state.json, which the GitHub Actions workflow commits back to the repo so the
next run remembers them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from check import Opening, find_openings, _load_config

STATE_PATH = "state.json"
BOOKING_URL = "https://www.sevenrooms.com/reservations/{slug}"
# How many openings to spell out in the notification body before summarising.
MAX_LISTED = 3


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"notified": {}}
    state.setdefault("notified", {})
    return state


def save_state(state: dict, path: str = STATE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state: dict, today: dt.date) -> dict:
    """Drop slots whose date has passed so state.json cannot grow forever."""
    cutoff = today.isoformat()
    state["notified"] = {
        key: seen_at
        for key, seen_at in state["notified"].items()
        # Keys are "YYYY-MM-DD HH:MM:SS", so a plain string compare works.
        if key[:10] >= cutoff
    }
    return state


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
    args = parser.parse_args()

    config = _load_config()

    if args.test_notification:
        import notify

        notify.send(
            f"Test — {config['venue_name']}",
            "If you can read this, APNs delivery is working.",
            collapse_id="test",
        )
        print("Test notification sent.")
        return 0

    openings = find_openings(config)
    print(
        f"Checked {config['venue_name']} "
        f"({config['date_start']} to {config['date_end']}, "
        f"party of {config['party_size']}): {len(openings)} bookable slot(s)."
    )
    for o in openings:
        print(f"  {o.date} ({o.shift}) — {o.time_label}")

    state = load_state()
    already = state["notified"]
    new = [o for o in openings if o.key() not in already]

    if not new:
        print("Nothing new to notify.")
        # Still prune + save so expired entries age out even on quiet runs.
        if not args.dry_run:
            save_state(prune_state(state, dt.date.today()))
        return 0

    print(f"{len(new)} newly appeared opening(s):")
    for o in new:
        print(f"  NEW  {o.date} — {o.time_label}")

    if args.dry_run:
        print("Dry run — no notification sent, state not saved.")
        return 0

    import notify

    title, body = format_alert(new, config)
    notify.send(
        title,
        body,
        collapse_id=config["venue_slug"],
        extra={"url": BOOKING_URL.format(slug=config["venue_slug"])},
    )
    print(f"Notified: {title} — {body}")

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for o in new:
        already[o.key()] = now
    save_state(prune_state(state, dt.date.today()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - surface a clean error in CI logs
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
