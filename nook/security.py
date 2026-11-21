"""Signed commands (online) and offline tokens (Bluetooth).

Online command, sent by the cloud on cabinet/{id}/cmd/open:

    {"locker": 17, "requestId": "uuid", "exp": 1767225600, "member": "m-42",
     "sig": hex(HMAC-SHA256(secret, "{cabinet}|{locker}|{requestId}|{exp}"))}

Offline token, issued in advance by the cloud to the member's app and handed
to the cabinet over Bluetooth when the internet is down:

    v1.<base64url(json payload)>.<base64url(HMAC-SHA256(offline_key, payload))>
    payload = {"cab", "locker", "member", "nbf", "exp", "nonce"}

The offline key is derived from the cabinet secret, so a leaked online
signature cannot be replayed as a token and vice versa. Both kinds are single
use: their requestId / nonce is remembered until it expires.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable


def offline_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"nook-offline-v1", hashlib.sha256).digest()


def command_body(cabinet: str, locker: int, request_id: str, exp: int) -> bytes:
    return f"{cabinet}|{locker}|{request_id}|{exp}".encode()


def sign_command(secret: bytes, cabinet: str, locker: int, request_id: str,
                 exp: int, member: str | None = None) -> dict:
    """Cloud side. Kept here so tests and the simulator share one definition."""
    sig = hmac.new(secret, command_body(cabinet, locker, request_id, exp), hashlib.sha256)
    cmd = {"locker": locker, "requestId": request_id, "exp": exp, "sig": sig.hexdigest()}
    if member:
        cmd["member"] = member
    return cmd


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_offline_token(secret: bytes, cabinet: str, locker: int, member: str,
                        nbf: int, exp: int, nonce: str) -> str:
    """Cloud side: a token the app stores and sends over Bluetooth later."""
    payload = json.dumps({"cab": cabinet, "locker": locker, "member": member,
                          "nbf": nbf, "exp": exp, "nonce": nonce},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(offline_key(secret), payload, hashlib.sha256).digest()
    return f"v1.{_b64e(payload)}.{_b64e(sig)}"


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    locker: int = 0
    member: str | None = None
    ref: str = ""             # requestId or nonce


SeenFn = Callable[[str, int], bool]   # (id, expires_at) -> True if new


class Verifier:
    def __init__(self, secret: bytes, cabinet: str, lockers: int, seen: SeenFn,
                 max_ttl: int = 60, skew: int = 5, max_offline_days: int = 7,
                 clock: Callable[[], float] = time.time):
        if len(secret) < 16:
            raise ValueError("cabinet secret must be at least 16 bytes")
        self.secret = secret
        self.okey = offline_key(secret)
        self.cabinet = cabinet
        self.lockers = lockers
        self.seen = seen
        self.max_ttl = max_ttl
        self.skew = skew
        self.max_offline = max_offline_days * 86400
        self.clock = clock

    def _locker(self, value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= self.lockers and str(n) == str(value).strip() else None

    def command(self, cmd: dict) -> Verdict:
        if not isinstance(cmd, dict):
            return Verdict(False, "not an object")
        locker = self._locker(cmd.get("locker"))
        rid, exp, sig = cmd.get("requestId"), cmd.get("exp"), cmd.get("sig")
        if locker is None:
            return Verdict(False, "bad locker")
        if not isinstance(rid, str) or not 8 <= len(rid) <= 64:
            return Verdict(False, "bad requestId")
        if not isinstance(exp, int) or isinstance(exp, bool):
            return Verdict(False, "bad exp")
        if not isinstance(sig, str):
            return Verdict(False, "missing sig")
        good = hmac.new(self.secret, command_body(self.cabinet, locker, rid, exp),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig.lower()):
            return Verdict(False, "bad signature", locker)
        now = self.clock()
        if exp < now - self.skew:
            return Verdict(False, "expired", locker)
        if exp > now + self.max_ttl + self.skew:
            return Verdict(False, "expiry too far ahead", locker)
        if not self.seen(f"cmd:{rid}", exp + self.skew):
            return Verdict(False, "replayed", locker)
        member = cmd.get("member") if isinstance(cmd.get("member"), str) else None
        return Verdict(True, "", locker, member, rid)

    def offline_token(self, token: str) -> Verdict:
        if not isinstance(token, str) or len(token) > 1024:
            return Verdict(False, "bad token")
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return Verdict(False, "bad token format")
        try:
            payload_raw, sig = _b64d(parts[1]), _b64d(parts[2])
        except (ValueError, TypeError):
            return Verdict(False, "bad token encoding")
        good = hmac.new(self.okey, payload_raw, hashlib.sha256).digest()
        if not hmac.compare_digest(good, sig):
            return Verdict(False, "bad signature")
        try:
            data = json.loads(payload_raw)
        except ValueError:
            return Verdict(False, "bad payload")
        if data.get("cab") != self.cabinet:
            return Verdict(False, "token for another cabinet")
        locker = self._locker(data.get("locker"))
        nbf, exp, nonce = data.get("nbf"), data.get("exp"), data.get("nonce")
        if locker is None or not isinstance(nbf, int) or not isinstance(exp, int) \
                or not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
            return Verdict(False, "bad payload")
        now = self.clock()
        if now < nbf - self.skew:
            return Verdict(False, "not valid yet", locker)
        if now > exp + self.skew:
            return Verdict(False, "expired", locker)
        if exp - nbf > self.max_offline:
            return Verdict(False, "validity window too long", locker)
        if not self.seen(f"ble:{nonce}", exp + self.skew):
            return Verdict(False, "already used", locker)
        member = data.get("member") if isinstance(data.get("member"), str) else None
        return Verdict(True, "", locker, member, nonce)
