"""RS485 bus access: one request at a time, with timeouts and retries."""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Transport(Protocol):
    """The subset of pyserial.Serial the bus uses (the simulator mimics it)."""

    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


class Bus:
    def __init__(self, transport: Transport, retries: int = 2, gap: float = 0.02):
        self._t = transport
        self._lock = threading.Lock()
        self.retries = retries
        self.gap = gap                    # silence between frames on the line
        self.errors = 0

    def transact(self, frame: bytes, reply_len: int) -> bytes | None:
        """Send a frame and read a fixed-length reply. None if no answer."""
        with self._lock:
            for attempt in range(self.retries + 1):
                try:
                    self._t.reset_input_buffer()
                    self._t.write(frame)
                    reply = self._t.read(reply_len)
                except OSError as exc:
                    log.warning("bus I/O error: %s", exc)
                    reply = b""
                time.sleep(self.gap)
                if len(reply) == reply_len:
                    return reply
                self.errors += 1
                log.debug("no/short reply to %s (attempt %d)", frame.hex(" "), attempt + 1)
            return None

    def close(self) -> None:
        self._t.close()


def open_serial(port: str, baudrate: int = 9600, timeout: float = 0.3) -> Transport:
    import serial  # pyserial, only needed with real hardware

    return serial.Serial(port, baudrate=baudrate, bytesize=8, parity="N",
                         stopbits=1, timeout=timeout)
