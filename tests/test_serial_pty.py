"""The real pyserial path, against simulated boards on a virtual serial port."""

import sys

import pytest

pytest.importorskip("serial")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a pty (Linux/macOS)")


def test_unlock_and_poll_over_pty():
    from nook.bus import Bus, open_serial
    from nook.locks import LockController
    from nook.sim.devices import PtyBridge, SimCabinet

    cab = SimCabinet(100, 48, 1)
    bridge = PtyBridge(cab)
    bridge.start()
    locks = LockController(Bus(open_serial(bridge.port, timeout=0.3)), 100, 48, 1)
    res = locks.unlock(99)
    assert res.ok and res.door_open
    doors, failed = locks.read_all()
    assert failed == [] and doors[99] is True and sum(doors.values()) == 1
