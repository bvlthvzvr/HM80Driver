"""Install firmware 2.5.9 on the keyboard, taking the image from the vendor's own installer.

The image sits unpacked inside hm-mag-v2.5.9.exe, so a range request fetches just those bytes. Nothing is stored in this
repository. The bytes are checked against SHA256 before a single packet goes to the keyboard. The packets are the ones the
official app sends (docs/PROTOCOL.md, "Firmware update"). Worked on real hardware in both directions."""
import hashlib
import os
import ssl
import struct
import urllib.request
import zlib

from .protocol import pkt

URL = 'https://vip.123pan.cn/1835998201/download/hm-mag-v2.5.9.exe'
OFFSET, SIZE = 430461, 90624                    # where the image sits in that installer
SHA256 = '45f7cbaab6f44bed8fabd9ef97de05639098c372493bc9da7822b084b0fd5880'
VERSION, MODEL = (2, 5, 9), b'm14a'
CA_FILES = ('/etc/ssl/certs/ca-certificates.crt', '/etc/pki/tls/certs/ca-bundle.crt', '/etc/ssl/ca-bundle.pem',
            '/etc/ssl/cert.pem')                # where the distributions keep the certificate authorities


class FlashError(Exception):
    """Something stopped the flash. The message says what, in words for the user."""


def fetch():
    """The image, or FlashError. Blocks for a second or two."""
    req = urllib.request.Request(URL, headers={'Range': f'bytes={OFFSET}-{OFFSET + SIZE - 1}'})
    # The Python inside the AppImage looks for the certificate authorities in a folder that only exists on the machine
    # that built it, so name the system's own file. Certificates are still checked.
    context = ssl.create_default_context(cafile=next((f for f in CA_FILES if os.path.exists(f)), None))
    try:
        with urllib.request.urlopen(req, timeout=20, context=context) as r:
            data = r.read(SIZE + 1)
    except OSError as e:
        raise FlashError(f"Could not download the firmware ({getattr(e, 'reason', e)}). Check the internet connection.")
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise FlashError('The download is not the firmware this app expects, so nothing was sent to the keyboard.')
    return data


class Flash:
    """Answers the keyboard like the official app while it takes the image. Nothing else is ever sent to it."""

    def __init__(self, image):
        self.image, self.crc = image, zlib.crc32(image) & 0xFFFFFFFF
        self.served, self.requests = set(), 0
        self.sent_all = self.done = False

    @property
    def percent(self):
        return 100 if self.sent_all else max(self.served, default=0) * 100 // len(self.image)

    def _check_model(self, model):
        if bytes(model) != MODEL:
            raise FlashError(f'This keyboard reports the model {bytes(model).decode("ascii", "replace")!r}, not the '
                             'HM80, so nothing was sent to it.')

    def feed(self, p, hello):
        """The replies to one packet from the keyboard: a heartbeat (0x01), an image request (0x14) or a chunk (0x15).
        `hello(version)` builds the heartbeat reply. Sets `done` once the keyboard is back on the new firmware."""
        if p[0] == 0x01:
            fw = tuple(p[6:9])
            if fw == VERSION:                       # it has the new firmware (just flashed, or it had it all along)
                self.done = True
                return []
            if self.sent_all:
                raise FlashError('The keyboard came back on firmware ' + '.'.join(map(str, fw)) +
                                 ', so the new firmware was not installed.')
            self._check_model(p[1:5])
            return [hello(VERSION)]                 # a deliberate mismatch: this is what makes it ask for the image
        if p[0] == 0x14:
            self._check_model(p[1:5])
            self.requests += 1
            if self.sent_all or (self.requests > 4 and not self.served):
                raise FlashError('The keyboard keeps asking for the firmware and does not accept it.')
            return [pkt(b'\x14' + bytes(VERSION) + struct.pack('<II', len(self.image), self.crc))]
        off, n = struct.unpack_from('<I', p, 1)[0], p[5]
        if n > 25 or off >= len(self.image):
            return []
        self.served.add(off)
        self.sent_all = self.sent_all or off + n >= len(self.image)
        return [pkt(b'\x15' + bytes(p[1:6]) + self.image[off:off + n])]
