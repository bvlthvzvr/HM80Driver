# Run: pytest. Packets below were captured from a real HM80 and the official app.
import json
import struct

import pytest

from hm80.protocol import (ACTIONS, FN_DIM, FN_LAYER, FUNCTION_GROUPS, JITTER, KNOWN_FIRMWARE, SW_VERSION,
                           FirmwareRequest, Host, LAYERS, NKEYS, Profile, UntestedFirmware, action_label, fn_lighting,
                           globals_packet, lighting_packets, pkt, screen_file)

APP_HELLO = bytes.fromhex('01020509420409 5cad4a3398783b249e806f2aa8806f2aa84f95877000000050'.replace(' ', ''))
APP_LIGHT0 = bytes.fromhex('1600bc00efff26' + '00' * 24 + 'e6')
APP_GLOBALS = bytes.fromhex('1700008501' '00ffff2302' + 'ff' * 12 + '00' * 9 + 'b4')
KB_HELLO = bytes.fromhex('016d31346100020509 64' + 'ff' * 12 + '000000000000cc6500cd'.replace(' ', ''))


def test_packets_match_official_app():
    assert pkt(APP_HELLO[:31]) == APP_HELLO
    assert lighting_packets([APP_LIGHT0[2:7]])[0] == APP_LIGHT0
    assert globals_packet(APP_GLOBALS[1:22]) == APP_GLOBALS


def test_profile_roundtrip_and_edit():
    p = Profile.factory()
    assert Profile(p.to_hmc()).b == p.b
    assert p.get(0, 47, 'press') == 200 and p.get(0, 47, 'travel') == 330
    before = p.crcs()
    p.set(0, 47, 'press', 300)
    after = p.crcs()
    assert after[0] != before[0] and after[1:] == before[1:]


def test_key_mode_fields_sit_where_the_official_app_puts_them():
    # The official app set this on a real key: Space on layer 2, Rapid fire every 8 ms.
    rec = bytes.fromhex('4a01' '00' 'c800' 'c800' '04' '002cff' '00ffff' '0a00' '0800')
    p = Profile.factory()
    o = p._off(1, 75)
    p.b[o:o + 18] = rec
    assert (p.get(1, 75, 'mode'), p.get(1, 75, 'action'), p.get(1, 75, 'count'), p.get(1, 75, 'time')) == \
        (4, b'\x00\x2c\xff', 10, 8)
    p.set(1, 75, 'time', 8 | JITTER)
    assert bytes(p.b[o + 16:o + 18]) == b'\x08\x80'


def test_copy_is_independent_and_same_settings_ignores_only_the_active_layer():
    p = Profile.factory()
    c = p.copy()
    assert c is not p and c.b == p.b and p.same_settings(c)
    g = p.globals()
    g[0] = 3                                                    # the keyboard moved to another layer: not a setting
    p.set_globals(g)
    assert p.same_settings(c) and p.b != c.b
    g[3] ^= 1                                                   # the indicator light: a setting
    p.set_globals(g)
    assert not p.same_settings(c)
    p = Profile.factory()
    c = p.copy()
    p.set(0, 47, 'press', 300)
    assert not p.same_settings(c) and c.get(0, 47, 'press') == 200      # editing one never touches its copy
    p.set(0, 47, 'press', 200)
    assert p.same_settings(c)                                   # put back by hand: nothing left to save
    p.set_name(2, 'Work')
    assert not p.same_settings(c)


def test_every_function_on_the_factory_fn_layer_can_be_put_on_a_key():
    offered = {(kind, code) for _, kind, codes in FUNCTION_GROUPS for code in codes}
    offered |= {(2, code) for code in ACTIONS[2]} | {(0, 0x46)}          # the media functions, and PrtSc (Screenshot)
    factory = Profile.factory()
    wanted = {tuple(factory.get(FN_LAYER, k, 'action')[:2]) for k in range(NKEYS)      # what a key does on the Fn layer
              if factory.get(FN_LAYER, k, 'action')[:2] != factory.get(0, k, 'action')[:2]}    # instead of its legend
    wanted = {a for a in wanted if a[1] != 0xFF}                                       # (not "does nothing")
    assert len(wanted) > 20 and not wanted - offered, sorted(action_label(bytes(a) + b'\xff') for a in wanted - offered)
    names = [(kind, code) for _, kind, codes in FUNCTION_GROUPS for code in codes]
    assert len(names) == len(set(names))                                  # none twice
    assert all(code in ACTIONS[kind] for kind, code in names)             # and every one has a name
    assert not {(k, c) for k, c in names if k == 2} and (0, 0x46) not in names     # none that the media row has already


def test_fn_layer_copies_the_lighting_of_the_layer_in_use_only_dimmer_unless_it_is_custom():
    p = Profile.factory()
    cycle, pinwheel = p.lighting(FN_LAYER), p.lighting(2)
    assert cycle != pinwheel
    copy = fn_lighting(pinwheel)
    assert copy is not pinwheel                                      # a copy: editing one never edits the other
    assert copy[:3] == pinwheel[:3] and copy[4] == pinwheel[4]       # the same effect, color and speed...
    assert copy[3] == round(pinwheel[3] * FN_DIM) < pinwheel[3]      # ...at a lower brightness
    assert [fn_lighting(bytearray([0x55, 1, 2, b, 4]))[3] for b in (0, 100, 255)] == [0, 40, 102]
    for byte0 in (0x01, 0x80 | 0x05, 0x01 | 0x40):                   # the Custom effect, or custom overlaid on another
        assert fn_lighting(bytearray([byte0, 1, 2, 3, 4])) is None
    assert fn_lighting(bytearray([0x55, 1, 2, 3, 4]))[0] == 0x55     # light bar effects don't matter


def test_host_state_machine():
    host = Host(Profile.factory())
    out = host.feed(KB_HELLO[:10] + KB_HELLO[10:])            # split reads are reassembled
    assert out == [host.hello()] and host.model == 'm14a' and host.firmware == '2.5.9'
    rec = host.feed(pkt(bytes([0x18, 2, 47])))
    assert rec == [pkt(bytes([0x18, 2, 47]) + host.profile.record(2, 47))] and host.pulled == 1
    for i in range(LAYERS):
        host.feed(pkt(bytes([0x16, i, 1, 2, 3, 4, 5])))
    host.feed(pkt(b'\x17' + bytes(range(21))))
    assert host.ready and host.lighting[3] == bytes([1, 2, 3, 4, 5]) and host.globals == bytes(range(21))
    assert host.screen_page == 8
    assert host.feed(KB_HELLO[:31] + b'\x00') == []          # bad checksum: dropped, no reply
    with pytest.raises(FirmwareRequest):
        host.feed(pkt(b'\x14'))


def test_screen_file_and_upload():
    f = screen_file('user.gif', 2, 1, 3, 80, False, bytes(range(12)))
    assert f[:4] == b'\x01\x00\x19\xc0' and f[4:24] == b'user.gif'.ljust(20, b'\0')
    assert struct.unpack_from('<HHHHBI', f, 24) == (2, 1, 3, 80, 0, 12)
    assert struct.unpack_from('<I', f, 37)[0] == sum(f[:37]) and f[41:53] == bytes(range(12)) and len(f) == 256
    host = Host(Profile.factory())
    assert host.start_screen_upload(f, 512) == pkt(b'\x1d\x02' + struct.pack('<I', 512))
    assert host.feed(pkt(b'\x1d\x01')) == [pkt(b'\x1d\x03' + f[4:24] + struct.pack('<I', 256))]
    chunk = host.feed(pkt(b'\x1d\x03' + struct.pack('<IB', 24, 24)))[0]
    assert chunk[:7] == b'\x1d\x04' + struct.pack('<IB', 24, 24) and chunk[7:31] == f[24:48]
    assert host.feed(pkt(b'\x1d\x05')) == [pkt(b'\x1d\x07')] and host.screen_done and host.screen is None


def test_realtime_travel_from_heartbeat():
    host = Host(Profile.factory())
    host.feed(KB_HELLO)
    assert host.realtime is None                              # 0xFF: no key moving
    live = bytes.fromhex('016d3134610002050964' '3001e70c0000e80cc800c8000000000000006c1100')  # captured, S pressed
    host.feed(pkt(live))
    assert host.realtime == (48, True, 3303, 3304)


def test_every_heartbeat_report_is_kept_until_taken():
    host = Host(Profile.factory())
    live = bytes.fromhex('016d3134610002050964' '3001e70c0000e80cc800c8000000000000006c1100')  # captured, S pressed
    host.feed(KB_HELLO + pkt(live) + KB_HELLO)                # three heartbeats in one read
    assert host.reports == [None, (48, True, 3303, 3304), None]
    assert host.realtime is None                              # the latest, as before
    host.reports.clear()
    for _ in range(1000):                                     # nobody takes them: they must not pile up
        host.feed(pkt(live))
    assert len(host.reports) == 256


def test_layer_names_can_be_changed():
    p = Profile.factory()
    crcs = p.crcs()
    p.set_name(1, 'Gaming')
    assert p.name(1) == 'Gaming' and p.name(0) == 'Layer1' and p.name(2) == 'Layer3'    # only that layer changes
    assert p.crcs() == crcs                                   # names are not part of what the keyboard syncs
    p.set_name(1, 'Ünïcode 键盘')
    assert p.name(1) == 'Ünïcode 键盘'
    again = Profile(p.to_hmc())                               # survives saving and loading the .hmc
    assert [again.name(l) for l in range(LAYERS)][1] == 'Ünïcode 键盘'
    p.set_name(1, 'x' * 200)                                  # too long: cut, still null-terminated, next layer intact
    assert p.name(1) == 'x' * 60 and p.name(2) == 'Layer3'
    p.set_name(1, '键' * 30)                                  # a cut never splits a multi-byte character
    assert p.name(1) == '键' * 20
    p.set_name(1, '')
    assert p.name(1) == 'Layer 2'                             # an empty name falls back to the default


def source_layer():
    src = Profile.factory()
    src.set_name(1, 'Gaming')
    for field, value in (('init', 7), ('press', 33), ('release', 44)):
        src.set(1, 5, field, value)
    src.set_lighting(1, bytes.fromhex('0102030405'))
    return src


def test_a_layer_can_be_exported_and_imported():
    src = source_layer()
    data = json.loads(json.dumps(src.export_layer(1)))               # as it goes through a file
    dst = Profile.factory()
    dst.set(3, 10, 'travel', 350)                                    # this layer is calibrated differently
    before = dst.crcs()
    assert dst.import_layer(3, data) == 'Gaming'
    assert dst.name(3) == 'Gaming' and bytes(dst.lighting(3)) == bytes.fromhex('0102030405')
    assert [dst.get(3, 5, f) for f in ('init', 'press', 'release')] == [7, 33, 44]
    for k in range(NKEYS):
        assert dst.record(3, k)[2:] == src.record(1, k)[2:]          # everything but travel came across
    assert dst.get(3, 10, 'travel') == 350 and dst.get(3, 11, 'travel') == 330     # travel stayed this layer's own
    after = dst.crcs()
    assert after[3] != before[3] and [after[i] for i in (0, 1, 2, 4, 5)] == [before[i] for i in (0, 1, 2, 4, 5)]
    assert [dst.name(i) for i in (0, 1, 2, 4, 5)] == ['Layer1', 'Layer2', 'Layer3', 'Layer5', 'Fn']


def test_importing_a_layer_can_leave_the_lighting_and_name_alone():
    data = json.loads(json.dumps(source_layer().export_layer(1)))
    dst = Profile.factory()
    light = bytes(dst.lighting(3))
    dst.import_layer(3, data, lighting=False)
    assert bytes(dst.lighting(3)) == light                           # lighting comes from the keyboard when connected
    data['name'] = '   '
    dst.set_name(3, 'Mine')
    dst.import_layer(3, data)
    assert dst.name(3) == 'Mine'                                     # a blank name in the file keeps the current one


def test_a_layer_file_that_is_not_valid_is_refused_and_changes_nothing():
    good = source_layer().export_layer(1)
    record = bytearray(bytes.fromhex(good['keys'][0]))

    def broken(**changes):
        return {**good, **changes}
    zero_press, bad_mode = bytearray(record), bytearray(record)
    zero_press[3:5] = b'\0\0'
    bad_mode[7] = 9
    cases = [('not a layer file', 'not an HM80 layer file'), ({'format': 'something else'}, 'not an HM80 layer file'),
             (broken(version=2), 'version 2'), (broken(keys='nope'), 'should hold 84 keys'),
             (broken(keys=good['keys'][:-1]), 'should hold 84 keys'),
             (broken(keys=['zz'] + good['keys'][1:]), 'not hex data'),
             (broken(keys=['00' * 17] + good['keys'][1:]), 'should be 18 bytes'),
             (broken(keys=[zero_press.hex()] + good['keys'][1:]), 'key 0 has settings'),
             (broken(keys=[bad_mode.hex()] + good['keys'][1:]), 'key 0 has settings'),
             (broken(lighting='0102'), 'lighting should be 5 bytes'), (broken(lighting=None), 'not hex data'),
             (broken(name=5), 'name is not text')]
    dst = Profile.factory()
    snapshot = bytes(dst.b)
    for data, why in cases:
        with pytest.raises(ValueError, match=why):
            dst.import_layer(2, data)
        assert bytes(dst.b) == snapshot                              # nothing was half-applied


def test_calibration_table_and_live_results():
    host = Host(Profile.factory())
    host.feed(bytes.fromhex('1900020a07df02 0102 2c072103 0202 20075203 0302 2d07af03 0402 2f07fc03 0d'.replace(' ', '')))
    assert host.cali[0] == (2, 1802, 735) and host.cali[4] == (2, 1839, 1020) and host.cali_done == []
    host.feed(bytes.fromhex('191c021d07da02fe' + 'ff' * 23 + '1e'))           # Insert recalibrated, live capture
    assert host.cali[28] == (2, 1821, 730) and host.cali_done == [28]
    assert host.hello()[28] == 0
    host.cali_page = True
    assert host.hello()[28] == 1


def test_factory_profile_sane():
    p = Profile.factory()
    assert [p.name(l) for l in range(LAYERS)] == ['Layer1', 'Layer2', 'Layer3', 'Layer4', 'Layer5', 'Fn']
    assert all(p.get(0, k, 'travel') == 330 for k in range(NKEYS))


def test_key_actions_are_named_like_the_official_app():
    p = Profile.factory()
    label = lambda layer, key: action_label(p.get(layer, key, 'action'))
    assert [label(0, k) for k in (0, 47, 73, 82)] == ['Esc', 'A', 'Win', '↓']
    assert [label(5, k) for k in (0, 28, 29, 71, 79, 60)] == ['', 'Home', 'Page−', 'Bri+', '', 'L1']
    assert action_label(b'\x02\x02\xff') == 'Vol+' and action_label(b'\x04\x11\xff') == 'Fn'
    before = p.crcs()
    p.set(5, 71, 'action', b'\x02\x02\xff')                       # Fn + up = volume up, as a media key
    assert p.record(5, 71)[8:11] == b'\x02\x02\xff' and p.crcs()[5] != before[5] and p.crcs()[:5] == before[:5]


def test_switch_tester_values():
    host = Host(Profile.factory())
    beat = lambda key, on, depth, released=0, deepest=0: pkt(
        b'\x01m14a\x00\x02\x05\x09\x64' + bytes([key, on]) + struct.pack('<hhh', depth, released, deepest))
    for depth in range(0, 900, 100):                           # nine readings are not yet a window of ten
        host.feed(beat(5, 0, depth))
    assert host.fluct == 0
    host.feed(beat(5, 1, 3400, released=900, deepest=3400))
    assert host.fluct == 3400 and host.release_at == 900           # the last ten: 0 ... 800, then 3400
    host.feed(beat(5, 1, 30000, released=-500))                  # clamped as the official app does: -0.1 to 4.2 mm
    assert host.fluct == 4200 - 100 and host.release_at == -100
    host.feed(pkt(KB_HELLO[:31]))                                  # no key moving: nothing to measure
    assert host.fluct == 0 and host.release_at == -100
    assert host.series[-1] == (0, 0, 0) and host.series[-2] == (4200, 1, 4100)                # the graphs' points
    assert host.series[0] == (0, 0, 0) and host.series[1] == (100, 0, 0) and len(host.series) == 12


def test_hello_echoes_the_keyboards_firmware_version():
    beat = lambda fw: pkt(b'\x01m14a\x00' + bytes(fw) + b'\x64' + b'\xff' * 12)
    host = Host(Profile.factory())
    assert host.hello()[1:4] == bytes(SW_VERSION)                       # before it has said anything
    for fw in sorted(KNOWN_FIRMWARE):                                   # a known version gets its own version back
        host = Host(Profile.factory())
        assert host.feed(beat(fw))[0][1:4] == bytes(fw) and host.firmware == '.'.join(map(str, fw))
    host = Host(Profile.factory())
    with pytest.raises(UntestedFirmware) as e:                          # an unknown one: nothing is sent until agreed
        host.feed(beat((2, 5, 8)))
    assert e.value.version == (2, 5, 8) and host.fw is None
    host.accepted.add((2, 5, 8))
    assert host.feed(beat((2, 5, 8)))[0][1:4] == b'\x02\x05\x08'
