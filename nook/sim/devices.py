"""Simulated Kerong KR-CU48 boards that speak the real byte protocol.

SimTransport plugs into the Bus in place of a serial port (works everywhere,
Windows included). PtyBridge puts the same boards behind a virtual serial
port (Linux/macOS) so the real pyserial code path is exercised too.
"""

from __future__ import annotations

import os
import threading
import time

from .. import protocol as p
from ..power import PowerMonitor, PowerState

BYTE_TIME = 10 / 9600            # 8N1 at 9600 baud


class SimBoard:
    def __init__(self, addr: int, channels: int = p.CHANNELS_PER_BOARD):
        self.addr = addr
        self.doors = [False] * p.CHANNELS_PER_BOARD      # True = open
        self.jammed: set[int] = set()                     # channels whose door won't pop
        self.channels = channels
        self.fault = False                                # board stops answering

    def handle(self, frame: bytes) -> bytes | None:
        if self.fault or len(frame) != 5 or not p.check_bcc(frame) or frame[1] != self.addr:
            return None
        cmd, ch, fn = frame[0], frame[2], frame[3]
        if cmd == p.UNLOCK_CMD and fn == p.UNLOCK_FN and 1 <= ch <= p.CHANNELS_PER_BOARD:
            if ch <= self.channels and ch not in self.jammed:
                self.doors[ch - 1] = True                 # the spring pushes it open
            return p.unlock_reply(self.addr, ch, self.doors[ch - 1])
        if cmd == p.QUERY_CMD and fn == p.QUERY_FN:
            return p.query_reply(self.addr, self.doors)
        return None


class SimCabinet:
    def __init__(self, lockers: int = 100, locks_per_board: int = 48, first_address: int = 1,
                 auto_close: float = 0):
        self.lockers = lockers
        self.locks_per_board = locks_per_board
        self.boards: dict[int, SimBoard] = {}
        n = (lockers + locks_per_board - 1) // locks_per_board
        for i in range(n):
            used = min(locks_per_board, lockers - i * locks_per_board)
            self.boards[first_address + i] = SimBoard(first_address + i, used)
        self.first_address = first_address
        self.powered = True
        self.auto_close = auto_close
        self._opened_at: dict[int, float] = {}
        self._lock = threading.Lock()
        if auto_close:
            threading.Thread(target=self._closer, name="sim-auto-close", daemon=True).start()

    def _where(self, locker: int) -> tuple[SimBoard, int]:
        i = (locker - 1) // self.locks_per_board
        return self.boards[self.first_address + i], (locker - 1) % self.locks_per_board + 1

    def handle(self, frame: bytes) -> bytes | None:
        if not self.powered or len(frame) < 2:
            return None
        board = self.boards.get(frame[1])
        with self._lock:
            reply = board.handle(frame) if board else None
            if reply and frame[0] == p.UNLOCK_CMD:
                locker = (frame[1] - self.first_address) * self.locks_per_board + frame[2]
                self._opened_at.setdefault(locker, time.monotonic())
        return reply

    def is_open(self, locker: int) -> bool:
        board, ch = self._where(locker)
        return board.doors[ch - 1]

    def set_door(self, locker: int, is_open: bool) -> None:
        """A person closes (or forces open) a door."""
        board, ch = self._where(locker)
        with self._lock:
            board.doors[ch - 1] = is_open
            if is_open:
                self._opened_at[locker] = time.monotonic()
            else:
                self._opened_at.pop(locker, None)

    def set_jammed(self, locker: int, jammed: bool) -> None:
        board, ch = self._where(locker)
        (board.jammed.add if jammed else board.jammed.discard)(ch)

    def snapshot(self) -> list[dict]:
        out = []
        for n in range(1, self.lockers + 1):
            board, ch = self._where(n)
            out.append({"n": n, "open": board.doors[ch - 1], "jammed": ch in board.jammed,
                        "board": board.addr, "fault": board.fault})
        return out

    def _closer(self) -> None:
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            for locker, t in list(self._opened_at.items()):
                if now - t >= self.auto_close and self.is_open(locker):
                    self.set_door(locker, False)


class SimTransport:
    """Looks like pyserial.Serial to the Bus."""

    def __init__(self, cabinet: SimCabinet, realistic_timing: bool = True):
        self.cabinet = cabinet
        self.timing = realistic_timing
        self._rx = b""

    def reset_input_buffer(self) -> None:
        self._rx = b""

    def write(self, data: bytes) -> int:
        if self.timing:
            time.sleep(len(data) * BYTE_TIME)
        reply = self.cabinet.handle(bytes(data))
        if reply:
            self._rx += reply
        return len(data)

    def read(self, size: int) -> bytes:
        if not self._rx and self.timing:
            time.sleep(0.05)                 # what a serial timeout would cost
        if self.timing:
            time.sleep(min(size, len(self._rx)) * BYTE_TIME)
        out, self._rx = self._rx[:size], self._rx[size:]
        return out

    def close(self) -> None:
        pass


class PtyBridge(threading.Thread):
    """Serves the simulated boards on a pseudo-terminal; open .port with pyserial."""

    def __init__(self, cabinet: SimCabinet):
        super().__init__(name="sim-pty", daemon=True)
        import tty

        self.cabinet = cabinet
        self.master, slave = os.openpty()
        tty.setraw(slave)
        tty.setraw(self.master)
        self.port = os.ttyname(slave)
        self._slave = slave                   # keep it open so the pty stays alive

    def run(self) -> None:
        buf = b""
        while True:
            try:
                data = os.read(self.master, 64)
            except OSError:
                return
            buf += data
            while len(buf) >= 5:              # every host frame is 5 bytes
                frame, buf = buf[:5], buf[5:]
                if not p.check_bcc(frame):
                    buf = frame[1:] + buf     # resynchronise
                    continue
                reply = self.cabinet.handle(frame)
                if reply:
                    os.write(self.master, reply)


class SimPower(PowerMonitor):
    """Mains switch and a battery that drains / charges in fast-forward."""

    def __init__(self, cabinet: SimCabinet, drain_per_s: float = 0.5, charge_per_s: float = 2.0):
        self.cabinet = cabinet
        self.mains = True
        self.battery = 100.0
        self.drain, self.charge = drain_per_s, charge_per_s
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def set_mains(self, on: bool) -> None:
        with self._lock:
            self._tick()
            self.mains = on

    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        if self.mains:
            self.battery = min(100.0, self.battery + self.charge * dt)
        else:
            self.battery = max(0.0, self.battery - self.drain * dt)
        self.cabinet.powered = self.mains or self.battery > 0

    def read(self) -> PowerState:
        with self._lock:
            self._tick()
            return PowerState(self.mains, self.battery)
