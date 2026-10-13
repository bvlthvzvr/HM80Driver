"""HM80 (SikaKeyb / HEMU "m14a") configuration protocol: profiles, packets and the host state machine.

The keyboard talks over its CDC-ACM serial port in 32-byte packets: [cmd][30 bytes][sum(bytes 0..30) & 0xFF].
The host owns the per-key table: it answers the keyboard's 0x01 heartbeat with a CRC-32 per layer, and the
keyboard pulls records that differ with 0x18. Lighting (0x16) and global settings (0x17) are pushed by the
keyboard on connect and written back by the host. See docs/PROTOCOL.md.
"""
import base64
import os
import struct
import time
import zlib
from collections import deque

VID, PID = 0xC019, 0x1234
LAYERS, SLOTS, REC, NKEYS = 6, 110, 18, 84
SIZE = 0xBDF4                                   # host state struct, stored qCompress'd in .hmc files
OFF_NAMES, NAME_LEN = 0x16, 0x3D
OFF_G17, OFF_L16, OFF_KEYS = 0x137D, 0x1392, 0x13B0
OFF_CUSTOM, CUSTOM_LAYER, CUSTOM_FRAMES = 0x4218, 0x14A4, 16    # custom per-key lighting, per layer
FIELDS = {'travel': (0, '<H'), 'init': (2, '<B'), 'press': (3, '<H'), 'release': (5, '<H'), 'mode': (7, '<B'),
          'action': (8, '3s'), 'action2': (11, '3s'), 'count': (14, '<H'), 'time': (16, '<H')}
DEBUG_LOG = os.environ.get('HM80_DEBUG_PACKETS')   # file path: log every packet that differs from the last with its command
_last_seen = {}


def _debug_log(p):
    if _last_seen.get(p[0]) != p:
        _last_seen[p[0]] = p
        with open(DEBUG_LOG, 'a') as f:
            f.write(f'{time.time():.3f} {p.hex(" ")}\n')


JITTER = 0x8000                                 # in 'time': the firmware adds 1-5 ms at random to each wait
SW_VERSION = b'\x02\x05\x09'                    # what we report until the keyboard has said which firmware it has
# Firmware versions whose images (from the official drivers) are the same keyboard code as 2.5.9 apart from the version
# digits and small details. The keyboard asks for a firmware update when the version in our hello differs from its
# own, so we echo its version instead. 2.5.8 is a different build and is left out. See docs/PROTOCOL.md.
KNOWN_FIRMWARE = {(2, 5, 3), (2, 5, 4), (2, 5, 5), (2, 5, 6), (2, 5, 7), (2, 5, 9)}
LAYER_FORMAT, LAYER_VERSION = 'hm80-layer', 1    # marks the files made by export_layer

BACKLIGHT_EFFECTS = ['Off', 'Custom', 'Static', 'Breathe', 'Cycle', 'Cycle Left/Right', 'Cycle Up/Down',
                     'Cycle Out/In', 'Rainbow Moving', 'Pinwheel', 'Spiral', 'Rainbow Beacon',
                     'Reactive Simple', 'Reactive Wide', 'Reactive Nexus', 'Splash']
LIGHTBAR_EFFECTS = ['Off', 'Static', 'Breathe', 'Cycle', 'Cycle Left', 'Cycle Right', 'Gravity Pendulum',
                    'Key Depth']

# Physical layout, matching the HM80: rows of (key index, label, width in key units, gap before).
# Main block is 15u wide; the nav column starts 0.3u to its right. Space has three sensors (2.25 / 1.75 / 2.25).
LAYOUT = [
    [(0, 'Esc', 1, 0)] + [(i, f'F{i}', 1, 0.25 if i in (1, 5, 9) else 0) for i in range(1, 13)]
    + [(13, 'Prtsc', 1, 0.25)],
    [(14, '`', 1, 0)] + [(15 + i, '1234567890'[i], 1, 0) for i in range(10)]
    + [(25, '-', 1, 0), (26, '=', 1, 0), (27, 'Backspace', 2, 0), (28, 'Insert', 1, 0.3), (29, 'Pg Up', 1, 0)],
    [(30, 'Tab', 1.5, 0)] + [(31 + i, c, 1, 0) for i, c in enumerate('QWERTYUIOP[]')]
    + [(43, '\\', 1.5, 0), (44, 'Delete', 1, 0.3), (45, 'Pg Dn', 1, 0)],
    [(46, 'Caps Lock', 1.75, 0)] + [(47 + i, c, 1, 0) for i, c in enumerate("ASDFGHJKL;'")]
    + [(58, 'Enter', 2.25, 0)],
    [(59, 'Shift', 2.25, 0)] + [(60 + i, c, 1, 0) for i, c in enumerate('ZXCVBNM,./')]
    + [(70, 'Shift', 2.75, 0), (71, '↑', 1, 0.3)],
    [(72, 'Ctrl', 1.25, 0), (73, 'Win', 1.25, 0), (74, 'Alt', 1.25, 0), (75, 'Space', 2.25, 0),
     (76, 'Space', 1.75, 0), (77, 'Space', 2.25, 0), (78, 'Alt', 1.25, 0), (79, 'Fn', 1.25, 0),
     (80, 'Ctrl', 1.25, 0), (81, '←', 1, 0.55), (82, '↓', 1, 0), (83, '→', 1, 0)],
]
KEY_LABELS = {i: label for row in LAYOUT for i, label, _, _ in row}

# What a key does: [type][code][extra] (record bytes 8..10). Codes and names follow the official app's own tables;
# extra only matters for MIDI and is 0xFF otherwise. Code 0xFF means the key does nothing on that layer.
KEYCODES = {
    0x04: 'A', 0x05: 'B', 0x06: 'C', 0x07: 'D', 0x08: 'E', 0x09: 'F', 0x0a: 'G', 0x0b: 'H', 0x0c: 'I', 0x0d: 'J',
    0x0e: 'K', 0x0f: 'L', 0x10: 'M', 0x11: 'N', 0x12: 'O', 0x13: 'P', 0x14: 'Q', 0x15: 'R', 0x16: 'S', 0x17: 'T',
    0x18: 'U', 0x19: 'V', 0x1a: 'W', 0x1b: 'X', 0x1c: 'Y', 0x1d: 'Z', 0x1e: '1', 0x1f: '2', 0x20: '3', 0x21: '4',
    0x22: '5', 0x23: '6', 0x24: '7', 0x25: '8', 0x26: '9', 0x27: '0', 0x28: 'Enter', 0x29: 'Esc',
    0x2a: 'Backspace', 0x2b: 'Tab', 0x2c: 'Space', 0x2d: '-', 0x2e: '=', 0x2f: '[', 0x30: ']', 0x31: '\\',
    0x33: ';', 0x34: "'", 0x35: '`', 0x36: ',', 0x37: '.', 0x38: '/', 0x39: 'Caps', 0x3a: 'F1', 0x3b: 'F2',
    0x3c: 'F3', 0x3d: 'F4', 0x3e: 'F5', 0x3f: 'F6', 0x40: 'F7', 0x41: 'F8', 0x42: 'F9', 0x43: 'F10', 0x44: 'F11',
    0x45: 'F12', 0x46: 'PrtSc', 0x47: 'ScrLk', 0x48: 'Pause', 0x49: 'Insert', 0x4a: 'Home', 0x4b: 'PgUp',
    0x4c: 'Delete', 0x4d: 'End', 0x4e: 'PgDn', 0x4f: '→', 0x50: '←', 0x51: '↓', 0x52: '↑', 0x53: 'NumLk',
    0x54: 'Pad/', 0x55: 'Pad*', 0x56: 'Pad-', 0x57: 'Pad+', 0x58: 'PadEnter', 0x59: 'Pad1', 0x5a: 'Pad2',
    0x5b: 'Pad3', 0x5c: 'Pad4', 0x5d: 'Pad5', 0x5e: 'Pad6', 0x5f: 'Pad7', 0x60: 'Pad8', 0x61: 'Pad9', 0x62: 'Pad0',
    0x63: 'Pad.', 0x74: 'ImeOn', 0x75: 'ImeOff', 0x76: 'Menu', 0x80: 'Ctrl', 0x81: 'Shift', 0x82: 'Alt',
    0x83: 'Win', 0x84: 'Ctrl', 0x85: 'Shift', 0x86: 'Alt', 0x87: 'Win'}
MEDIA = {1: 'Vol−', 2: 'Vol+', 3: 'Prev', 4: 'Next', 5: 'Play', 6: 'Mute', 7: 'Stop'}
ACTIONS = {
    0: KEYCODES,
    1: {1: 'L click', 2: 'M click', 3: 'R click', 4: 'Wheel ↑', 5: 'Wheel ↓'},
    2: MEDIA,
    3: {1: 'Mode−', 2: 'Mode+', 3: 'Speed−', 4: 'Speed+', 5: 'Hue−', 6: 'Hue+', 7: 'Sat−', 8: 'Sat+', 9: 'Bri−',
        10: 'Bri+', 11: 'Light', 12: 'LED test', 13: 'Flash', 14: 'Bar−', 15: 'Bar+'},
    4: {0: 'Layer−', 1: 'Layer+', **{2 + i: f'L{i + 1}' for i in range(5)}, 7: 'Fn layer',
        **{0x0c + i: f'Temp L{i + 1}' for i in range(5)}, 0x11: 'Fn', 0x16: 'Win lock'},
    5: {i: f'Open {i + 1}' for i in range(4)},
    7: {0: 'Page−', 1: 'Page+'},
}


# What the app offers to put on a key besides the media functions and PrtSc: (title, action type, codes). It is
# everything the factory Fn layer uses, plus the rest of the same kind (layers L4 and L5, the other navigation keys).
# Hue−/Hue+ and Flash are in the official app's table but not in the factory layout, and are left out.
FUNCTION_GROUPS = (
    ('Lighting', 3, (1, 2, 3, 4, 9, 10, 7, 8, 14, 15, 11, 12)),
    ('Layers', 4, (2, 3, 4, 5, 6, 0x16)),
    ('Navigation', 0, (0x49, 0x4C, 0x4A, 0x4D, 0x4B, 0x4E, 0x47, 0x48)),
    ('Screen', 7, (0, 1)),
)


def action_label(action):
    """Short legend for an action: '' if the key does nothing, '?' if the official app has no name for it either."""
    kind, code = action[0], action[1]
    if code == 0xFF and kind != 6:
        return ''
    return 'MIDI' if kind == 6 else ACTIONS.get(kind, {}).get(code, '?')


# Factory profile for the HM80, as shipped inside the official hm-mag 2.5.9 app (/res/device/template.m14a.hmc).
# A keyboard that was never customized matches it, so starting from it changes nothing on the keyboard.
FACTORY_HMC = base64.b64decode(
    'AAC99Hja7dzLThNRHIDxM3S0tCB4rXfFK94VAQVveC2KqOjKrYm6dEFCojuolDQxIb6FiXHZh+hbjY0Rpf02TWscrN+vCYsvZ4bO'
    'acM/OQsyIYS3819eRiEKv82++vBmfiS058fFlzu5eLSTi8c6uXi8zYuL74IkSZIkSZIE5SgkyVBPss7C1+RTZbqalMqT1WR1daGa'
    'zL1e+1GpzEShVn+FcDqpX5kP+fCrXEO5jnID5SbKLZQplNsod1DuotxDuY/yAKWIMo5yCOUwyhDKEZSjKMdQjqOcQDmJMoxyHuUC'
    'yhmURyiPUc6i7ETZh9KLsgtlN8oBlD0o/SjbUXagXES5hDKCMovyFGUSJU5CtrEUsCab1Jc1lBzW5FH6ULagDKAMooyijKGcQllC'
    'OYiyH2Uzyl6UTSjbULaiXEG5ijKBUkZ5gbKI8hGlhHKurbLSXGI++zLKHMpzlGdr5fNaSSzdUpzdzu7OZncuChlnd9NurJvdP0sB'
    'a5zdzm5nt8XZ7exOb3ZHzu6m3YhRCihZ3MfZ7ex2dluc3c5uz8xTPDNPWjgzd3Y7u53dFme3s9vZ7ex2dju7nd0Wy79TMhFKD0o/'
    '7jPTXLJhgz1pEWUa5WEL+5NF6UXpw32eYH+irvi2DKAMtnCfpeYS4zsWZ1DiDbYbZTx7HmsW8RQFrCn9ofezgt/FNct4z9jnTA4l'
    '9i9k95bQ8P8gJEmSJEmSJOlv84xSkiRJkiRJUpo8o5QkSZIkSZKUJs8oJUmSJEmSJKXJM0pJkiRJkiRJafqPzyhrtZqfvyRJkiRJ'
    'kpSubxPvh78DKJw8Hg==')


class Profile:
    """The host-side state struct (same .hmc format as the Windows app, so profiles move both ways)."""

    def __init__(self, hmc_bytes):
        n = struct.unpack('>I', hmc_bytes[:4])[0]   # qCompress header: big-endian uncompressed size
        self.b = bytearray(zlib.decompress(hmc_bytes[4:]))
        if n != SIZE or len(self.b) != SIZE or self.b[0] != 3:
            raise ValueError('not an HM80 (m14) profile')

    @classmethod
    def load(cls, path):
        with open(path, 'rb') as f:
            return cls(f.read())

    @classmethod
    def factory(cls):
        return cls(FACTORY_HMC)

    def to_hmc(self):
        return struct.pack('>I', SIZE) + zlib.compress(bytes(self.b))

    def copy(self):
        return Profile(self.to_hmc())

    def same_settings(self, other):
        """True if both hold the same settings. Where the keyboard is (the active layer) is not a setting, so it is
        left out: moving between layers changes nothing."""
        a, b = bytearray(self.b), bytearray(other.b)
        a[OFF_G17] = b[OFF_G17] = 0
        return a == b

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(self.to_hmc())
        os.replace(tmp, path)                        # atomic: a crash never leaves a half-written profile

    def _off(self, layer, key):
        return OFF_KEYS + (layer * SLOTS + key) * REC

    def record(self, layer, key):
        o = self._off(layer, key)
        return bytes(self.b[o:o + REC])

    def get(self, layer, key, field):
        off, fmt = FIELDS[field]
        return struct.unpack_from(fmt, self.b, self._off(layer, key) + off)[0]

    def set(self, layer, key, field, value):
        off, fmt = FIELDS[field]
        struct.pack_into(fmt, self.b, self._off(layer, key) + off, value)

    def crcs(self):
        return [zlib.crc32(self.b[self._off(l, 0):self._off(l, NKEYS)]) for l in range(LAYERS)]

    def name(self, layer):
        o = OFF_NAMES + layer * NAME_LEN
        return self.b[o:o + NAME_LEN].split(b'\0')[0].decode('utf-8', 'replace') or f'Layer {layer + 1}'

    def set_name(self, layer, name):
        """Store a layer's name: UTF-8, null-terminated, cut to fit without splitting a character."""
        data = name.encode('utf-8')[:NAME_LEN - 1].decode('utf-8', 'ignore').encode('utf-8')
        o = OFF_NAMES + layer * NAME_LEN
        self.b[o:o + NAME_LEN] = data.ljust(NAME_LEN, b'\0')

    def export_layer(self, layer):
        """One layer as plain data (it is saved as JSON): its name, lighting and the 84 per-key records, in hex."""
        return {'format': LAYER_FORMAT, 'version': LAYER_VERSION, 'name': self.name(layer),
                'lighting': bytes(self.lighting(layer)).hex(),
                'keys': [self.record(layer, k).hex() for k in range(NKEYS)]}

    @staticmethod
    def parse_layer(data):
        """Check what export_layer made. Returns (name, lighting, records), or raises ValueError saying why not."""
        if not isinstance(data, dict) or data.get('format') != LAYER_FORMAT:
            raise ValueError('This is not an HM80 layer file.')
        if data.get('version') != LAYER_VERSION:
            raise ValueError(f"This layer file is version {data.get('version')}, "
                             f'but this app reads version {LAYER_VERSION}.')

        def raw(value, size, what):
            try:
                out = bytes.fromhex(value)
            except (TypeError, ValueError):
                raise ValueError(f'The layer file is damaged, because {what} is not hex data.') from None
            if len(out) != size:
                raise ValueError(f'The layer file is damaged, because {what} should be {size} bytes, not {len(out)}.')
            return out

        name = data.get('name', '')
        if not isinstance(name, str):
            raise ValueError('The layer file is damaged, because the name is not text.')
        keys = data.get('keys')
        if not isinstance(keys, list) or len(keys) != NKEYS:
            raise ValueError(f'The layer file is damaged, because it should hold {NKEYS} keys.')
        records = [raw(r, REC, f'key {k}') for k, r in enumerate(keys)]
        field = lambda rec, name: struct.unpack_from(FIELDS[name][1], rec, FIELDS[name][0])[0]
        for k, rec in enumerate(records):
            if field(rec, 'press') < 1 or field(rec, 'release') < 1 or field(rec, 'mode') > 6:
                raise ValueError(f'The layer file is damaged, because key {k} has settings the keyboard cannot use.')
        return name, raw(data.get('lighting'), 5, 'the lighting'), records

    def import_layer(self, layer, data, lighting=True):
        """Replace a layer's per-key settings (and its name, and its lighting unless told not to) with exported data.
        The switches' own travel stays as it was: that is calibration, not a setting. Returns the file's name."""
        name, light, records = self.parse_layer(data)
        for key, rec in enumerate(records):
            travel = self.get(layer, key, 'travel')
            o = self._off(layer, key)
            self.b[o:o + REC] = rec
            self.set(layer, key, 'travel', travel)
        if name.strip():
            self.set_name(layer, name.strip())
        if lighting:
            self.set_lighting(layer, light)
        return name.strip()

    def custom_lighting(self, layer):
        """A layer's custom per-key lighting: (mode, frame mask, 16 frames of 110 keys' RGB). Mode 0x10 plays the
        frames ticked in the mask in order, 0x11 at random, and n shows frame n. Read from the app; the mask byte
        order and the color order are inferred."""
        b = self.b[OFF_CUSTOM + layer * CUSTOM_LAYER:OFF_CUSTOM + (layer + 1) * CUSTOM_LAYER]
        return b[0], b[2] | b[3] << 8, [bytes(b[4 + f * SLOTS * 3:4 + (f + 1) * SLOTS * 3]) for f in range(CUSTOM_FRAMES)]

    def lighting(self, layer):
        return bytearray(self.b[OFF_L16 + layer * 5:OFF_L16 + layer * 5 + 5])

    def set_lighting(self, layer, data):
        self.b[OFF_L16 + layer * 5:OFF_L16 + layer * 5 + 5] = data

    def globals(self):
        return bytearray(self.b[OFF_G17:OFF_G17 + 21])

    def set_globals(self, data):
        self.b[OFF_G17:OFF_G17 + 21] = data


def clamp_um(v):
    """A depth as the official app shows it: -0.1 to 4.2 mm."""
    return max(-100, min(v, 4200))


def pkt(body):
    if len(body) > 31:
        raise ValueError(f'packet body is {len(body)} bytes, max 31')
    b = bytes(body).ljust(31, b'\0')
    return b + bytes([sum(b) & 0xFF])


SCREEN_W, SCREEN_H, SCREEN_MAX_FRAMES = 240, 135, 120


def pad256(n):
    return -(-n // 256) * 256


def screen_file(name, width, height, frames, delay_ms, alpha, pixels):
    """One screen image file: 41-byte header + RGB565-LE pixels (+1 alpha byte each if alpha), padded to 256."""
    head = (b'\x01\x00\x19\xc0' + name.encode()[:19].ljust(20, b'\0')
            + struct.pack('<HHHHBI', width, height, frames, delay_ms, int(alpha), len(pixels)))
    head += struct.pack('<I', sum(head))
    return (head + pixels).ljust(pad256(len(head) + len(pixels)), b'\0')


def lighting_packets(structs):
    """All six 0x16 structs, the way the app sends any lighting change."""
    return [pkt(bytes([0x16, i]) + bytes(s)) for i, s in enumerate(structs)]


FN_LAYER = LAYERS - 1                               # the Fn layer: the keyboard is on it while Fn is held
FN_DIM = 0.4                                        # how bright the Fn layer's lighting is, next to the layer it copies


def fn_lighting(struct):
    """The lighting struct that makes the Fn layer look like the layer that has `struct`, only dimmer, so it is clear
    the Fn layer is held. None if it cannot be copied, because that layer plays custom per-key lighting, whose frames
    belong to the layer."""
    if struct[0] & 0x80 or struct[0] & 0x0F == 1:       # overlay custom, or the Custom effect itself
        return None
    dim = bytearray(struct)
    dim[3] = round(dim[3] * FN_DIM)                     # byte 3 is the brightness
    return dim


def globals_packet(block):
    return pkt(b'\x17' + bytes(block))


class FirmwareRequest(Exception):
    """The keyboard asked for a firmware image (0x14/0x15). This tool never flashes firmware."""


class UntestedFirmware(Exception):
    """The keyboard reports a firmware version this tool has not been checked against. Nothing has been sent to it yet;
    add `version` to `Host.accepted` to carry on."""

    def __init__(self, version):
        super().__init__('.'.join(map(str, version)))
        self.version = version


class Host:
    """Feed raw bytes from the port; returns the reply packets to write. Mirrors the official app's behavior."""

    def __init__(self, profile):
        self.profile = profile
        self.model = self.firmware = None
        self.fw = None                              # the keyboard's firmware version (major, minor, patch)
        self.accepted = set(KNOWN_FIRMWARE)         # versions we agree to talk to
        self.lighting = {}                          # layer -> 5-byte 0x16 struct, as pushed by the keyboard
        self.globals = None                         # 21-byte 0x17 block, as pushed by the keyboard
        self.pulled = 0                             # records the keyboard pulled since connect
        self.state_rev = 0                          # bumps whenever the keyboard pushes 0x16/0x17
        self.realtime = None                        # (key, triggered, depth_um, deepest_um) of the key moving now
        self.reports = []                           # every heartbeat's `realtime`, oldest first, until taken
        self.release_at = self.fluct = 0            # where the key last released, and how far it moved over the
        self._window = deque(maxlen=10)             # last 10 readings (um), like the official switch tester
        self.series = []                            # every heartbeat's (depth, triggered, fluctuation) for the graphs,
                                                    # until taken
        self.screen = None                          # file being sent to the screen (None = no upload)
        self.screen_queue, self.screen_sent, self.screen_done = [], 0, False
        self.cali = {}                              # key -> (status 2 ok / 1 failed / 0 never, rest, bottom), raw / 7
        self.cali_done = []                         # keys the keyboard calibrated since this was last emptied
        self.cali_page = False                      # like the app on its Calibration page: flag in heartbeat replies
        self.flash = None                           # a firmware.Flash that takes over the update packets, if one is armed
        self._buf = b''

    def hello(self, version=None):
        version = bytes(version or self.fw or SW_VERSION)         # the keyboard's own, so it never asks for an update
        return pkt(b'\x01' + version + struct.pack('<6I', *self.profile.crcs()) + bytes([self.cali_page]))

    def feed(self, data):
        self._buf += data
        out = []
        while len(self._buf) >= 32:
            p, self._buf = self._buf[:32], self._buf[32:]
            if sum(p[:31]) & 0xFF != p[31]:       # same as the app: drop everything on a bad checksum
                self._buf = b''
                break
            if DEBUG_LOG:
                _debug_log(p)
            cmd = p[0]
            if self.flash and cmd in (0x01, 0x14, 0x15):
                out += self.flash.feed(p, self.hello)
                if not self.flash.done:
                    continue
                self.flash = None                   # the keyboard is on the new firmware: carry on as usual
            if cmd in (0x14, 0x15):
                raise FirmwareRequest()
            if cmd == 0x01:
                if tuple(p[6:9]) not in self.accepted:      # before any reply, so it never asks for an update
                    raise UntestedFirmware(tuple(p[6:9]))
                self.fw = tuple(p[6:9])
                self.model, self.firmware = p[1:5].decode('ascii', 'replace'), '.'.join(map(str, p[6:9]))
                # Every heartbeat carries the live travel of the key that moved last (0xFF = none), in micrometers.
                self.realtime = (p[10], bool(p[11]), *struct.unpack_from('<h2xh', p, 12)) if p[10] != 0xFF else None
                if self.realtime:                   # p[14:16] is the depth where the key last let go
                    self._window.append(clamp_um(self.realtime[2]))
                    self.release_at = clamp_um(struct.unpack_from('<h', p, 14)[0])
                    self.fluct = max(self._window) - min(self._window) if len(self._window) == 10 else 0
                else:
                    self._window.clear()
                    self.fluct = 0
                self.series.append((self._window[-1], int(self.realtime[1]), self.fluct) if self.realtime else (0, 0, 0))
                del self.series[:-256]
                self.reports.append(self.realtime)
                del self.reports[:-256]             # nobody is listening: keep only the latest
                out.append(self.hello())
            elif cmd == 0x18 and p[1] < LAYERS and p[2] < SLOTS:
                out.append(pkt(bytes([0x18, p[1], p[2]]) + self.profile.record(p[1], p[2])))
                self.pulled += 1
            elif cmd == 0x16 and p[1] < LAYERS:
                self.lighting[p[1]] = bytearray(p[2:7])   # bytes after the payload are stale buffer data
                self.state_rev += 1
            elif cmd == 0x17:
                self.globals = bytearray(p[1:22])
                self.state_rev += 1
            elif cmd == 0x19:                       # whole calibration table, 5 keys a packet; FE: one key just done
                one = p[7] == 0xFE
                for e in [p[1:7]] if one else [p[1 + 6 * i:7 + 6 * i] for i in range(5)]:
                    if e[0] < NKEYS:
                        self.cali[e[0]] = (e[1], *struct.unpack_from('<HH', e, 2))
                        if one:
                            self.cali_done.append(e[0])
            elif cmd == 0x1D and self.screen is not None:
                out += self._screen(p)
        return out

    @property
    def screen_page(self):
        """The page the keyboard's screen is on (1-4), byte 8 of the global block it pushes; None until that arrives."""
        return self.globals[8] if self.globals else None

    @property
    def ready(self):
        """True once the keyboard has pushed its lighting and global state."""
        return self.globals is not None and len(self.lighting) == LAYERS

    # Screen upload, as in the app: announce -> kb acks (1) -> file header -> kb pulls chunks (3) -> kb done (5) -> end.
    def start_screen_upload(self, data, announce):
        # screen = b'' marks "active, header not sent yet"
        self.screen, self.screen_queue, self.screen_sent, self.screen_done = b'', [bytes(data)], 0, False
        return pkt(b'\x1d\x02' + struct.pack('<I', announce))

    def cancel_screen_upload(self):
        self.screen = None
        return pkt(b'\x1d\x06')

    def _screen(self, p):
        sub = p[1]
        if sub == 3:                                # chunk request: u32 offset, u8 length
            off, n = struct.unpack_from('<IB', p, 2)
            n = min(n, 24)                          # 32 - header(7) - checksum(1)
            self.screen_sent = max(self.screen_sent, off + n)
            return [pkt(b'\x1d\x04' + struct.pack('<IB', off, n) + self.screen[off:off + n])]
        if sub in (1, 5):                           # ready for the next file
            if self.screen_queue:
                self.screen, self.screen_sent = self.screen_queue.pop(0), 0
                return [pkt(b'\x1d\x03' + self.screen[4:24] + struct.pack('<I', len(self.screen)))]
            self.screen, self.screen_done = None, True
            return [pkt(b'\x1d\x07')]
        return []


def find_port():
    """Return /dev/ttyACMn of the first connected HM80, or None."""
    base = '/sys/class/tty'
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not name.startswith('ttyACM'):
            continue
        dev = os.path.realpath(os.path.join(base, name, 'device'))
        usb = os.path.dirname(dev)                  # interface -> usb device
        try:
            vid = int(open(os.path.join(usb, 'idVendor')).read(), 16)
            pid = int(open(os.path.join(usb, 'idProduct')).read(), 16)
        except OSError:
            continue
        if (vid, pid) == (VID, PID):
            return '/dev/' + name
    return None
