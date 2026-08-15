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


class APNsError(RuntimeError):
    """A push could not be delivered to a given device token."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise APNsError(f"Missing required environment variable {name}")
    return value


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
) -> list[str]:
    """Deliver one alert to every configured device.

    Returns the list of device tokens Apple rejected as permanently invalid, so
    the caller can report them; raises APNsError only for credential-level
    failures that affect every device.
    """
    # A "time-sensitive" interruption-level would break through Focus modes, but
    # it needs an extra entitlement on the App ID; add it once basic delivery works.
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if extra:
        payload.update(extra)

    headers = {
        "authorization": f"bearer {_auth_token()}",
        "apns-topic": _require_env("APNS_TOPIC"),
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + 3600),
    }
    if collapse_id:
        # APNs caps this at 64 bytes.
        headers["apns-collapse-id"] = collapse_id[:64]

    host = _host()
    invalid: list[str] = []
    with httpx.Client(http2=True, timeout=30) as client:
        for index, token in enumerate(device_tokens(), start=1):
            # Never log any part of a device token: this runs in a public repo,
            # and GitHub only masks *exact* matches of a secret's value, so even
            # a short prefix would show up verbatim in the build log. Identify
            # the device by its position in APNS_DEVICE_TOKENS instead.
            label = f"device #{index}"
            resp = client.post(
                f"{host}/3/device/{token}", json=payload, headers=headers
            )
            if resp.status_code == 200:
                continue

            reason = ""
            try:
                reason = resp.json().get("reason", "")
            except Exception:
                reason = resp.text[:200]

            # These mean the app was uninstalled or the token belongs to the
            # other APNs environment — the device is gone, not the credential.
            if reason in {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}:
                invalid.append(token)
                print(
                    f"  ! {label} rejected ({reason}); "
                    f"check APNS_ENV (currently {os.environ.get('APNS_ENV', 'sandbox')})"
                )
                continue

            raise APNsError(
                f"APNs returned {resp.status_code} ({reason or 'no reason'}) "
                f"for {label}"
            )
    return invalid
