import pytest

from nook import protocol as p


def test_bcc_is_xor():
    assert p.bcc(bytes([0x8A, 0x01, 0x11, 0x11])) == 0x8A ^ 0x01 ^ 0x11 ^ 0x11


def test_unlock_frame():
    assert p.unlock_frame(1, 17) == bytes([0x8A, 0x01, 0x11, 0x11, 0x8B])


def test_query_frame():
    f = p.query_frame(2)
    assert f[:4] == bytes([0x80, 0x02, 0x00, 0x33]) and p.check_bcc(f)


@pytest.mark.parametrize("addr,ch", [(0, 1), (1, 0), (1, 49), (128, 1)])
def test_frame_range_checks(addr, ch):
    with pytest.raises(ValueError):
        p.unlock_frame(addr, ch)


def test_query_roundtrip():
    doors = [False] * 48
    doors[0] = doors[16] = doors[47] = True
    assert p.parse_query_reply(p.query_reply(3, doors), 3) == doors


def test_unlock_reply_roundtrip():
    assert p.parse_unlock_reply(p.unlock_reply(1, 5, True), 1, 5) is True
    assert p.parse_unlock_reply(p.unlock_reply(1, 5, False), 1, 5) is False


def test_bad_replies_are_rejected():
    good = p.unlock_reply(1, 5, True)
    with pytest.raises(p.FrameError):
        p.parse_unlock_reply(good[:-1] + bytes([good[-1] ^ 1]), 1, 5)   # checksum
    with pytest.raises(p.FrameError):
        p.parse_unlock_reply(good, 1, 6)                                 # wrong channel
    with pytest.raises(p.FrameError):
        p.parse_query_reply(b"\x80\x01", 1)                              # too short
