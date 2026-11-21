"""Kerong KR-CU48 lock board frames (RS485, 9600 8N1).

Every frame ends with a BCC byte: the XOR of all bytes before it.

    Unlock one lock   host -> board   8A <addr> <ch> 11 <bcc>
                      board -> host   8A <addr> <ch> <state> <bcc>
    Read all doors    host -> board   80 <addr> 00 33 <bcc>
                      board -> host   80 <addr> <6 bytes, 48 bits> 33 <bcc>

<addr> is the board address set on its DIP switches (1, 2, 3...), <ch> the
lock channel 1-48. In replies a set bit / state byte 0x00 means the door is
OPEN, 0x11 means CLOSED.

Check these against the datasheet that ships with your boards: reply layouts
differ between Kerong firmware versions (CU16 vs CU48). Everything that
depends on them lives in this file.
"""

from __future__ import annotations

UNLOCK_CMD = 0x8A
QUERY_CMD = 0x80
UNLOCK_FN = 0x11
QUERY_FN = 0x33

STATE_OPEN = 0x00
STATE_CLOSED = 0x11

CHANNELS_PER_BOARD = 48
STATUS_BYTES = CHANNELS_PER_BOARD // 8          # 6
UNLOCK_REPLY_LEN = 5
QUERY_REPLY_LEN = 2 + STATUS_BYTES + 2          # 10


class FrameError(ValueError):
    """A reply that is too short, has the wrong header or a bad checksum."""


def bcc(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def with_bcc(body: bytes) -> bytes:
    return body + bytes([bcc(body)])


def check_bcc(frame: bytes) -> bool:
    return len(frame) >= 2 and bcc(frame[:-1]) == frame[-1]


def _check_addr(addr: int) -> None:
    if not 1 <= addr <= 0x7F:
        raise ValueError(f"board address out of range: {addr}")


def _check_channel(channel: int) -> None:
    if not 1 <= channel <= CHANNELS_PER_BOARD:
        raise ValueError(f"channel out of range: {channel}")


def unlock_frame(addr: int, channel: int) -> bytes:
    _check_addr(addr)
    _check_channel(channel)
    return with_bcc(bytes([UNLOCK_CMD, addr, channel, UNLOCK_FN]))


def query_frame(addr: int) -> bytes:
    _check_addr(addr)
    return with_bcc(bytes([QUERY_CMD, addr, 0x00, QUERY_FN]))


def parse_unlock_reply(reply: bytes, addr: int, channel: int) -> bool:
    """Return True when the board reports the door open after the pulse."""
    if len(reply) != UNLOCK_REPLY_LEN:
        raise FrameError(f"unlock reply has {len(reply)} bytes, expected {UNLOCK_REPLY_LEN}")
    if not check_bcc(reply):
        raise FrameError("unlock reply: bad checksum")
    if reply[0] != UNLOCK_CMD or reply[1] != addr or reply[2] != channel:
        raise FrameError(f"unlock reply for the wrong lock: {reply.hex(' ')}")
    return reply[3] == STATE_OPEN


def parse_query_reply(reply: bytes, addr: int) -> list[bool]:
    """Return 48 booleans, index 0 = channel 1, True = door open."""
    if len(reply) != QUERY_REPLY_LEN:
        raise FrameError(f"status reply has {len(reply)} bytes, expected {QUERY_REPLY_LEN}")
    if not check_bcc(reply):
        raise FrameError("status reply: bad checksum")
    if reply[0] != QUERY_CMD or reply[1] != addr or reply[-2] != QUERY_FN:
        raise FrameError(f"status reply from the wrong board: {reply.hex(' ')}")
    bits = reply[2:2 + STATUS_BYTES]
    return [bool(bits[i // 8] >> (i % 8) & 1) for i in range(CHANNELS_PER_BOARD)]


# Board side (used by the simulator) ----------------------------------------

def unlock_reply(addr: int, channel: int, door_open: bool) -> bytes:
    state = STATE_OPEN if door_open else STATE_CLOSED
    return with_bcc(bytes([UNLOCK_CMD, addr, channel, state]))


def query_reply(addr: int, doors_open: list[bool]) -> bytes:
    bits = bytearray(STATUS_BYTES)
    for i, is_open in enumerate(doors_open[:CHANNELS_PER_BOARD]):
        if is_open:
            bits[i // 8] |= 1 << (i % 8)
    return with_bcc(bytes([QUERY_CMD, addr]) + bytes(bits) + bytes([QUERY_FN]))
