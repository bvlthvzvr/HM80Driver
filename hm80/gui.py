"""PySide6 configurator for the HM80 Hall-effect keyboard."""
import fcntl
import filecmp
import functools
import glob
import json
import os
import shutil
import sys
import termios
import time
import tty
import zlib

from PySide6.QtCore import QEvent, QFile, QObject, QSettings, QSize, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QMovie, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow,
                               QSizePolicy, QStyle, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton, QSlider,
                               QSpinBox,
                               QTabBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .board import PAGE_USER, GraphView, KeyboardView, SwitchView
from .firmware import VERSION as FW_VERSION, Flash, FlashError, fetch
from .protocol import (ACTIONS, BACKLIGHT_EFFECTS, FN_LAYER, FUNCTION_GROUPS, JITTER, LAYERS, LAYOUT, LIGHTBAR_EFFECTS,
                       NKEYS, PID, SCREEN_H, SCREEN_MAX_FRAMES, SCREEN_W, VID, FirmwareRequest, Host, Profile,
                       UntestedFirmware, action_label, find_port,
                       fn_lighting, globals_packet, lighting_packets, pad256, pkt, screen_file)
from .travel import TravelTrack

CONFIG = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'hm80-linux',
                      'config.m14.hmc')
PRESETS = [('Extreme', 10, 2, 4), ('Fast', 0, 20, 20), ('Normal', 0, 200, 200)]  # official app's quick sets
SCREEN_COPY = os.path.join(os.path.dirname(CONFIG), 'screen')  # copy of what the keyboard's screen holds
GALLERY = os.path.join(os.path.dirname(CONFIG), 'gallery')     # every image chosen, to put on the keyboard again
GALLERY_ICON = (96, 54)
SOCD_LAYERS = 3                                     # the 0x17 block only has room for pairs on layers 1-3
WASD = (32, 47, 48, 49)
FACTORY = Profile.factory()                     # what each key does out of the box; layer 0 matches the printed caps
# One-click key functions: (action, name, theme icon, Qt's own icon if there is no icon theme). The seven media keys
# are all the firmware has (its media table, in the official app's order); Screenshot is the PrtSc key.
SP = QStyle.StandardPixmap
MEDIA_KEYS = [(b'\x02\x01\xff', 'Vol −', 'audio-volume-low', SP.SP_MediaVolume),
              (b'\x02\x02\xff', 'Vol +', 'audio-volume-high', SP.SP_MediaVolume),
              (b'\x02\x03\xff', 'Previous', 'media-skip-backward', SP.SP_MediaSkipBackward),
              (b'\x02\x04\xff', 'Next', 'media-skip-forward', SP.SP_MediaSkipForward),
              (b'\x02\x05\xff', 'Play/Pause', 'media-playback-start', SP.SP_MediaPlay),
              (b'\x02\x06\xff', 'Mute', 'audio-volume-muted', SP.SP_MediaVolumeMuted),
              (b'\x02\x07\xff', 'Stop', 'media-playback-stop', SP.SP_MediaStop),
              (b'\x00\x46\xff', 'Screenshot', 'camera-photo', SP.SP_DesktopIcon)]              # PrtSc
KEY_NAMES = {action[:2]: name for action, name, _, _ in MEDIA_KEYS}
# Key modes, by mode byte, named like the official app: (name, then what the form calls key A, key B, the count and the
# time, None where the mode does not use it; whether the time can have jitter; what the mode does). Mode 5, one key
# canceling another, is left out: the official app no longer offers it. Light/Heavy's judge speed is compared with
# the press speed the firmware measures: at or above count / 1000 the press is heavy.
KEY_MODES = {
    0: ('Press', 'Key', None, None, None, False, 'An ordinary key.'),
    1: ('Press/Release', 'Key', 'On release', None, 'Held for', True,
        'Works as an ordinary key, and when it is let go it also presses a second key, held for the time set.'),
    2: ('Short/Long', 'Tap', 'Hold', None, 'Hold after', False,
        'A tap sends one key. Holding it longer than the time set sends the other.'),
    3: ('N Rapid fire', 'Key', None, 'Taps', 'Every', True,
        'One press taps the key the number of times set, the time set apart.'),
    4: ('Rapid fire', 'Key', None, None, 'Every', True,
        'While the key is held it is tapped over and over, the time set apart.'),
    6: ('Light/Heavy', 'Light', 'Heavy', 'Judge speed', None, False,
        'A slow press sends one key, a fast press the other. The higher the judge speed, the faster a press has to be '
        'to count as heavy.'),
}
MODE_TAGS = {1: 'P/R', 2: 'S/L', 3: 'NRF', 4: 'RF', 5: 'SOCD', 6: 'L/H'}   # what the drawn keyboard tags them with
NO_ACTION = b'\x00\xff\xff'
LIGHT_FIELD_W = 240                             # width of every drop-down, button and slider in the Lighting tab, px
TILE = 52                                      # side of a function button in the Media tab, in px
LAYER_NAME_MAX = 16                             # characters a layer's name may have when it is renamed
LAYER_SUFFIX, LAYER_FILTER = '.hm80layer', 'HM80 layer (*.hm80layer)'     # what an exported layer is saved as
UNTESTED_MSG = ('The keyboard reports firmware {0}. This program was checked against 2.5.3 to 2.5.7 and 2.5.9, which '
                'run the same keyboard code, so yours may behave differently.\n\nIt can carry on and talk to the '
                'keyboard as it is, install firmware 2.5.9, or disconnect.')
STUCK_MSG = ('The keyboard is asking for a firmware image and will not type until it gets one. Unplugging it does not '
             'clear this.')
FLASH_MSG = ("This app downloads the firmware from the keyboard maker's website (about 90 KB), checks it against a "
             'known checksum, and installs it. It takes a few seconds. Do not unplug the keyboard or close this app '
             'until it finishes.\n\nThis has been tested on one keyboard. If it fails, the official HM Driver on native '
             'Windows can install the firmware instead. Never use a virtual machine for that.')


def hue_to_byte(h):
    return round(max(h, 0) * 256 / 360) % 256


def byte_to_hue(b):
    return round(b * 360 / 256) % 360


def mm(v):
    return f'{v / 100:.2f}'


def convert_screen_image(path):
    """Image or GIF -> screen file, following the official app. Returns (file, announced size, summary)."""
    ext = path.rsplit('.', 1)[-1].lower().replace('jpeg', 'jpg')
    if ext not in ('gif', 'png', 'jpg', 'bmp'):
        raise ValueError('use a GIF, PNG, JPG or BMP file')
    frames, delay = [], 0
    if ext == 'gif':
        movie = QMovie(path)
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        while len(frames) <= SCREEN_MAX_FRAMES and movie.jumpToFrame(len(frames)):
            delay = delay or max(movie.nextFrameDelay(), 0)   # the screen uses one delay for all frames
            frames.append(movie.currentImage())
    else:
        frames.append(QImage(path))
    if not frames or frames[0].isNull():
        raise ValueError('could not read the image')
    if len(frames) > SCREEN_MAX_FRAMES:
        raise ValueError(f'GIFs can have at most {SCREEN_MAX_FRAMES} frames')
    size = frames[0].size()
    if size.width() > SCREEN_W or size.height() > SCREEN_H:
        size = size.scaled(SCREEN_W, SCREEN_H, Qt.AspectRatioMode.KeepAspectRatio)
    w, h, alpha = size.width(), size.height(), ext == 'png'
    pixels = bytearray()
    for f in frames:
        if f.size() != size:
            f = f.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        rgb = f.convertToFormat(QImage.Format.Format_RGB16)          # RGB565, little-endian like the app
        bits, bpl = bytes(rgb.constBits()), rgb.bytesPerLine()
        frame = b''.join(bits[y * bpl:y * bpl + w * 2] for y in range(h))
        if alpha:
            a = f.convertToFormat(QImage.Format.Format_Alpha8)
            abits, abpl = bytes(a.constBits()), a.bytesPerLine()
            mixed = bytearray(w * h * 3)
            mixed[0::3], mixed[1::3] = frame[0::2], frame[1::2]
            mixed[2::3] = b''.join(abits[y * abpl:y * abpl + w] for y in range(h))
            frame = mixed
        pixels += frame
    data = screen_file(f'user.{ext}', w, h, len(frames), delay, alpha, bytes(pixels))
    # The app announces GIFs as if they had an alpha byte per pixel; copy that exactly.
    announce = pad256(41 + w * h * len(frames) * (3 if ext in ('gif', 'png') else 2))
    timing = f', {delay} ms per frame' if ext == 'gif' else ''
    return data, announce, f'{w}×{h}, {len(frames)} frame(s){timing}, {len(data) / 1024:.0f} KB'


@functools.cache
def caps_led():
    """The sysfs LED the kernel keeps for this keyboard's Caps Lock light (None if it has none)."""
    ids = f'{VID:04X}:{PID:04X}'
    return next((led for led in glob.glob('/sys/class/leds/*::capslock') if ids in os.path.realpath(led)), None)


def gallery_add(path, name=None):
    """Keep a copy of an image in the gallery unless it has the same file already. Returns the gallery's path to it."""
    os.makedirs(GALLERY, exist_ok=True)
    for have in os.listdir(GALLERY):
        if filecmp.cmp(path, os.path.join(GALLERY, have), shallow=False):
            return os.path.join(GALLERY, have)
    stem, ext = os.path.splitext(os.path.basename(path))
    stem = name or stem                             # `name` is given without the extension
    dest, n = os.path.join(GALLERY, stem + ext.lower()), 1
    while os.path.exists(dest):                     # the same name for a different picture
        n += 1
        dest = os.path.join(GALLERY, f'{stem}-{n}{ext.lower()}')
    shutil.copyfile(path, dest)
    return dest


def caps_lock():
    try:
        return int(open(caps_led() + '/brightness').read()) > 0
    except (OSError, ValueError, TypeError):
        return False


class NoWheel(QObject):
    """Installed on the whole app: the mouse wheel never changes a slider or a drop-down, so scrolling past one by
    accident cannot alter a setting. Dragging, clicking and the keyboard still work."""

    def eventFilter(self, obj, event):
        return event.type() == QEvent.Wheel and isinstance(obj, (QSlider, QComboBox, QSpinBox))


class NoArrows(QObject):
    """Installed on the whole app: the arrow keys never change the tab or the layer, wherever the keyboard focus is.
    With focus on a tab bar they switch tabs, and with focus on a layer button they switch layers, so pressing them on
    the keyboard being configured would move the window under you. Sliders, text fields and lists still use them."""
    ARROWS = (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down)

    def __init__(self, parent, layers):
        super().__init__(parent)
        self.layers = layers                    # the QButtonGroup of layer buttons

    def eventFilter(self, obj, event):
        return (event.type() in (QEvent.KeyPress, QEvent.KeyRelease) and event.key() in self.ARROWS
                and (isinstance(obj, QTabBar) or obj in self.layers.buttons()))


class NameEdit(QLineEdit):
    """Sits inside a button while its name is edited, and widens the button to fit what is typed. Enter or clicking
    away keeps the name, Escape cancels."""
    done = Signal(object)                       # the new text, or None when canceled

    def __init__(self, button, text, limit):
        super().__init__(text, button)
        self.button, self.open = button, True
        self.setMaxLength(limit)
        self.setAlignment(Qt.AlignCenter)
        self.textChanged.connect(self.fit)
        self.returnPressed.connect(lambda: self.finish(self.text()))
        button.installEventFilter(self)         # the button is resized as the name grows: follow it
        self.fit()
        self.setGeometry(button.rect())
        self.show()
        self.selectAll()
        self.setFocus()

    def fit(self, _=None):
        wanted = self.fontMetrics().horizontalAdvance(self.text()) + 32
        self.button.setMinimumWidth(max(self.button.sizeHint().width(), wanted))

    def eventFilter(self, obj, event):
        if obj is self.button and event.type() == QEvent.Resize:
            self.setGeometry(self.button.rect())
        return False

    def finish(self, text):
        if self.open:
            self.open = False
            self.button.setMinimumWidth(0)
            self.hide()
            self.done.emit(text)
            self.deleteLater()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.finish(None)
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if e.reason() != Qt.PopupFocusReason:   # (its own right-click menu is not "clicking away")
            self.finish(self.text())


class MainWindow(QMainWindow):
    def __init__(self, profile):
        super().__init__()
        self.setWindowTitle('HM80 Configurator')
        self.profile, self.layer = profile, 0
        self.saved = profile.copy()                 # what was last saved: changes are what differs from it
        self.page = 0                               # the tab the unsaved-changes check last let the user onto
        self.settings = QSettings(os.path.join(os.path.dirname(CONFIG), 'settings.ini'), QSettings.Format.IniFormat)
        self.fn_follows = self.settings.value('fn_follows_lighting', True, type=bool)   # see mirror_fn
        self.base = 0                               # the layer in use: the one shown, or before Fn was held
        self.fn_sent = None                         # the Fn layer's lighting as last sent, while it follows
        self.fd = self.notifier = self.host = self.port = None
        self.blocked, self.updating, self.seen_rev = False, False, -1
        self.flash = self.flash_dialog = None       # the firmware flash under way, and the box that shows it
        self.status_text = 'Looking for the keyboard…'
        self.travel, self._shown = TravelTrack(), None   # live key travel, played back smoothly; what is on screen
        self.rest_key = None                        # the key the depth gauge was last showing in use
        self.loading_sliders = False                # True while the program, not the user, moves the sliders
        self.set_buttons = []                       # (button, keys): the buttons that select a fixed set of keys
        self.anim = QTimer(self, interval=8, timeout=self.animate)
        self.no_wheel = NoWheel(self)
        QApplication.instance().installEventFilter(self.no_wheel)

        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        bar = QHBoxLayout()
        self.layer_group = QButtonGroup(self)
        for i in range(LAYERS):
            b = QPushButton(checkable=True)
            b.setContextMenuPolicy(Qt.CustomContextMenu)            # right click: rename
            b.customContextMenuRequested.connect(lambda pos, i=i: self.layer_menu(i, pos))
            self.layer_group.addButton(b, i)
            bar.addWidget(b)
        self.layer_group.idClicked.connect(self.choose_layer)
        self.no_arrows = NoArrows(self, self.layer_group)
        QApplication.instance().installEventFilter(self.no_arrows)
        bar.addStretch()
        self.save_btn = QPushButton(QIcon.fromTheme('document-save', self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogSaveButton)), 'Save')
        self.save_btn.setShortcut(QKeySequence.StandardKey.Save)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.save_btn)
        root.addLayout(bar)
        root.addSpacing(2)

        self.board = KeyboardView()
        self.board.selectionChanged.connect(self.selection_changed)
        self.board.screenClicked.connect(self.screen_clicked)
        self.show_saved_screen()
        root.addWidget(self.board)

        sel = QHBoxLayout()
        for text, keys in (('Select all', tuple(self.board.keys)), ('WASD', WASD)):
            b = QPushButton(text, checkable=True)       # a toggle: pressed while its keys are the selection
            b.clicked.connect(lambda _=False, k=keys: self.toggle_set(k))
            self.set_buttons.append((b, set(keys)))
            sel.addWidget(b)
        clear = QPushButton('Clear selection')
        clear.clicked.connect(lambda: self.select(()))
        sel.addWidget(clear)
        sel.addStretch()
        root.addLayout(sel)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_performance(), 'Performance')
        self.tabs.addTab(self.build_modes(), 'Modes')
        self.tabs.addTab(self.build_keys(), 'Media')
        self.light_tab = self.tabs.addTab(self.build_lighting(), 'Lighting')
        self.screen_tab = self.tabs.addTab(self.build_screen(), 'Screen')
        self.tabs.addTab(self.build_profile(), 'Profile')
        self.cali_tab = self.tabs.addTab(self.build_calibration(), 'Calibration')
        self.tabs.currentChanged.connect(self.tab_changed)
        root.addWidget(self.tabs)
        for b in self.findChildren(QPushButton):        # every button as wide as its text, never stretched out
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.timer = QTimer(self, interval=1000, timeout=self.poll)
        self.timer.start()
        self.poll()
        self.refresh()

    # ---------- UI construction ----------
    def build_performance(self):
        w, controls = QWidget(), QWidget()
        row = QHBoxLayout(w)
        row.addWidget(controls, 1)
        self.switch = SwitchView()
        row.addWidget(self.switch)
        form = QFormLayout(controls)
        form.addRow(QLabel('A key triggers once it is pressed down by the <b>initial actuation</b> plus the '
                           '<b>actuation</b> distance. After that it works as a rapid trigger, so it triggers again '
                           'each time you push down by the actuation distance, and releases when you lift by the '
                           'reset distance.', wordWrap=True))
        self.sliders = {}
        travel = max(self.profile.get(0, k, 'travel') for k in range(NKEYS))
        for field, text, lo in (('init', 'Initial actuation', 0), ('press', 'Actuation', 1), ('release', 'Reset', 1)):
            s = QSlider(Qt.Horizontal, minimum=lo, maximum=travel, singleStep=1, pageStep=10)
            value = QLabel(f'{mm(lo)} mm', minimumWidth=70)
            s.valueChanged.connect(lambda v, label=value: label.setText(f'{mm(v)} mm'))
            s.valueChanged.connect(self.sliders_moved)
            row = QHBoxLayout()
            row.addWidget(s, 1)
            row.addWidget(value)
            self.sliders[field] = s
            form.addRow(text, row)
        presets = QHBoxLayout()
        for name, i, p, r in PRESETS:
            b = QPushButton(f'{name} ({mm(i)} / {mm(p)} / {mm(r)})')
            b.clicked.connect(lambda _=False, v=(i, p, r): (self.set_sliders(*v), self.show_pending()))
            presets.addWidget(b)
        form.addRow('Presets', presets)
        apply = QPushButton('Apply to selected keys')
        apply.clicked.connect(self.apply_performance)
        form.addRow(apply)
        self.perf_note = QLabel(wordWrap=True)
        form.addRow(self.perf_note)
        self.graphs = GraphView()
        form.addRow(self.graphs)
        self.set_sliders(0, 200, 200)
        return w

    def slider(self, maximum, commit):
        s = QSlider(Qt.Horizontal, maximum=maximum)
        s.setTracking(False)                        # valueChanged fires on release / discrete steps only
        s.valueChanged.connect(lambda v: None if self.updating else commit(v))
        return s

    def build_keys(self):
        w = QWidget()
        form = QFormLayout(w)
        form.addRow(QLabel('Select keys above, then click a function to give it to them on the layer shown. Use the Fn '
                           'layer for Fn + key shortcuts.', wordWrap=True))
        groups = [('Media', [(a, n, QIcon.fromTheme(i, self.style().standardIcon(s))) for a, n, i, s in MEDIA_KEYS])]
        groups += [(title, [(bytes((kind, c, 0xFF)), ACTIONS[kind][c], None) for c in codes])   # what else the Fn layer uses
                   for title, kind, codes in FUNCTION_GROUPS]
        tiles, rows = [], []
        for group in groups:                                    # a row of buttons for each kind; a short one shares
            if len(group[1]) <= 2 and rows:                     # the row before it
                rows[-1].append(group)
            else:
                rows.append([group])
        for groups_in_row in rows:
            row = QHBoxLayout(spacing=4)
            for n, (_, items) in enumerate(groups_in_row):
                if n:
                    row.addSpacing(10)
                for action, name, icon in items:                # every function is a square button
                    tip = f'{name} on the selected keys' + (' (the PrtSc key)' if action[:2] == b'\x00\x46' else '')
                    b = QToolButton(text=name, toolTip=tip, toolButtonStyle=Qt.ToolButtonStyle.ToolButtonTextOnly)
                    if icon:
                        b.setIcon(icon)
                        b.setIconSize(QSize(TILE * 2 // 5, TILE * 2 // 5))
                        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                    b.setFixedSize(TILE, TILE)
                    b.clicked.connect(lambda _=False, a=action, n=name: self.assign_key(a, n))
                    row.addWidget(b)
                    tiles.append(b)
            row.addStretch()
            form.addRow(groups_in_row[0][0], row)
        font = QFont(self.font())                               # one text size for all, small enough for the longest
        wide = max(QFontMetrics(font).horizontalAdvance(b.text()) for b in tiles)
        if wide > TILE - 8:
            if font.pointSizeF() > 0:
                font.setPointSizeF(font.pointSizeF() * (TILE - 8) / wide)
            else:
                font.setPixelSize(max(6, font.pixelSize() * (TILE - 8) // wide))
        for b in tiles:
            b.setFont(font)
        restore = QPushButton('Factory default')
        restore.clicked.connect(self.restore_keys)
        form.addRow(restore)
        self.keys_note = QLabel(wordWrap=True)
        form.addRow(self.keys_note)
        return w

    def build_modes(self):
        w = QWidget()
        self.mode_form = form = QFormLayout(w, fieldGrowthPolicy=QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.addRow(QLabel('Select keys above, pick a mode and its keys, then press Apply to selected keys. It applies '
                           'to the layer shown.', wordWrap=True))
        self.mode_tabs = QTabBar(expanding=False)
        for m, spec in KEY_MODES.items():
            self.mode_tabs.setTabData(self.mode_tabs.addTab(spec[0]), m)
            if m == 2:                                  # SOCD (not a key mode: the 0x17 block's pairs) after Short/Long
                self.mode_tabs.setTabData(self.mode_tabs.addTab('SOCD'), 'socd')
        self.mode_tabs.currentChanged.connect(self.mode_tab_changed)
        form.addRow(self.mode_tabs)
        self.mode_about = QLabel(wordWrap=True)
        form.addRow(self.mode_about)
        # A key is picked by clicking its button, then pressing that key on the keyboard (read from the keyboard's own
        # key reports, so it is the key's printed legend).
        self._picks = {}                                        # mode tab -> what keys A and B are to send (see `picked`)
        self.capturing = None                                   # the picker waiting for a key press, if one is
        self.auto_key, self.auto_from = None, 0                 # the key a press selected (and which picker's press)
        self.pickers = []
        for which in (0, 1):
            b = QPushButton(checkable=True, minimumWidth=140)
            b.clicked.connect(lambda on, which=which: self.capture(which if on else None))
            self.pickers.append(b)
        self.count = QSpinBox(minimum=1, maximum=1000)
        self.time = QSpinBox(minimum=5, maximum=5000, suffix=' ms')
        self.mode_rows = [(QLabel(), field) for field in (*self.pickers, self.count, self.time)]
        for label, field in self.mode_rows:
            form.addRow(label, field)
        self.jitter = QCheckBox('Add 1–5 ms at random to each wait, so the timing is not exactly the same every time')
        form.addRow(self.jitter)
        apply, self.mode_reset = QPushButton('Apply to selected keys'), QPushButton('Reset')
        apply.clicked.connect(self.apply_mode)
        self.mode_reset.clicked.connect(self.reset_mode)
        self.mode_buttons = QHBoxLayout()
        self.mode_buttons.addWidget(apply)
        self.mode_buttons.addWidget(self.mode_reset)
        self.mode_buttons.addStretch()
        form.addRow(self.mode_buttons)
        self.mode_note = QLabel(wordWrap=True)
        form.addRow(self.mode_note)
        self.socd_box = self.build_socd()
        form.addRow(self.socd_box)
        self.show_mode_form()
        self.capture(None)
        return w

    def build_lighting(self):
        w = QWidget()
        form = QFormLayout(w)
        self.effect = QComboBox()
        self.effect.addItems(BACKLIGHT_EFFECTS)
        self.effect.activated.connect(lambda v: self.set_light_bits(0x0F, v))
        self.color = QPushButton('Pick color…')
        self.color.clicked.connect(self.pick_color)
        self.brightness = self.slider(255, lambda v: self.set_light_byte(3, v))
        self.speed = self.slider(255, lambda v: self.set_light_byte(4, v))
        for i, s in ((3, self.brightness), (4, self.speed)):     # the drawn light bar follows while dragging
            s.sliderMoved.connect(lambda v, i=i: self.show_light_bar({i: v}))
        self.bar = QComboBox()
        self.bar.addItems(LIGHTBAR_EFFECTS)
        self.bar.activated.connect(lambda v: self.set_light_bits(0x70, v << 4))
        self.overlay = QCheckBox('Overlay custom lighting on each key')
        self.overlay.clicked.connect(lambda on: self.set_light_bits(0x80, 0x80 if on else 0))
        self.master = QCheckBox('Lights on')
        self.master.clicked.connect(lambda on: self.set_global_bits(2, 0x80, 0x80 if on else 0))
        self.autooff = self.slider(60, lambda v: self.set_global_bits(2, 0x7F, v))
        self.autooff_label = QLabel()
        self.autooff.sliderMoved.connect(lambda v: self.autooff_label.setText(self.autooff_text(v)))
        self.indicator = QCheckBox('Indicator light on')
        self.indicator.clicked.connect(lambda on: self.set_global_byte(3, int(on)))
        self.indicator_color = QPushButton('Pick color…')
        self.indicator_color.clicked.connect(self.pick_indicator)
        self.winlock = QCheckBox('Win key locked on this layer')
        self.winlock.clicked.connect(lambda on: self.set_global_bits(7, 1 << self.layer, (1 << self.layer) * on))
        self.fn_note = QLabel(wordWrap=True)
        self.fn_follow = QCheckBox('Fn layer shows the lighting of the layer I am on')
        self.fn_follow.setChecked(self.fn_follows)
        self.fn_follow.setToolTip('Holding Fn puts the keyboard on the Fn layer, which has lighting of its own '
                                  '(Cycle by default). With this on, the Fn layer is given the lighting of the layer '
                                  'you were on, only dimmer, so holding Fn does not change the effect.')
        self.fn_follow.clicked.connect(self.set_fn_follows)
        form.addRow(QLabel('<b>This layer</b>'))
        form.addRow(self.fn_note)
        form.addRow('Backlight effect', self.effect)
        form.addRow('Color', self.color)
        form.addRow('Brightness', self.brightness)
        form.addRow('Speed', self.speed)
        form.addRow('Light bar effect', self.bar)
        form.addRow(self.overlay)
        form.addRow(self.winlock)
        form.addRow(QLabel('<b>Whole keyboard</b>'))
        form.addRow(self.master)
        auto = QHBoxLayout()
        auto.addWidget(self.autooff)
        auto.addWidget(self.autooff_label)
        auto.addStretch()
        form.addRow('Auto off', auto)
        form.addRow(self.indicator)
        form.addRow('Indicator color', self.indicator_color)
        form.addRow(self.fn_follow)
        for field in (self.effect, self.color, self.brightness, self.speed, self.bar, self.autooff,
                      self.indicator_color):            # one width, so the right ends line up
            field.setFixedWidth(LIGHT_FIELD_W)
        return w

    def build_socd(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.socd_labels, self.socd_buttons = [], []
        for pair in range(2):
            h = QHBoxLayout()
            label = QLabel(minimumWidth=220)
            bind, clear = QPushButton('Bind the 2 selected keys'), QPushButton('Clear')
            bind.clicked.connect(lambda _=False, p=pair: self.bind_socd(p))
            clear.clicked.connect(lambda _=False, p=pair: self.set_socd(p, 0xFF, 0xFF))
            for x in (label, bind, clear):
                h.addWidget(x)
            h.addStretch()
            self.socd_labels.append(label)
            self.socd_buttons += [bind, clear]
            v.addLayout(h)
        self.socd_per_key = QLabel(wordWrap=True)
        v.addWidget(self.socd_per_key)
        return w

    def build_screen(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(f"Put your own image or GIF on the keyboard's {SCREEN_W}×{SCREEN_H} screen. Anything larger "
                           f'is scaled down to fit. GIFs can have up to {SCREEN_MAX_FRAMES} frames, and the first '
                           "frame's delay is used for all of them. Big GIFs take a few minutes to upload.", wordWrap=True))
        row = QHBoxLayout()
        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setFixedSize(SCREEN_W, SCREEN_H)
        self.preview.setStyleSheet('background: black')
        row.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)   # level with the first button beside it
        col = QVBoxLayout()
        choose = QPushButton('Choose image or GIF…')
        choose.clicked.connect(self.choose_screen_image)
        self.screen_info = QLabel(wordWrap=True)
        self.upload_btn, self.cancel_btn = QPushButton('Upload to keyboard'), QPushButton('Cancel upload')
        self.upload_btn.clicked.connect(self.start_upload)
        self.cancel_btn.clicked.connect(self.cancel_upload)
        self.progress = QProgressBar()
        self.screen_note = QLabel(wordWrap=True)
        pick = QHBoxLayout()
        pick.addWidget(choose)
        pick.addWidget(self.screen_info, 1)   # what was chosen, beside the button that chose it
        col.addLayout(pick)
        for x in (self.upload_btn, self.cancel_btn, self.progress, self.screen_note):
            col.addWidget(x)
        col.addStretch()
        row.addLayout(col, 1)
        v.addLayout(row)
        head = QHBoxLayout()
        self.gallery_refresh = QPushButton('Refresh')
        self.gallery_refresh.setToolTip(f'Look again for images you put in the gallery folder at\n{GALLERY}')
        self.gallery_refresh.clicked.connect(self.refresh_gallery)
        head.addWidget(self.gallery_refresh)
        self.gallery_remove = QPushButton('Delete')
        self.gallery_remove.setToolTip('Move the selected image to the trash and take it out of the gallery.')
        self.gallery_remove.clicked.connect(self.remove_from_gallery)
        head.addWidget(self.gallery_remove)
        head.addStretch()
        v.addLayout(head)
        self.gallery = QListWidget(viewMode=QListView.ViewMode.IconMode, movement=QListView.Movement.Static,
                                   resizeMode=QListView.ResizeMode.Adjust, iconSize=QSize(*GALLERY_ICON),
                                   wordWrap=True, uniformItemSizes=True)
        self.gallery.setMinimumHeight(GALLERY_ICON[1] + 36)
        self.gallery.itemClicked.connect(self.pick_from_gallery)
        self.gallery.itemSelectionChanged.connect(
            lambda: self.gallery_remove.setEnabled(bool(self.gallery.selectedItems())))
        v.addWidget(self.gallery, 1)
        self.screen_data, self.uploading, self.movie = None, False, None
        if glob.glob(SCREEN_COPY + '.*'):               # what is on the keyboard now is worth keeping too
            gallery_add(glob.glob(SCREEN_COPY + '.*')[0], 'on keyboard')
        self.fill_gallery()
        self.set_upload_buttons()
        return w

    def build_calibration(self):
        w = QWidget()
        form = QFormLayout(w)
        form.addRow(QLabel('Calibration teaches the keyboard where each switch rests and where it bottoms out. It is '
                           'only needed after changing switches, or if a key triggers too early or too late. '
                           'Press Start, then press each key you want to calibrate <b>slowly all the way down</b>, '
                           'let it come back up and leave it alone for a second. It lights blue on the keyboard '
                           'and here when done (red means check that the switch is seated). Keys you don\'t press keep '
                           'their old calibration. Press Stop to save. Unplugging before Stop throws the new '
                           'values away.', wordWrap=True))
        self.cali_btn = QPushButton('Start calibration', checkable=True)
        self.cali_btn.clicked.connect(self.set_calibrating)
        form.addRow(self.cali_btn)
        self.cali_note = QLabel(wordWrap=True)
        form.addRow(self.cali_note)
        self.calibrating = False
        return w

    def calibration_page(self, _=None):
        """Like the official app: tell the keyboard while its Calibration page is open, and show the results."""
        on = self.tabs.currentIndex() == self.cali_tab
        if self.host:
            self.host.cali_page = on or self.calibrating
        if not on and not self.calibrating:
            self.board.marks = {}
            self.board.update()
        self.show_calibration()

    def set_calibrating(self, on):
        if on:
            self.host.cali_done.clear()
            self.board.marks = {}
        self.calibrating = on
        self.send([pkt(b'\x19\x01' if on else b'\x19\x00')])   # 19 00 also saves; 19 02 (factory) is never sent
        self.calibration_page()

    def show_calibration(self):
        self.cali_btn.setText('Stop and save' if self.calibrating else 'Start calibration')
        self.cali_btn.setChecked(self.calibrating)
        if not self.host or not self.host.cali:
            return self.cali_note.setText('')
        for k in self.host.cali_done:
            self.board.marks[k] = QColor(47, 124, 246, 150) if self.host.cali[k][0] == 2 else QColor(224, 68, 46, 170)
        self.host.cali_done.clear()
        self.board.update()
        if self.calibrating:
            failed = [self.key_name(k) for k in self.board.marks if self.host.cali[k][0] != 2]
            text = f'Calibrating, {len(self.board.marks)} key(s) done.'
            return self.cali_note.setText(text + (f' Check these switches ({", ".join(failed)}).' if failed else ''))
        bad = [self.key_name(k) for k in sorted(self.host.cali) if self.host.cali[k][0] != 2]
        self.cali_note.setText(f'Not calibrated, or failed last time ({", ".join(bad)}).' if bad else
                               f'All {len(self.host.cali)} keys are calibrated.')

    def closeEvent(self, e):
        if self.flash:
            QMessageBox.warning(self, 'Installing firmware', 'Wait until the firmware is installed before closing.')
            return e.ignore()
        if not self.settled('closing'):
            return e.ignore()
        if self.calibrating and self.fd is not None:             # keep what was calibrated
            self.send([pkt(b'\x19\x00')])
        super().closeEvent(e)

    def tab_changed(self, index):
        """Leaving a tab with unsaved changes asks first. The tab is put back before the question is asked, so the
        page the user is leaving stays on screen behind it."""
        if index != self.page and self.dirty and not self.updating:     # (refresh moves the tab if its own is disabled)
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(self.page)
            self.tabs.blockSignals(False)
            if self.settled('switching tabs'):
                self.tabs.setCurrentIndex(index)                # nothing is unsaved now, so this one goes through
            return
        self.page = index
        self.calibration_page()

    def build_profile(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel('The keyboard never reports its settings for each key. This app keeps the master copy in '
                           f'<br><code>{CONFIG}</code><br>It uses the same .hmc format as the official Windows app, '
                           'which stores it at <code>Documents\\config.m14.hmc</code>. Profiles move both ways.',
                           wordWrap=True))
        for text, fn in (('Import .hmc profile…', self.import_profile), ('Export .hmc profile…', self.export_profile),
                         ('Restore factory defaults…', self.factory_reset)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            v.addWidget(b, alignment=Qt.AlignLeft)
        self.fw_button = QPushButton(f'Install firmware {".".join(map(str, FW_VERSION))}…')
        self.fw_button.clicked.connect(self.install_firmware)
        v.addWidget(self.fw_button, alignment=Qt.AlignLeft)
        v.addStretch()
        return w

    # ---------- state helpers ----------
    @property
    def live(self):
        return self.host is not None and self.host.ready

    def light(self):
        return self.host.lighting[self.layer] if self.live else self.profile.lighting(self.layer)

    def glob(self):
        return self.host.globals if self.live else self.profile.globals()

    def selected(self):
        return sorted(self.board.selected)

    def select(self, keys):
        self.board.set_selection(keys)

    def toggle_set(self, keys):
        """Select these keys, or clear the selection if they are already exactly what is selected."""
        self.select(() if self.board.selected == set(keys) else keys)

    # ---------- saving ----------
    # Every change takes effect on the keyboard straight away, so it can be tried. Saving is what keeps it: it makes
    # the profile the stored master copy. Until then the change is what differs from `saved`, and Discard goes back.
    @property
    def dirty(self):
        return not self.profile.same_settings(self.saved)

    def store(self):
        self.profile.save(CONFIG)
        self.saved = self.profile.copy()

    def save(self):
        """Keep the changes: store the profile. False if it could not be written."""
        try:
            self.store()
        except OSError as e:
            self.warn('Save failed', f'Could not write {CONFIG}:\n{e.strerror}')
            return False
        self.refresh()
        self.statusBar().showMessage('Saved', 4000)
        return True

    def ask_unsaved(self, what):
        """'save', 'discard' or None (cancel): what to do with the unsaved changes before `what` goes ahead."""
        box = QMessageBox(QMessageBox.Question, 'Unsaved changes',
                          f'You have changes that are not saved yet. Save them before {what}?', parent=self)
        box.setInformativeText('Discard puts everything back as it was when you last saved, on the keyboard too.')
        save = box.addButton('Save', QMessageBox.AcceptRole)
        discard = box.addButton('Discard', QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(save)
        box.exec()
        return 'save' if box.clickedButton() is save else 'discard' if box.clickedButton() is discard else None

    def settled(self, what):
        """True if `what` (switching layers or tabs, closing) can go ahead: nothing is unsaved, or the user saved it
        or threw it away. False if they canceled, or the save failed."""
        if not self.dirty:
            return True
        choice = self.ask_unsaved(what)
        if choice == 'save':
            return self.save()
        if choice == 'discard':
            self.revert()
            return True
        return False

    def revert(self):
        """Put everything back as it was when it was last saved, on the keyboard too. The layer stays where it is."""
        profile = self.saved.copy()
        g = profile.globals()
        g[0] = self.profile.globals()[0]
        profile.set_globals(g)
        self.use_profile(profile)
        self.refresh()
        self.selection_changed()

    def note(self, text):
        self.perf_note.setText(text)

    @staticmethod
    def autooff_text(v):
        return 'Never' if v == 0 else f'{v} min'

    # ---------- refresh ----------
    def refresh(self):
        self.updating = True
        for i, b in enumerate(self.layer_group.buttons()):
            b.setText(self.profile.name(i))
            b.setChecked(i == self.layer)
        self.board.values = {i: mm(self.profile.get(self.layer, i, 'init') + self.profile.get(self.layer, i, 'press'))
                             for i in self.board.keys}
        self.board.legends = {}
        for k in self.board.keys:
            action = self.profile.get(self.layer, k, 'action')
            if action[:2] != FACTORY.get(0, k, 'action')[:2]:           # not what the printed legend says
                self.board.legends[k] = KEY_NAMES.get(action[:2]) or action_label(action)
        s, g = self.light(), self.glob()
        self.board.tags = {k: [MODE_TAGS[m]] for k in self.board.keys             # every mode a key has: a key with a mode
                           if (m := self.profile.get(self.layer, k, 'mode')) in MODE_TAGS}   # and in an SOCD pair has both
        if self.layer < SOCD_LAYERS:                                # keys in an SOCD pair on this layer
            for k in g[9 + self.layer * 4:13 + self.layer * 4]:
                if k != 0xFF and 'SOCD' not in self.board.tags.get(k, ()):
                    self.board.tags.setdefault(k, []).append('SOCD')
        self.board.update()
        self.effect.setCurrentIndex(s[0] & 0x0F)
        self.bar.setCurrentIndex((s[0] >> 4) & 7)
        self.overlay.setChecked(bool(s[0] & 0x80))
        self.color.setStyleSheet(f'background: {QColor.fromHsv(byte_to_hue(s[1]), s[2], 255).name()}')
        self.brightness.setValue(s[3])
        self.speed.setValue(s[4])
        self.winlock.setChecked(bool(g[7] & (1 << self.layer)))
        self.master.setChecked(bool(g[2] & 0x80))
        self.autooff.setValue(g[2] & 0x7F)
        self.autooff_label.setText(self.autooff_text(g[2] & 0x7F))
        self.indicator.setChecked(bool(g[3]))
        self.indicator_color.setStyleSheet(f'background: {QColor.fromHsv(byte_to_hue(g[4]), g[5], 255).name()}')
        locked = self.layer == FN_LAYER and self.fn_mirrors()       # its lighting is that of the layer in use
        for control in (self.effect, self.color, self.brightness, self.speed, self.bar, self.overlay):
            control.setEnabled(not locked)
        self.fn_note.setVisible(locked)
        if locked:
            self.fn_note.setText(f'The Fn layer shows the lighting of {self.profile.name(self.layer_in_use())}, '
                                 'dimmed. Change it on that layer, or untick "Fn layer shows the lighting of the '
                                 'layer I am on" below.')
        self.show_light_bar()
        for pair, label in enumerate(self.socd_labels):
            if self.layer >= SOCD_LAYERS:
                label.setText(f'Pair {pair + 1}: not available on this layer')
                continue
            a, b = g[9 + self.layer * 4 + pair * 2: 11 + self.layer * 4 + pair * 2]
            names = 'not set' if a == 0xFF else f'{self.key_name(a)} + {self.key_name(b)}'
            label.setText(f'Pair {pair + 1}: {names}')
        for b in self.socd_buttons:
            b.setEnabled(self.layer < SOCD_LAYERS and self.live)      # SOCD pairs live in the keyboard's 0x17 block
        cancels = {tuple(action_label(self.profile.get(self.layer, k, f)) for f in ('action', 'action2'))
                   for k in self.board.keys if self.profile.get(self.layer, k, 'mode') == 5}
        pairs = [f'{a} ↔ {b}' if (b, a) in cancels else f'{a} → {b}' for a, b in sorted(cancels)
                 if (b, a) not in cancels or a < b]                 # a key mode, kept in each key (see MODE_TAGS)
        self.socd_per_key.setVisible(bool(pairs))
        self.socd_per_key.setText(f'The official app\'s old "Cover" mode left these key pairs on this layer, {", ".join(pairs)}. '
                                  'They are kept in each key and work on any layer. To remove one, select its keys and '
                                  'press Reset on the Press tab.')
        for tab in (self.light_tab, self.cali_tab):  # lighting and calibration need the keyboard
            self.tabs.setTabEnabled(tab, self.live)
        self.set_upload_buttons()
        dirty = self.dirty
        self.save_btn.setEnabled(dirty)
        self.save_btn.setToolTip('Keep your changes (Ctrl+S)' if dirty else 'Nothing to save, there are no changes since '
                                 'the last save')
        self.updating = False
        self.show_rest()
        self.refresh_status()

    def show_light_bar(self, changes=None):
        """Draw the light bar from this layer's lighting, with any not-yet-sent byte changes {index: value}."""
        s = self.light().copy()
        for i, v in (changes or {}).items():
            s[i] = v
        self.board.set_lighting(s, on=bool(self.glob()[2] & 0x80), custom=self.profile.custom_lighting(self.layer))

    def key_name(self, idx):
        return next((label for row in LAYOUT for i, label, _, _ in row if i == idx), f'#{idx}')

    def refresh_status(self):
        fw = self.host.fw if self.host else None
        self.fw_button.setEnabled(bool(fw) and fw != FW_VERSION and not self.flash)
        self.fw_button.setToolTip('Connect a keyboard first' if not fw else 'It already runs this firmware'
                                  if fw == FW_VERSION else 'Download the firmware from the keyboard maker and install it')
        if self.host:
            fw = f'firmware {self.host.firmware}' if self.host.firmware else 'waiting for heartbeat'
            self.status.setText(f'Connected on {self.port} · {self.host.model or "HM80"} {fw} · '
                                f'{self.host.pulled} key records synced')
        else:
            self.status.setText(self.status_text)

    # ---------- live key travel (sent in every heartbeat) ----------
    def show_realtime(self):
        """Queue the heartbeats' key travel. The animation timer plays it back smoothly, a moment late."""
        if not self.host or not self.host.reports:
            return
        reports, self.host.reports = self.host.reports, []
        self.travel.push(time.monotonic(),
                         [r if r and r[0] in self.board.keys else None for r in reports])
        self.switch.set_tester(self.host.fluct, self.host.release_at)
        series, self.host.series = self.host.series, []
        self.graphs.push(series)
        if not self.anim.isActive():
            self.anim.start()
        self.animate()

    def animate(self):
        now = time.monotonic()
        live = self.travel.frame(now)
        if not self.travel.active(now):
            self.anim.stop()                                        # caught up: nothing more to draw until a key moves
        if live is None or live == self._shown:
            return
        self._shown = live
        key, on, depth, deepest = live
        self.rest_key = key
        if self.capturing is not None and on:                     # a picker was waiting for this key
            self.picked_by_press(key)
        state = self.gauge_state(key, depth, on, deepest)
        self.switch.set_state(state)
        moving = depth > 0 or on                                    # else back at rest; the last key stays shown
        self.board.set_live((key, max(0, min(depth, state[4])) / state[4], on) if moving else None)
        self.show_reactive()

    def show_reactive(self):
        """Give the drawn screen's reactive page the key being pressed and its depth."""
        key, on, depth, _ = self._shown or (None, False, 0, 0)
        if key is None or not (depth > 0 or on):                    # a key is shown only while it is down
            return self.board.set_reactive('', 0, 0)
        name, _, _, _, travel, _, _ = self.gauge_state(key)
        if len(name) == 1 and name.isalpha() and not caps_lock():  # letters are lowercase unless Caps Lock is on
            name = name.lower()
        self.board.set_reactive(name, min(1.0, depth / travel), depth / 1000)

    def follow_page(self):
        """The drawn screen shows the page the keyboard's screen is on."""
        self.board.set_page(self.host.screen_page if self.live else None)
        self.show_reactive()

    def gauge_state(self, key, depth=0, on=False, deepest=0):
        """What the depth gauge shows for a key: its name, depth, whether it is triggered, how deep it went, and its
        travel, actuation depth and reset distance from the profile, all in micrometers."""
        act = (self.profile.get(self.layer, key, 'init') + self.profile.get(self.layer, key, 'press')) * 10
        return (self.key_name(key), depth, on, deepest, self.profile.get(0, key, 'travel') * 10, act,
                self.profile.get(self.layer, key, 'release') * 10)

    def show_rest(self):
        """With no key moving, keep the gauge showing a key at rest, so it is there from the moment the program opens:
        the key last pressed, else the first one selected, else the first key. Its numbers follow the profile."""
        state = self.switch.state
        if self.travel.active(time.monotonic()) or state and (state[1] > 0 or state[2]):
            return                                                  # a key is moving: the animation draws it
        key = self.rest_key if self.rest_key is not None else next(iter(self.selected()), min(self.board.keys))
        self.switch.set_state(self.gauge_state(key))

    # ---------- per-key performance ----------
    def set_sliders(self, i, p, r):
        self.loading_sliders = True
        for f, v in (('init', i), ('press', p), ('release', r)):
            self.sliders[f].setValue(v)
        self.loading_sliders = False

    def selection_changed(self):
        for button, keys in self.set_buttons:
            button.setChecked(self.board.selected == keys)
        keys = self.selected()
        if self.auto_key is not None and keys != [self.auto_key]:
            self.auto_key = None                                    # no longer just what a key press chose
        if keys:
            self.set_sliders(*(self.profile.get(self.layer, keys[0], f) for f in ('init', 'press', 'release')))
            self.show_pending()
            self.load_mode(keys[0])
        self.show_rest()

    def sliders_moved(self, _=None):
        if not self.loading_sliders:
            self.show_pending()

    def sliders_problem(self, keys):
        """Why the sliders' values cannot be applied to `keys`, or None if they can."""
        i, p, r = (self.sliders[f].value() for f in ('init', 'press', 'release'))
        if p < 1 or r < 1 or i + p < 10:
            return 'Actuation and reset must be at least 0.01 mm, and initial + actuation at least 0.10 mm.'
        travel = min(self.profile.get(0, k, 'travel') for k in keys or self.board.keys)
        if i + p > travel:
            return (f'Initial + actuation is {mm(i + p)} mm, deeper than the key travel ({mm(travel)} mm on stock '
                    f'switches). Lower one of them.')
        if r > travel:
            return f'Reset is {mm(r)} mm, deeper than the key travel ({mm(travel)} mm on stock switches).'

    def show_pending(self):
        """The sliders only edit a form: nothing changes on the keys, the gauge or the keyboard until Apply."""
        keys = self.selected()
        values = tuple(self.sliders[f].value() for f in ('init', 'press', 'release'))
        if problem := self.sliders_problem(keys):
            self.note(f'{problem} Apply will refuse this.')
        elif not keys:
            self.note('Select the keys to change on the keyboard above, then press Apply to selected keys.')
        elif any(tuple(self.profile.get(self.layer, k, f) for f in ('init', 'press', 'release')) != values
                 for k in keys):
            self.note('Not applied yet. Press Apply to selected keys. The gauge and the keyboard keep their current '
                      'values until then.')
        else:
            self.note('')

    def apply_performance(self):
        keys = self.selected()
        if not keys:
            return self.note('Select one or more keys first.')
        if problem := self.sliders_problem(keys):
            return self.note(problem)
        i, p, r = (self.sliders[f].value() for f in ('init', 'press', 'release'))
        for k in keys:
            for f, v in (('init', i), ('press', p), ('release', r)):
                self.profile.set(self.layer, k, f, v)
        self.refresh()
        warn = ' Values under 0.20 mm are very sensitive and may chatter.' if p < 20 or r < 20 else ''
        self.note(f'Applied to {len(keys)} key(s) on {self.profile.name(self.layer)}. {self.when_synced()}{warn}')

    def when_synced(self):
        return 'The keyboard picks it up within a second.' if self.host else 'It is sent when the keyboard connects.'

    # ---------- what keys do (per-key table, like the performance settings) ----------
    def set_actions(self, source, done):
        """Give the selected keys on this layer the mode and both actions from source(key) -> (mode, a, b)."""
        keys = self.selected()
        if not keys:
            return self.keys_note.setText('Select one or more keys first.')
        for k in keys:
            for field, value in zip(('mode', 'action', 'action2'), source(k)):
                self.profile.set(self.layer, k, field, value)
        self.refresh()
        self.keys_note.setText(f'{done} {len(keys)} key(s) on {self.profile.name(self.layer)}. {self.when_synced()}')

    def assign_key(self, action, name):                 # a plain key: no tap/hold or other second action
        self.set_actions(lambda k: (0, action, b'\x00\xff\xff'), f'{name} set on')

    def restore_keys(self):
        self.set_actions(lambda k: (FACTORY.get(self.layer, k, f) for f in ('mode', 'action', 'action2')),
                         'Restored')

    def chosen_mode(self):
        return self.mode_tabs.tabData(self.mode_tabs.currentIndex())

    @property
    def picked(self):
        """The keys picked on the mode tab shown, [A, B]. Every tab has its own, so a key picked on one tab is not
        shown on the others."""
        return self._picks.setdefault(self.chosen_mode(), [NO_ACTION, NO_ACTION])

    def mode_tab_changed(self, _=None):
        self.capture(None)                                      # stop waiting for a key, and show this tab's picks
        self.show_mode_form()

    def show_mode_form(self, _=None):
        """Show only the settings the chosen mode uses, named for what they do in it. The SOCD tab shows its pairs."""
        socd = self.chosen_mode() == 'socd'
        name, *labels, jitter, about = KEY_MODES[0 if socd else self.chosen_mode()]
        for (label, field), text in zip(self.mode_rows, labels):
            label.setText(text or '')
            self.mode_form.setRowVisible(field, text is not None and not socd)
        self.mode_form.setRowVisible(self.jitter, jitter and not socd)
        for row in (self.mode_buttons, self.mode_note):
            self.mode_form.setRowVisible(row, not socd)
        self.mode_form.setRowVisible(self.socd_box, socd)
        self.mode_about.setText('While both keys of a pair are held, the last one pressed wins. Releasing it brings '
                                'the other back. Select two keys above, then bind them. Two pairs per layer, on '
                                'layers 1–3. Both keys must have the same travel. Needs the keyboard connected.'
                                if socd else about)
        self.mode_reset.setToolTip('Put the selected keys back to their factory key' if self.chosen_mode() == 0 else
                                   f'Take {name} off the selected keys. They stay ordinary keys sending the same key')

    def capture(self, which):
        """Make picker `which` (0: key A, 1: key B) wait for a key to be pressed on the keyboard; None stops waiting.
        While it waits, key presses do nothing in the window, so the key pressed cannot also click or type."""
        if which is not None and not self.live:
            which = None
            self.mode_note.setText('Connect the keyboard to pick a key by pressing it.')
        self.capturing = which
        app = QApplication.instance()
        if which is None:
            app.removeEventFilter(self)
        else:
            app.installEventFilter(self)
        for i, b in enumerate(self.pickers):
            b.setChecked(i == which)
            action = self.picked[i]
            b.setText('Press a key on the keyboard…' if i == which else
                      KEY_NAMES.get(action[:2]) or action_label(action) or 'Select A Key')

    def eventFilter(self, obj, event):                  # only installed while a picker waits for a key
        return event.type() in (QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride)

    def pick(self, which, action):
        self.picked[which] = action
        self.capture(None)

    def auto_selected(self):
        """True while the one key selected is a key that a press chose for itself, not one the user selected."""
        return self.auto_key is not None and self.selected() == [self.auto_key]

    def picked_by_press(self, key):
        """A picker got its key from a press on the keyboard. With no keys selected yet, the key pressed is also the
        one to change: it is selected on the drawn keyboard, so Apply can go ahead. That selection is only provisional:
        pressing another key for the same picker moves it (and so does the first picker's key, if the second one
        chose it), until the user selects keys themselves. Selecting fills the form from the key, so the mode tab and
        the other picker's key are kept as they were."""
        which, tab, other = self.capturing, self.mode_tabs.currentIndex(), self.picked[1 - self.capturing]
        if not self.selected() or self.auto_selected() and which <= self.auto_from:
            self.select((key,))
            self.selection_changed()
            self.mode_tabs.setCurrentIndex(tab)
            self.picked[1 - which] = other
            self.auto_key, self.auto_from = key, which
        self.pick(which, FACTORY.get(0, key, 'action'))

    def load_mode(self, k):
        """Fill the form with what key k does on this layer: its mode, count, time and jitter. The keys to send are
        not filled in: they are always picked afresh, so the pickers say "Select A Key" until then."""
        get = lambda f: self.profile.get(self.layer, k, f)
        tab = next((i for i in range(self.mode_tabs.count()) if self.mode_tabs.tabData(i) == get('mode')), None)
        if tab is not None and self.chosen_mode() != 'socd':      # (on SOCD, keys are selected to bind them)
            self.mode_tabs.setCurrentIndex(tab)
        self._picks.clear()
        self.capture(None)
        self.count.setValue(get('count'))
        self.time.setValue(get('time') & ~JITTER)
        self.jitter.setChecked(bool(get('time') & JITTER))
        self.show_mode_form()
        self.mode_note.setText('' if tab is not None else 'This key is in a SOCD pair kept in the key itself (the official '
                               'app\'s old "Cover" mode, which cancels its opposite key). Applying a mode here '
                               'replaces it.')

    def apply_mode(self):
        self.capture(None)
        keys, m = self.selected(), self.chosen_mode()
        if not keys:
            return self.mode_note.setText('Select one or more keys first.')
        name, a_label, b_label, count_label, time_label, jitter, _ = KEY_MODES[m]
        for action, label in zip(self.picked, (a_label, b_label)):
            if label and action[1] == 0xFF:
                return self.mode_note.setText(f'Pick the {label.lower()} key first.')
        a, b = self.picked[0], self.picked[1] if b_label else NO_ACTION
        for k in keys:
            for f, v in (('mode', m), ('action', a), ('action2', b)):
                self.profile.set(self.layer, k, f, v)
            if count_label:
                self.profile.set(self.layer, k, 'count', self.count.value())
            if time_label:
                self.profile.set(self.layer, k, 'time',
                                 self.time.value() | (JITTER if jitter and self.jitter.isChecked() else 0))
        self.refresh()
        self.mode_note.setText(f'{name} set on {len(keys)} key(s) on {self.profile.name(self.layer)}. '
                               f'{self.when_synced()}')

    def reset_mode(self):
        """On the Press tab: the selected keys go back to their factory key. On another mode's tab: the selected keys
        that use that mode lose it, and stay ordinary keys sending the same key."""
        self.capture(None)
        keys, m = self.selected(), self.chosen_mode()
        name = KEY_MODES[m][0]
        if not keys:
            return self.mode_note.setText('Select one or more keys first.')
        if m == 0:
            for k in keys:
                for f in ('mode', 'action', 'action2', 'count', 'time'):
                    self.profile.set(self.layer, k, f, FACTORY.get(self.layer, k, f))
            done = f'{len(keys)} key(s) back to their factory key'
        else:
            keys = [k for k in keys if self.profile.get(self.layer, k, 'mode') == m]
            if not keys:
                return self.mode_note.setText(f'None of the selected keys use {name}.')
            count_label, time_label = KEY_MODES[m][3:5]
            for k in keys:
                self.profile.set(self.layer, k, 'mode', 0)
                self.profile.set(self.layer, k, 'action2', NO_ACTION)
                for field, used in (('count', count_label), ('time', time_label)):
                    if used:    # what the mode set goes back to a plain key's: a mode applied and removed leaves no trace
                        self.profile.set(self.layer, k, field, FACTORY.get(self.layer, k, field))
            done = f'{name} removed from {len(keys)} key(s)'
        auto = self.auto_selected()
        self.refresh()
        if auto:                                                  # the key was only selected so as to be picked
            self.select(())
            self._picks.clear()
            self.capture(None)
        else:
            self.load_mode(self.selected()[0])
        self.mode_note.setText(f'{done} on {self.profile.name(self.layer)}. {self.when_synced()}')

    # ---------- lighting / globals (keyboard-side state, sent immediately) ----------
    # Holding Fn puts the keyboard on the Fn layer, and the Fn layer has lighting of its own (Cycle, from the factory),
    # so the lighting seems to change whenever Fn is held. With `fn_follows`, the Fn layer is sent the lighting of the
    # layer in use instead, only dimmer (see fn_lighting). Only what the keyboard is sent changes: the lighting stored
    # for the Fn layer stays as it was.
    def layer_in_use(self):
        """The layer the user is on: the one shown, or the one before Fn was held while the Fn layer is shown."""
        if self.layer != FN_LAYER:
            self.base = self.layer
        return self.base

    def fn_mirrors(self):
        """True if the Fn layer is being given the lighting of the layer in use (that can be copied)."""
        return bool(self.live and self.fn_follows and fn_lighting(self.host.lighting[self.layer_in_use()]))

    def mirror_fn(self):
        """Set the lighting the keyboard is to show on the Fn layer: that of the layer in use, or the Fn layer's own
        if it does not follow. True if that changed it, which the caller must send."""
        in_use = self.layer_in_use()                # noted even when not following, so turning it on starts right
        if not self.live:
            return False
        copy = self.fn_follows and fn_lighting(self.host.lighting[in_use])
        target = copy or self.profile.lighting(FN_LAYER)
        self.fn_sent = bytes(target) if copy else None
        if self.host.lighting[FN_LAYER] == target:
            return False
        self.host.lighting[FN_LAYER] = bytearray(target)
        return True

    def follow_fn(self):
        if self.mirror_fn():
            self.send(lighting_packets(self.host.lighting[i] for i in range(LAYERS)))

    def adopt_fn_edit(self):
        """Fn + the lighting keys change the lighting of the layer the keyboard is on, which is the Fn layer while Fn
        is held. While the Fn layer follows, that is an edit of the layer in use, so it goes there. The Fn layer is
        dimmer, so a brightness change goes over as the same step, not as the Fn layer's dimmed value."""
        if self.fn_sent is None or bytes(self.host.lighting[FN_LAYER]) == self.fn_sent:
            return
        in_use, edited = self.layer_in_use(), bytearray(self.host.lighting[FN_LAYER])
        edited[3] = min(255, max(0, self.host.lighting[in_use][3] + edited[3] - self.fn_sent[3]))
        self.host.lighting[in_use] = edited

    def set_fn_follows(self, on):
        self.fn_follows = on
        self.settings.setValue('fn_follows_lighting', on)
        self.settings.sync()
        self.follow_fn()
        self.refresh()

    def commit_light(self, s, layer=None):
        layer = self.layer if layer is None else layer
        self.host.lighting[layer] = s
        self.profile.set_lighting(layer, s)
        self.mirror_fn()                            # sent along with the rest just below
        self.send(lighting_packets(self.host.lighting[i] for i in range(LAYERS)))
        self.refresh()

    def set_light_bits(self, mask, value):
        s = self.light()
        s[0] = (s[0] & ~mask & 0xFF) | value
        self.commit_light(s)

    def set_light_byte(self, i, v):
        s = self.light()
        s[i] = v
        self.commit_light(s)

    def commit_globals(self, g):
        self.host.globals = g
        self.profile.set_globals(g)
        self.send([globals_packet(g)])
        self.refresh()

    def set_global_byte(self, i, v):
        g = self.glob()
        g[i] = v
        self.commit_globals(g)

    def set_global_bits(self, i, mask, value):
        g = self.glob()
        g[i] = (g[i] & ~mask & 0xFF) | value
        self.commit_globals(g)

    def pick_color(self):
        s = self.light()
        c = QColorDialog.getColor(QColor.fromHsv(byte_to_hue(s[1]), s[2], 255), self, 'Backlight color')
        if c.isValid() and self.live:
            s[1], s[2] = hue_to_byte(c.hsvHue()), c.hsvSaturation()
            self.commit_light(s)

    def pick_indicator(self):
        g = self.glob()
        c = QColorDialog.getColor(QColor.fromHsv(byte_to_hue(g[4]), g[5], g[6]), self, 'Indicator color')
        if c.isValid() and self.live:
            g[4], g[5], g[6] = hue_to_byte(c.hsvHue()), c.hsvSaturation(), c.value()
            self.commit_globals(g)

    def layer_menu(self, i, pos):
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.addAction('Rename…', lambda: self.rename_layer(i))
        menu.addSeparator()
        menu.addAction('Export layer…', lambda: self.export_layer(i))
        menu.addAction('Import layer…', lambda: self.import_layer(i))
        menu.popup(self.layer_group.button(i).mapToGlobal(pos))

    def rename_layer(self, i):
        """Turn the layer's button into a text box. An empty name goes back to the factory name."""
        NameEdit(self.layer_group.button(i), self.profile.name(i), LAYER_NAME_MAX).done.connect(
            lambda name: self.finish_rename(i, name))

    def finish_rename(self, i, name):
        if name is None:
            return
        self.profile.set_name(i, name.strip() or Profile.factory().name(i))
        self.refresh()

    def ask_path(self, save, title, name=''):
        """The file box for layer files. '' if it is canceled."""
        home = os.path.expanduser('~')
        if save:
            return QFileDialog.getSaveFileName(self, title, os.path.join(home, name), LAYER_FILTER)[0]
        return QFileDialog.getOpenFileName(self, title, home, LAYER_FILTER)[0]

    def confirm(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def export_layer(self, i):
        """Save one layer (its per-key settings, name and lighting) to a file that can be imported on any layer."""
        name = self.profile.name(i)
        safe = ''.join('_' if c in '\\/:*?"<>|' else c for c in name)       # a name that is fine as a file name
        path = self.ask_path(True, 'Export layer', safe + LAYER_SUFFIX)
        if not path:
            return
        if not path.endswith(LAYER_SUFFIX):
            path += LAYER_SUFFIX
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.profile.export_layer(i), f, ensure_ascii=False, indent=1)
        except OSError as e:
            return self.warn('Export failed', f'Could not write {path}:\n{e.strerror}')
        self.statusBar().showMessage(f'Exported {name} to {path}', 8000)

    def import_layer(self, i):
        """Replace a layer with one from a file. The lighting comes too, but only while the keyboard is connected,
        because the lighting always comes from the keyboard."""
        path = self.ask_path(False, 'Import layer')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            name = Profile.parse_layer(data)[0].strip()
        except OSError as e:
            return self.warn('Import failed', f'Could not read {path}:\n{e.strerror}')
        except ValueError as e:                       # not JSON at all, or not a valid layer file
            problem = str(e) if str(e).startswith(('This', 'The layer')) else 'This is not an HM80 layer file.'
            return self.warn('Import failed', f'{os.path.basename(path)}\n{problem}')
        live, current = self.live, self.profile.name(i)
        if not self.confirm('Import layer', f'Replace the key settings and name of "{current}" with those in '
                            f'{os.path.basename(path)}' + (f' (named "{name}")' if name else '') + '? ' +
                            ('Its lighting is replaced too. ' if live else 'Its lighting is not imported while the '
                             'keyboard is not connected. ') + 'This cannot be undone, so export the layer first if you '
                            'want a copy.'):
            return
        self.profile.import_layer(i, data, lighting=False)
        if live:
            self.commit_light(bytearray.fromhex(data['lighting']), i)       # also tells the keyboard and refreshes
        else:
            self.refresh()
        if i == self.layer:
            self.selection_changed()
        self.statusBar().showMessage(f'Imported {os.path.basename(path)} into {self.profile.name(i)}', 8000)

    def choose_layer(self, i):
        if i != self.layer and not self.settled('switching layers'):
            return self.refresh()                   # stay: the layer button that was clicked goes back to unpressed
        self.layer = i
        self.follow_fn()
        if self.live:                               # like the official app: also switch the keyboard's layer
            self.set_global_byte(0, i)
        self.refresh()
        self.selection_changed()

    def bind_socd(self, pair):
        keys = self.selected()
        if len(keys) != 2:
            return QMessageBox.information(self, 'SOCD', 'Select exactly two keys first.')
        a, b = keys
        if self.profile.get(0, a, 'travel') != self.profile.get(0, b, 'travel'):
            return QMessageBox.information(self, 'SOCD', 'Both keys must have the same travel.')
        other = self.glob()[9 + self.layer * 4 + (1 - pair) * 2:][:2]
        if a in other or b in other:
            return QMessageBox.information(self, 'SOCD', 'One of these keys is already in the other pair.')
        self.set_socd(pair, a, b)

    def set_socd(self, pair, a, b):
        g = self.glob()
        o = 9 + self.layer * 4 + pair * 2
        g[o], g[o + 1] = a, b
        self.commit_globals(g)

    # ---------- screen image ----------
    def set_upload_buttons(self):
        self.upload_btn.setEnabled(self.screen_data is not None and not self.uploading and self.live)
        self.upload_btn.setToolTip('' if self.live else 'Connect the keyboard to upload.')
        self.cancel_btn.setEnabled(self.uploading)
        self.gallery.setEnabled(not self.uploading)
        self.gallery_refresh.setEnabled(not self.uploading)

    def screen_clicked(self):
        self.tabs.setCurrentIndex(self.screen_tab)
        if not self.uploading:
            self.choose_screen_image()

    def fill_gallery(self, select=None):
        self.gallery.clear()
        os.makedirs(GALLERY, exist_ok=True)
        for name in sorted(os.listdir(GALLERY), key=str.lower):
            path = os.path.join(GALLERY, name)
            image = QImage(path)                        # a GIF gives its first frame
            if image.isNull():
                continue
            item = QListWidgetItem(QIcon(QPixmap.fromImage(image.scaled(
                *GALLERY_ICON, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))),
                os.path.splitext(name)[0])
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.gallery.addItem(item)
            if path == select:
                self.gallery.setCurrentItem(item)
        self.gallery_remove.setEnabled(bool(self.gallery.selectedItems()))

    def refresh_gallery(self):
        """Pick up images added to (or removed from) the gallery folder while the app is open."""
        item = self.gallery.currentItem()
        self.fill_gallery(select=item and item.data(Qt.ItemDataRole.UserRole))

    def pick_from_gallery(self, item):
        if not self.uploading:
            self.load_screen_image(item.data(Qt.ItemDataRole.UserRole))

    def remove_from_gallery(self):
        item = self.gallery.currentItem()
        if not item:
            return
        if not QFile.moveToTrash(item.data(Qt.ItemDataRole.UserRole)):
            return QMessageBox.warning(self, 'Delete', f'Could not move "{item.text()}" to the trash.')
        self.fill_gallery()

    def choose_screen_image(self):
        path = QFileDialog.getOpenFileName(self, 'Choose image or GIF', os.path.expanduser('~'),
                                           'Images (*.gif *.png *.jpg *.jpeg *.bmp)')[0]
        if path and self.load_screen_image(path):
            self.fill_gallery(select=gallery_add(path))

    def load_screen_image(self, path):
        """Convert an image for the keyboard and preview it. Returns False if it cannot be used."""
        try:
            self.screen_data, self.screen_announce, summary = convert_screen_image(path)
            self.screen_path = path
        except ValueError as e:
            self.screen_data = None
            self.screen_info.setText(f'Cannot use this file ({e}).')
            self.set_upload_buttons()
            return False
        self.screen_info.setText(f'{os.path.basename(path)} ({summary})')
        self.board.show_screen(path)                    # the drawn keyboard shows it too, as a preview, from now on
        self.board.set_page(PAGE_USER)
        if self.movie:
            self.movie.stop()
        if path.lower().endswith('.gif'):
            self.movie = QMovie(path)
            first = QImage(path).size()
            self.movie.setScaledSize(first.scaled(SCREEN_W, SCREEN_H, Qt.AspectRatioMode.KeepAspectRatio)
                                     if first.width() > SCREEN_W or first.height() > SCREEN_H else first)
            self.preview.setMovie(self.movie)
            self.movie.start()
        else:
            self.movie = None
            pix = QPixmap(path)
            if pix.width() > SCREEN_W or pix.height() > SCREEN_H:
                pix = pix.scaled(SCREEN_W, SCREEN_H, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self.preview.setPixmap(pix)
        self.screen_note.setText('Shown on the drawn keyboard as a preview. It goes on the keyboard itself when you '
                                 'upload it.')
        self.progress.setValue(0)
        self.set_upload_buttons()
        return True

    def show_saved_screen(self):
        """The drawn keyboard's screen plays what the keyboard holds: the last image uploaded with this program."""
        self.board.show_screen(next(iter(glob.glob(SCREEN_COPY + '.*')), None))

    def start_upload(self):
        if not self.live or self.screen_data is None:
            return
        self.uploading = True
        self.progress.setRange(0, len(self.screen_data))
        self.progress.setValue(0)
        # The keyboard erases its storage before it starts pulling (~1 MB/s of the announced size).
        erase, transfer = 1 + self.screen_announce / 900_000, len(self.screen_data) / 15_000
        self.screen_note.setText(f'Preparing the keyboard\'s storage (about {erase:.0f} s), then uploading '
                                 f'(about {transfer / 60:.0f} min). Keep the keyboard plugged in.')
        self.send([self.host.start_screen_upload(self.screen_data, self.screen_announce)])
        self.set_upload_buttons()

    def cancel_upload(self):
        if self.host:
            self.send([self.host.cancel_screen_upload()])
        self.uploading = False
        self.screen_note.setText('Upload canceled.')
        self.show_saved_screen()                        # the keyboard did not get the previewed image
        self.follow_page()
        self.set_upload_buttons()

    def screen_progress(self):
        if not self.uploading or not self.host:
            return
        if self.host.screen_done:
            self.uploading = False
            self.progress.setValue(self.progress.maximum())
            self.screen_note.setText('Uploaded. The image is on the keyboard.')
            for old in glob.glob(SCREEN_COPY + '.*'):           # keep a copy so the drawn screen can play it
                os.remove(old)
            copy = SCREEN_COPY + os.path.splitext(self.screen_path)[1].lower()
            shutil.copyfile(self.screen_path, copy)
            self.board.show_screen(copy)
            self.set_upload_buttons()
        elif self.host.screen_sent:
            self.progress.setValue(self.host.screen_sent)
            left = (len(self.screen_data) - self.host.screen_sent) / 15_000
            self.screen_note.setText(f'Uploading… about {left / 60:.0f} min left. Keep the keyboard plugged in.'
                                     if left >= 60 else f'Uploading… about {left:.0f} s left.')

    # ---------- profiles ----------
    def use_profile(self, profile):
        """Make `profile` what the keyboard and this window go by."""
        self.profile = profile
        if self.host:
            self.host.profile = profile             # new CRCs: the keyboard pulls what differs
        if self.live:                               # make lighting/globals match the profile too
            for i in range(LAYERS):
                self.host.lighting[i] = profile.lighting(i)
            self.host.globals = profile.globals()
            self.mirror_fn()
            self.send(lighting_packets(self.host.lighting[i] for i in range(LAYERS)) + [globals_packet(self.host.globals)])

    def replace_profile(self, profile):
        self.use_profile(profile)
        self.layer = self.glob()[0] if self.glob()[0] < LAYERS else 0
        self.follow_fn()
        self.refresh()

    def import_profile(self):
        path = QFileDialog.getOpenFileName(self, 'Import profile', os.path.expanduser('~'),
                                           'Profile (*.hmc)')[0]
        if path:
            try:
                self.replace_profile(Profile.load(path))
            except (OSError, ValueError, zlib.error) as e:
                QMessageBox.warning(self, 'Import failed', f'Could not read {path}:\n{e}')

    def export_profile(self):
        path = QFileDialog.getSaveFileName(self, 'Export profile', os.path.expanduser('~/config.m14.hmc'),
                                           'Profile (*.hmc)')[0]
        if path:
            self.profile.save(path)

    def factory_reset(self):
        if QMessageBox.question(self, 'Restore factory defaults',
                                'Replace all key settings, lighting and SOCD pairs with factory defaults?'
                                ) == QMessageBox.Yes:
            self.replace_profile(Profile.factory())

    # ---------- serial link ----------
    def poll(self):
        if self.fd is not None or self.blocked:
            return self.refresh_status()
        port = find_port()
        if not port:
            self.status_text = 'Keyboard not found. Waiting…'
            return self.refresh_status()
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            self.status_text = f'No permission to open {port}. Install the udev rule from the README, then replug.'
            return self.refresh_status()
        except OSError as e:
            self.status_text = f'Cannot open {port}: {e.strerror} (is another program using it?)'
            return self.refresh_status()
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)       # only one program may answer the keyboard
            tty.setraw(fd)
        except OSError:
            os.close(fd)
            return
        self.fd, self.port, self.host = fd, port, Host(self.profile)
        self.host.flash = self.flash                # a flash that was interrupted carries on when the keyboard is back
        for v in self.settings.value('firmware_ok', [], type=list):     # versions the user already agreed to
            self.host.accepted.add(tuple(map(int, v.split('.'))))
        self.calibration_page()
        self.notifier = QSocketNotifier(fd, QSocketNotifier.Read, self)
        self.notifier.activated.connect(self.on_read)
        self.refresh_status()

    def ask_untested(self, version):
        self.notifier.setEnabled(False)             # the keyboard keeps sending while the question is open
        text = '.'.join(map(str, version))
        while True:
            box = QMessageBox(QMessageBox.Question, 'Untested firmware', UNTESTED_MSG.format(text), parent=self)
            carry = box.addButton('Carry on', QMessageBox.YesRole)
            install = box.addButton(f'Install firmware {".".join(map(str, FW_VERSION))}…', QMessageBox.ActionRole)
            box.setDefaultButton(box.addButton('Disconnect', QMessageBox.NoRole))
            box.exec()
            if box.clickedButton() is install and not self.offer_flash(f'The keyboard runs firmware {text}.'):
                continue                            # it was not installed: ask again
            if box.clickedButton() is carry:
                self.host.accepted.add(version)
                known = self.settings.value('firmware_ok', [], type=list)      # asked once per version, not at every start
                self.settings.setValue('firmware_ok', sorted({*known, text}))
            elif box.clickedButton() is not install:
                self.blocked = True
                return self.disconnect()
            break
        self.notifier.setEnabled(True)

    # ---------- firmware install ----------
    def offer_flash(self, why):
        """Ask first, then download and check the image. True once the flash is armed: it starts with the keyboard's next
        packet, and nothing has been sent to the keyboard before that."""
        box = QMessageBox(QMessageBox.Warning, f'Install firmware {".".join(map(str, FW_VERSION))}',
                          f'{why}\n\n{FLASH_MSG}', parent=self)
        install = box.addButton('Download and install', QMessageBox.AcceptRole)
        box.setDefaultButton(box.addButton('Not now', QMessageBox.RejectRole))
        box.exec()
        if box.clickedButton() is not install:
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)       # blocks the window for the download (20 s at most)
        try:
            image = fetch()
        except FlashError as e:
            QMessageBox.warning(self, 'Firmware not installed', str(e))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.flash, self.blocked = Flash(image), False
        if self.host:
            self.host.flash = self.flash
        d = self.flash_dialog = QProgressDialog('', None, 0, 100, self)       # None: no Cancel button
        d.setWindowTitle(f'Installing firmware {".".join(map(str, FW_VERSION))}')
        d.setWindowModality(Qt.WindowModal)
        d.setWindowFlag(Qt.WindowCloseButtonHint, False)
        d.setMinimumDuration(0)
        d.setAutoClose(False)
        d.setAutoReset(False)
        self.show_flash()
        self.refresh_status()                       # the Install button is off from now on
        return True

    def show_flash(self):
        f = self.flash
        step = ('Waiting for the keyboard.' if not f.served else 'Installing.' if not f.sent_all
                else 'Restarting the keyboard.')
        self.flash_dialog.setLabelText(f'{step} Do not unplug the keyboard or close this app.')
        self.flash_dialog.setValue(f.percent)
        self.flash_dialog.show()

    def flash_progress(self):
        if not self.flash.done:
            return self.show_flash()
        self.flash = None
        self.flash_dialog.close()
        self.flash_dialog = None
        self.refresh_status()
        QTimer.singleShot(0, lambda: QMessageBox.information(
            self, 'Firmware installed', f'The keyboard now runs firmware {".".join(map(str, FW_VERSION))}.'))

    def flash_failed(self, why):
        self.flash = None
        if self.flash_dialog:
            self.flash_dialog.close()
            self.flash_dialog = None
        self.blocked = True
        self.disconnect()
        QMessageBox.critical(self, 'Firmware not installed',
                             f'{why}\n\nThe official HM Driver on native Windows can install the firmware instead. '
                             'Never use a virtual machine for that.')

    def install_firmware(self):
        if not self.host or not self.host.fw or self.flash:
            return
        self.notifier.setEnabled(False)             # the keyboard keeps sending while the question is open
        self.offer_flash(f'The keyboard runs firmware {self.host.firmware}.')
        if self.notifier:
            self.notifier.setEnabled(True)

    def on_read(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            return self.disconnect()
        if not data:
            return self.disconnect()
        try:
            replies = self.host.feed(data)
        except FirmwareRequest:
            self.notifier.setEnabled(False)         # the keyboard keeps sending while the question is open
            if self.offer_flash(STUCK_MSG):
                return self.notifier.setEnabled(True)
            self.blocked = True
            return self.disconnect()
        except FlashError as e:
            return self.flash_failed(str(e))
        except UntestedFirmware as e:
            return self.ask_untested(e.version)
        self.send(replies, own=True)
        if self.flash:
            self.flash_progress()
        self.screen_progress()
        self.show_realtime()
        if self.host.cali_done or len(self.host.cali) == NKEYS and not self.cali_note.text():
            self.show_calibration()                 # a key was just calibrated, or the table has all arrived
        if self.live and self.host.state_rev != self.seen_rev:
            self.seen_rev = self.host.state_rev
            self.layer = self.host.globals[0] if self.host.globals[0] < LAYERS else self.layer
            self.adopt_fn_edit()
            kept = [i for i in range(LAYERS) if i != FN_LAYER or not self.fn_follows]     # (the Fn layer's is ours)
            changed = self.profile.globals() != self.host.globals or any(
                self.profile.lighting(i) != self.host.lighting[i] for i in kept)
            if changed:                             # keep the profile in step with the keyboard
                unsaved = self.dirty                # a change on the keyboard itself (Fn keys) is not one of ours
                for i in kept:
                    self.profile.set_lighting(i, self.host.lighting[i])
                self.profile.set_globals(self.host.globals)
                if not unsaved:                     # so with nothing else waiting to be saved, it is simply stored
                    self.store()
            self.follow_fn()
            self.follow_page()
            self.refresh()
        else:
            self.refresh_status()

    def send(self, packets, own=False):
        if self.flash and not own:                  # while the firmware goes over, only its own packets are sent
            return
        for p in packets:
            try:
                os.write(self.fd, p)
            except BlockingIOError:
                pass                                # the keyboard re-requests on its next heartbeat
            except OSError:
                return self.disconnect()

    def disconnect(self):
        if self.notifier:
            self.notifier.setEnabled(False)
            self.notifier.deleteLater()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = self.notifier = self.host = None
        self.seen_rev, self._shown, self.fn_sent = -1, None, None
        self.calibrating = False
        self.show_calibration()
        self.anim.stop()
        self.travel.clear()
        self.board.set_live(None)
        self.board.set_page(None)
        self.switch.set_state(None)
        self.switch.set_tester(0, 0)
        self.graphs.clear()
        self.status_text = 'Keyboard disconnected. Waiting…'
        if self.flash:
            self.show_flash()
        if self.uploading:
            self.uploading = False
            self.screen_note.setText('Upload interrupted because the keyboard disconnected.')
            self.show_saved_screen()                    # it never got the previewed image
            self.set_upload_buttons()
        self.refresh()


def first_run():
    box = QMessageBox(QMessageBox.Question, 'HM80 Configurator first run',
                      'The HM80 never reports its settings for each key (actuation, rapid trigger, key mapping) back to '
                      'the computer, so this app keeps the master copy. When the keyboard connects, it takes any '
                      'settings for each key that differ from that copy.\n\n'
                      'If you used the official HM Driver on Windows, import its profile '
                      '(Documents\\config.m14.hmc) to keep your settings. Otherwise start from factory defaults, '
                      'which match a keyboard that was never customized.')
    imp = box.addButton('Import Windows profile…', QMessageBox.AcceptRole)
    fac = box.addButton('Factory defaults', QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.exec()
    if box.clickedButton() is fac:
        return Profile.factory()
    if box.clickedButton() is imp:
        path = QFileDialog.getOpenFileName(None, 'Import profile', os.path.expanduser('~'),
                                           'Profile (*.hmc)')[0]
        if path:
            try:
                return Profile.load(path)
            except (OSError, ValueError, zlib.error) as e:
                QMessageBox.warning(None, 'Import failed', f'Could not read {path}:\n{e}')
    return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('hm80-linux')
    app.setDesktopFileName('hm80-linux')            # ties the window to its launcher / taskbar entry
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), 'icon.svg')))
    try:
        profile = Profile.load(CONFIG)
    except FileNotFoundError:
        profile = first_run()
    except (OSError, ValueError, zlib.error) as e:
        QMessageBox.warning(None, 'Profile unreadable', f'{CONFIG} could not be read ({e}).')
        profile = first_run()
    if profile is None:
        return
    profile.save(CONFIG)
    w = MainWindow(profile)
    w.show()
    sys.exit(app.exec())
