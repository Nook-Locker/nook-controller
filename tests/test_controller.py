"""End-to-end: real controller + simulated boards + in-memory cloud."""

import json
import time
import uuid

import pytest

from nook import config
from nook.app import build_controller
from nook.link import LoopbackLink
from nook.security import issue_offline_token, sign_command
from nook.sim.devices import SimCabinet, SimPower, SimTransport

SECRET = b"0123456789abcdef0123456789abcdef"


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def rig(tmp_path):
    cfg = config.load(None)
    cfg.cabinet.id = "cab-test"
    cfg.storage.db = str(tmp_path / "j.db")
    cfg.doors.poll_interval = 0.05
    cfg.security.min_unlock_interval = 0
    cab = SimCabinet(100, 48, 1)
    power = SimPower(cab)
    link = LoopbackLink("cab-test")
    ctl = build_controller(cfg, SimTransport(cab, realistic_timing=False), link, power, SECRET)
    ctl.start()
    yield ctl, cab, link, power
    ctl.stop()


def topics(link, name):
    return [m["payload"] for m in link.sent if m["topic"].endswith(name)]


def open_cmd(locker):
    return sign_command(SECRET, "cab-test", locker, str(uuid.uuid4()), int(time.time()) + 30, "m-1")


def test_locate_spans_three_boards(rig):
    ctl = rig[0]
    assert ctl.locks.locate(1) == (1, 1)
    assert ctl.locks.locate(48) == (1, 48)
    assert ctl.locks.locate(49) == (2, 1)
    assert ctl.locks.locate(100) == (3, 4)


def test_app_unlock_opens_and_reports(rig):
    ctl, cab, link, _ = rig
    assert link.inject("open", open_cmd(73))
    assert cab.is_open(73)
    ev = topics(link, "event/unlock")[-1]
    assert ev["ok"] and ev["doorOpen"] and ev["locker"] == 73 and ev["source"] == "app"
    assert wait_for(lambda: {"locker": 73, "event": "opened"}.items()
                    <= (topics(link, "event/door") or [{}])[-1].items())
    cab.set_door(73, False)
    assert wait_for(lambda: topics(link, "event/door")[-1]["event"] == "closed")


def test_garbage_and_bad_commands_never_unlock(rig):
    ctl, cab, link, _ = rig
    link.inject("open", b"not json")
    bad = open_cmd(5)
    bad["sig"] = "00" * 32
    link.inject("open", bad)
    assert not any(l["open"] for l in cab.snapshot())
    assert [e["ok"] for e in topics(link, "event/unlock")] == [False, False]


def test_offline_journal_then_flush(rig):
    ctl, cab, link, _ = rig
    link.set_online(False)
    now = int(time.time())
    tok = issue_offline_token(SECRET, "cab-test", 12, "m-12", now, now + 3600, uuid.uuid4().hex)
    assert ctl.handle_offline_token(tok)["doorOpen"]
    assert wait_for(lambda: ctl.journal.pending_count() >= 2)      # unlock + door opened
    before = len(link.sent)
    link.set_online(True)
    assert ctl.journal.pending_count() == 0
    sent = [m["topic"] for m in list(link.sent)[before:]]
    assert sent[0].endswith("event/unlock") and sent[-1].endswith("status")


def test_forced_door_is_flagged(rig):
    ctl, cab, link, _ = rig
    cab.set_door(30, True)
    assert wait_for(lambda: topics(link, "event/door") and topics(link, "event/door")[-1]["event"] == "forced")


def test_board_fault_and_recovery(rig):
    ctl, cab, link, _ = rig
    cab.boards[2].fault = True
    assert wait_for(lambda: topics(link, "event/fault") and topics(link, "event/fault")[-1]["failedBoards"] == [2])
    res = ctl.handle_open(json.dumps(open_cmd(60)).encode())
    assert not res["ok"] and "did not answer" in res["reason"]
    cab.boards[2].fault = False
    assert wait_for(lambda: topics(link, "event/fault")[-1]["failedBoards"] == [])


def test_jammed_lock_reports_no_door_movement(rig):
    ctl, cab, link, _ = rig
    cab.set_jammed(9, True)
    res = ctl.handle_open(json.dumps(open_cmd(9)).encode())
    assert res["ok"] and not res["doorOpen"]


def test_power_events(rig):
    ctl, cab, link, power = rig
    power.set_mains(False)
    ctl.check_power()
    assert topics(link, "event/power")[-1]["event"] == "on_battery"
    power.battery = 10
    ctl.check_power()
    assert topics(link, "event/power")[-1]["event"] == "low_battery"
    power.set_mains(True)
    ctl.check_power()
    assert topics(link, "event/power")[-1]["event"] == "mains_restored"


def test_rate_limit(rig):
    ctl, cab, link, _ = rig
    ctl.cfg.security.min_unlock_interval = 5
    assert ctl.handle_open(json.dumps(open_cmd(3)).encode())["ok"]
    assert ctl.handle_open(json.dumps(open_cmd(3)).encode())["reason"] == "too many requests"


def test_sync_reports_all_doors(rig):
    ctl, cab, link, _ = rig
    link.inject("open", open_cmd(44))
    link.inject("sync", b"")
    doors = topics(link, "/doors")[-1]["doors"]
    assert len(doors) == 100 and doors["44"] is True
