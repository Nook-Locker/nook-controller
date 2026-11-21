import time

import pytest

from nook.journal import Journal
from nook.security import Verifier, issue_offline_token, sign_command

SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def v():
    j = Journal(":memory:")
    return Verifier(SECRET, "cab-001", 100, j.remember)


def cmd(locker=17, rid="req-00000001", ttl=30, cab="cab-001", secret=SECRET):
    return sign_command(secret, cab, locker, rid, int(time.time()) + ttl, "m-1")


def test_valid_command(v):
    r = v.command(cmd())
    assert r.ok and r.locker == 17 and r.member == "m-1"


def test_replay_refused(v):
    c = cmd()
    assert v.command(c).ok
    assert v.command(c).reason == "replayed"


def test_expired_refused(v):
    assert v.command(cmd(ttl=-60)).reason == "expired"


def test_far_future_refused(v):
    assert v.command(cmd(ttl=3600)).reason == "expiry too far ahead"


def test_wrong_secret_or_cabinet_refused(v):
    assert v.command(cmd(secret=b"x" * 32)).reason == "bad signature"
    assert v.command(cmd(cab="cab-002")).reason == "bad signature"


def test_tampered_locker_refused(v):
    c = cmd(locker=17)
    c["locker"] = 18
    assert v.command(c).reason == "bad signature"


@pytest.mark.parametrize("locker", [0, 101, -1, "17; rm", True, None, 1.5])
def test_bad_locker_refused(v, locker):
    c = cmd()
    c["locker"] = locker
    assert not v.command(c).ok


def test_offline_token(v):
    now = int(time.time())
    tok = issue_offline_token(SECRET, "cab-001", 42, "m-9", now - 10, now + 3600, "nonce-123456")
    r = v.offline_token(tok)
    assert r.ok and r.locker == 42 and r.member == "m-9"
    assert v.offline_token(tok).reason == "already used"


def test_offline_token_rules(v):
    now = int(time.time())
    assert v.offline_token(issue_offline_token(SECRET, "cab-002", 1, "m", now, now + 60, "n" * 12)).reason \
        == "token for another cabinet"
    assert v.offline_token(issue_offline_token(SECRET, "cab-001", 1, "m", now - 99, now - 60, "n" * 12)).reason \
        == "expired"
    assert v.offline_token(issue_offline_token(SECRET, "cab-001", 1, "m", now, now + 30 * 86400, "n" * 12)).reason \
        == "validity window too long"
    assert not v.offline_token("v1.abc.def").ok
    assert not v.offline_token("garbage").ok


def test_online_signature_is_not_a_token(v):
    # the offline key is derived, so an online-style HMAC must not verify
    import base64, hashlib, hmac, json
    payload = json.dumps({"cab": "cab-001", "locker": 1, "member": "m", "nbf": 0,
                          "exp": int(time.time()) + 60, "nonce": "n" * 12}).encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).digest()
    b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    assert v.offline_token(f"v1.{b(payload)}.{b(sig)}").reason == "bad signature"
