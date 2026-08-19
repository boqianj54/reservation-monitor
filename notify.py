"""Push notification delivery via Apple Push Notification service (APNs).

Uses token-based authentication (a .p8 auth key + Key ID + Team ID) rather than
certificates, so the credential never expires and one key covers every app under
the BEspark team.

Required environment variables:
  APNS_KEY_P8     contents of the AuthKey_XXXXXXXXXX.p8 file (the PEM text)
  APNS_KEY_ID     10-char Key ID shown when the key was created
  APNS_TEAM_ID    Apple Developer Team ID
  APNS_TOPIC      the app's bundle identifier
  APNS_DEVICE_TOKENS  comma-separated APNs device token(s) from the iOS app
  APNS_ENV        "sandbox" (default, for builds installed from Xcode) or
                  "production" (TestFlight / App Store builds)
"""
from __future__ import annotations

import os
import time

import httpx
import jwt

SANDBOX_HOST = "https://api.sandbox.push.apple.com"
PRODUCTION_HOST = "https://api.push.apple.com"

# Apple rejects tokens older than 1 hour and rate-limits regeneration, so we
# reuse a single JWT for the life of the process and refresh well before expiry.
_TOKEN_TTL_SECONDS = 45 * 60

_cached_jwt: tuple[str, float] | None = None


# The device is permanently gone (app uninstalled, or the token belongs to the
# other APNs environment). Retrying never helps; the token must be replaced.
PERMANENT_DEVICE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
# Transient conditions worth one immediate retry.
RETRYABLE_REASONS = {
    "ExpiredProviderToken",
    "TooManyRequests",
    "TooManyProviderTokenUpdates",
    "ServiceUnavailable",
    "InternalServerError",
}


class APNsError(RuntimeError):
    """A push could not be delivered to a given device token."""


def _post(client, url: str, payload: dict, headers: dict) -> tuple[int, str]:
    """POST one notification, returning (status_code, apns_reason)."""
    resp = client.post(url, json=payload, headers=headers)
    if resp.status_code == 200:
        return 200, ""
    try:
        reason = resp.json().get("reason", "")
    except Exception:
        reason = resp.text[:200]
    return resp.status_code, reason


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise APNsError(f"Missing required environment variable {name}")
    return value


def _invalidate_auth_token() -> None:
    """Force the next _auth_token() call to mint a fresh JWT."""
    global _cached_jwt
    _cached_jwt = None


def _auth_token() -> str:
    global _cached_jwt
    now = time.time()
    if _cached_jwt and now - _cached_jwt[1] < _TOKEN_TTL_SECONDS:
        return _cached_jwt[0]

    key_p8 = _require_env("APNS_KEY_P8")
    # GitHub secrets often round-trip newlines as literal backslash-n.
    key_p8 = key_p8.replace("\\n", "\n")
    token = jwt.encode(
        {"iss": _require_env("APNS_TEAM_ID"), "iat": int(now)},
        key_p8,
        algorithm="ES256",
        headers={"kid": _require_env("APNS_KEY_ID")},
    )
    _cached_jwt = (token, now)
    return token


def device_tokens() -> list[str]:
    raw = _require_env("APNS_DEVICE_TOKENS")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _host() -> str:
    env = os.environ.get("APNS_ENV", "sandbox").strip().lower()
    if env == "production":
        return PRODUCTION_HOST
    if env == "sandbox":
        return SANDBOX_HOST
    raise APNsError(f"APNS_ENV must be 'sandbox' or 'production', got {env!r}")


def send(
    title: str,
    body: str,
    *,
    collapse_id: str | None = None,
    extra: dict | None = None,
    expiration_seconds: int = 24 * 3600,
) -> list[str]:
    """Deliver one alert to every configured device.

    Returns the list of device tokens Apple rejected as permanently invalid, so
    the caller can report them; raises APNsError only for credential-level
    failures that affect every device.

    `collapse_id` makes APNs *replace* any earlier undelivered alert carrying the
    same id. Pass None (the default) when each alert lists different openings,
    otherwise a second alert silently overwrites the first one the user never saw.
    """
    # "time-sensitive" breaks through Focus and Do Not Disturb, which is the
    # whole point here: a table can vanish in minutes, so a muted alert is as
    # good as no alert. Requires the matching entitlement in the iOS app.
    payload: dict = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "interruption-level": "time-sensitive",
        }
    }
    if extra:
        # Merge alongside "aps" rather than over it, so caller data can never
        # clobber the alert itself.
        for key, value in extra.items():
            if key != "aps":
                payload[key] = value

    headers = {
        "authorization": f"bearer {_auth_token()}",
        "apns-topic": _require_env("APNS_TOPIC"),
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + expiration_seconds),
    }
    if collapse_id:
        # APNs caps this at 64 bytes.
        headers["apns-collapse-id"] = collapse_id[:64]

    host = _host()
    invalid: list[str] = []
    errors: list[str] = []
    with httpx.Client(http2=True, timeout=30) as client:
        for index, token in enumerate(device_tokens(), start=1):
            # Never log any part of a device token: this runs in a public repo,
            # and GitHub only masks *exact* matches of a secret's value, so even
            # a short prefix would show up verbatim in the build log. Identify
            # the device by its position in APNS_DEVICE_TOKENS instead.
            label = f"device #{index}"
            url = f"{host}/3/device/{token}"

            status, reason = _post(client, url, payload, headers)
            if status != 200 and reason in RETRYABLE_REASONS:
                if reason == "ExpiredProviderToken":
                    _invalidate_auth_token()
                    headers["authorization"] = f"bearer {_auth_token()}"
                print(f"  … {label}: {reason}, retrying once")
                time.sleep(2)
                status, reason = _post(client, url, payload, headers)

            if status == 200:
                continue

            # These mean the app was uninstalled or the token belongs to the
            # other APNs environment — the device is gone, not the credential.
            if reason in PERMANENT_DEVICE_REASONS:
                invalid.append(token)
                print(
                    f"  ! {label} rejected ({reason}); "
                    f"check APNS_ENV (currently {os.environ.get('APNS_ENV', 'sandbox')})"
                )
                continue

            # Keep going so one broken device can't stop delivery to the others;
            # report everything that went wrong once the loop finishes.
            errors.append(f"{label}: HTTP {status} ({reason or 'no reason'})")

    if errors:
        raise APNsError("APNs delivery failed — " + "; ".join(errors))
    return invalid
