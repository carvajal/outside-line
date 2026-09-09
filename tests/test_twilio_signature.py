"""X-Twilio-Signature validation (`_twilio_signature_valid`).

Pure unit tests against a hand-computed vector: Twilio's scheme is
base64(HMAC-SHA1(auth_token, url + concat(k+v sorted by key))). The
endpoint-level skip-when-token-blank behavior is exercised implicitly
by every other webhook test in the suite (they all run with a blank
env and never see a 403).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from outside_line import twilio_handler as twh

_TOKEN = "12345678901234567890123456789012"
_URL = "https://example.com/twilio/voice"
_PARAMS = [("CallSid", "CA0000"), ("From", "+15005550006"), ("To", "+15005550009")]


def _sign(url: str, params: list[tuple[str, str]], token: str) -> str:
    payload = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(
        token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def test_valid_signature_accepted() -> None:
    sig = _sign(_URL, _PARAMS, _TOKEN)
    assert twh._twilio_signature_valid(sig, _URL, _PARAMS, _TOKEN)


def test_tampered_body_rejected() -> None:
    sig = _sign(_URL, _PARAMS, _TOKEN)
    tampered = [("CallSid", "CA0000"), ("From", "+19999999999")]
    assert not twh._twilio_signature_valid(sig, _URL, tampered, _TOKEN)


def test_wrong_url_rejected() -> None:
    sig = _sign(_URL, _PARAMS, _TOKEN)
    assert not twh._twilio_signature_valid(
        sig, "https://evil.example/twilio/voice", _PARAMS, _TOKEN
    )


def test_param_order_does_not_matter() -> None:
    """Twilio sorts by key server-side; our validator must too."""
    sig = _sign(_URL, _PARAMS, _TOKEN)
    shuffled = list(reversed(_PARAMS))
    assert twh._twilio_signature_valid(sig, _URL, shuffled, _TOKEN)
