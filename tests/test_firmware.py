"""The firmware install: a fake keyboard takes a made-up image, and the ways it can go wrong are refused."""
import hashlib
import ssl
import struct
import urllib.request
import zlib

import pytest

from hm80 import firmware
from hm80.firmware import Flash, FlashError
from hm80.protocol import FirmwareRequest, Host, Profile, pkt

IMAGE = bytes(range(256)) * 9 + b'tail'                               # not the real image, which is never in this repo
beat = lambda fw, model=b'm14a': pkt(b'\x01' + model + b'\x00' + bytes(fw) + b'\x64' + b'\xff' * 12)
host_for = lambda flash: setattr(h := Host(Profile.factory()), 'flash', flash) or h


def serve(host):
    """Pull every chunk the way the keyboard does, returning the bytes it reassembled."""
    got = bytearray(len(IMAGE))
    for off in range(0, len(IMAGE), 24):
        n = min(24, len(IMAGE) - off)
        (reply,) = host.feed(pkt(b'\x15' + struct.pack('<I', off) + bytes([n])))
        assert reply[1:6] == struct.pack('<I', off) + bytes([n])
        got[off:off + n] = reply[6:6 + n]
    return bytes(got)


def test_a_keyboard_on_other_firmware_is_made_to_ask_then_takes_the_image():
    flash = Flash(IMAGE)
    out = host_for(flash).feed(beat((2, 5, 8)))                       # untested version, but the flash goes first
    assert [p[1:4] for p in out] == [bytes(firmware.VERSION)]         # a deliberate mismatch, nothing else
    host = host_for(flash)                                            # the keyboard restarts: a new connection
    (info,) = host.feed(pkt(b'\x14m14a'))
    assert info == pkt(b'\x14' + bytes(firmware.VERSION) + struct.pack('<II', len(IMAGE), zlib.crc32(IMAGE)))
    assert serve(host) == IMAGE and flash.sent_all and flash.percent == 100 and not flash.done
    host = host_for(flash)                                            # restarted again, now on the new firmware
    out = host.feed(beat(firmware.VERSION))
    assert flash.done and host.flash is None and out == [host.hello()] and host.firmware == '2.5.9'


def test_a_keyboard_already_asking_gets_the_image_without_a_heartbeat():
    flash = Flash(IMAGE)
    host = host_for(flash)
    assert host.feed(pkt(b'\x14m14a'))[0][1:4] == bytes(firmware.VERSION)
    assert serve(host) == IMAGE


def test_nothing_is_sent_to_another_model_or_one_that_does_not_take_it():
    for packet in (beat((2, 5, 8), b'zzzz'), pkt(b'\x14zzzz')):
        with pytest.raises(FlashError, match='not the HM80'):
            host_for(Flash(IMAGE)).feed(packet)
    flash = Flash(IMAGE)
    serve(host_for(flash))
    with pytest.raises(FlashError, match='came back on firmware 2.5.8'):      # the flash did not take
        host_for(flash).feed(beat((2, 5, 8)))
    with pytest.raises(FlashError, match='does not accept'):                  # it asks again after the whole image
        host_for(flash).feed(pkt(b'\x14m14a'))
    flash = Flash(IMAGE)
    host = host_for(flash)
    with pytest.raises(FlashError, match='does not accept'):                  # it asks and never pulls
        for _ in range(5):
            host.feed(pkt(b'\x14m14a'))


def test_without_a_flash_a_request_is_still_refused():
    with pytest.raises(FirmwareRequest):
        Host(Profile.factory()).feed(pkt(b'\x14m14a'))


def test_the_download_is_checked_before_anything_is_sent(monkeypatch):
    class Reply:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return self.data[:n]
    seen = []
    def urlopen(req, timeout, context):
        assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED     # never unchecked
        seen.append(req.get_header('Range'))
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply)
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(firmware, 'SIZE', len(IMAGE))
    monkeypatch.setattr(firmware, 'SHA256', hashlib.sha256(IMAGE).hexdigest())
    reply = IMAGE
    assert firmware.fetch() == IMAGE and seen == [f'bytes={firmware.OFFSET}-{firmware.OFFSET + len(IMAGE) - 1}']
    reply = IMAGE[:-1] + b'X'                                         # one byte differs
    with pytest.raises(FlashError, match='not the firmware'):
        firmware.fetch()
    reply = b'<html>the whole page, not a range</html>' * 100         # a server that ignores the range request
    with pytest.raises(FlashError, match='not the firmware'):
        firmware.fetch()
    reply = OSError('offline')
    with pytest.raises(FlashError, match='Could not download'):
        firmware.fetch()
