"""Locker numbers -> board/channel, unlocking, and door polling."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import protocol as p
from .bus import Bus

log = logging.getLogger(__name__)


@dataclass
class UnlockResult:
    locker: int
    ok: bool                 # the board answered
    door_open: bool          # the board reports the door open after the pulse
    error: str | None = None


class LockController:
    def __init__(self, bus: Bus, lockers: int = 100,
                 locks_per_board: int = p.CHANNELS_PER_BOARD, first_address: int = 1):
        if not 1 <= locks_per_board <= p.CHANNELS_PER_BOARD:
            raise ValueError("locks_per_board must be 1-48")
        self.bus = bus
        self.lockers = lockers
        self.locks_per_board = locks_per_board
        self.first_address = first_address
        self.boards = (lockers + locks_per_board - 1) // locks_per_board

    def locate(self, locker: int) -> tuple[int, int]:
        if not 1 <= locker <= self.lockers:
            raise ValueError(f"no locker {locker}")
        board = (locker - 1) // self.locks_per_board
        return self.first_address + board, (locker - 1) % self.locks_per_board + 1

    def board_addresses(self) -> list[int]:
        return [self.first_address + i for i in range(self.boards)]

    def unlock(self, locker: int) -> UnlockResult:
        addr, ch = self.locate(locker)
        reply = self.bus.transact(p.unlock_frame(addr, ch), p.UNLOCK_REPLY_LEN)
        if reply is None:
            return UnlockResult(locker, False, False, f"board {addr} did not answer")
        try:
            return UnlockResult(locker, True, p.parse_unlock_reply(reply, addr, ch))
        except p.FrameError as exc:
            return UnlockResult(locker, False, False, str(exc))

    def read_board(self, addr: int) -> list[bool] | None:
        reply = self.bus.transact(p.query_frame(addr), p.QUERY_REPLY_LEN)
        if reply is None:
            return None
        try:
            return p.parse_query_reply(reply, addr)
        except p.FrameError as exc:
            log.warning("board %d: %s", addr, exc)
            return None

    def read_all(self) -> tuple[dict[int, bool], list[int]]:
        """Door state of every locker we could read, and the boards that failed."""
        doors: dict[int, bool] = {}
        failed: list[int] = []
        for i, addr in enumerate(self.board_addresses()):
            states = self.read_board(addr)
            if states is None:
                failed.append(addr)
                continue
            for ch in range(self.locks_per_board):
                locker = i * self.locks_per_board + ch + 1
                if locker <= self.lockers:
                    doors[locker] = states[ch]
        return doors, failed


DoorCallback = Callable[[int, bool], None]


class DoorWatcher(threading.Thread):
    """Polls the boards and reports every door that opens or closes."""

    def __init__(self, locks: LockController, on_change: DoorCallback,
                 on_left_open: DoorCallback | None = None,
                 on_board_fault: Callable[[list[int]], None] | None = None,
                 interval: float = 0.5, left_open_after: float = 120.0):
        super().__init__(name="door-watcher", daemon=True)
        self.locks = locks
        self.on_change = on_change
        self.on_left_open = on_left_open
        self.on_board_fault = on_board_fault
        self.interval = interval
        self.left_open_after = left_open_after
        self.doors: dict[int, bool] = {}
        self.failed_boards: list[int] = []
        self._opened_at: dict[int, float] = {}
        self._alarmed: set[int] = set()
        self._stop = threading.Event()

    def poll_once(self) -> None:
        doors, failed = self.locks.read_all()
        if failed != self.failed_boards:
            self.failed_boards = failed
            if self.on_board_fault:
                self.on_board_fault(failed)
        now = time.monotonic()
        first = not self.doors
        for locker, is_open in doors.items():
            before = self.doors.get(locker)
            self.doors[locker] = is_open
            if is_open and locker not in self._opened_at:
                self._opened_at[locker] = now
            if not is_open:
                self._opened_at.pop(locker, None)
                self._alarmed.discard(locker)
            if not first and before is not None and before != is_open:
                self.on_change(locker, is_open)
            if (is_open and self.on_left_open and locker not in self._alarmed
                    and now - self._opened_at[locker] >= self.left_open_after):
                self._alarmed.add(locker)
                self.on_left_open(locker, True)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:                       # never let polling die
                log.exception("door polling failed")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
