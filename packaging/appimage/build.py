#!/usr/bin/env python3
"""Build the HM80 Configurator AppImage: python3 packaging/appimage/build.py [--python 3.12] [--out dist]

Needs, on the build machine: python-appimage (pip install python-appimage), readelf (binutils), git and network access.
It builds a wheel of this checkout, lets python-appimage put it and PySide6 into a Python AppDir, cuts PySide6 down to
what the configurator uses (QtCore, QtGui, QtWidgets and their plugins), and packs the result with appimagetool.
The AppImage needs glibc 2.34 or newer. On X11 it also needs libxcb-cursor (it brings its own copy if the build machine
has one), and the keyboard's udev rule (see "Permission to open the keyboard" in the README) has to be installed once, as
root, on the user's machine.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAME = 'hm80-linux'
OUT_NAME = 'HM80_Configurator-x86_64.AppImage'
UNUSED_PYTHON = ('test', 'idlelib', 'tkinter', 'turtledemo', 'lib2to3', 'ensurepip', 'config-{v}-x86_64-linux-gnu')
KEEP_PLUGINS = {                                # what the configurator needs: the screens it can open on, and images
    'platforms': ['libqxcb.so', 'libqwayland.so', 'libqoffscreen.so', 'libqminimal.so'],
    'platformthemes': ['libqxdgdesktopportal.so'],
    'imageformats': ['libqgif.so', 'libqjpeg.so', 'libqsvg.so'],
    'iconengines': ['libqsvgicon.so'],
}
KEEP_PLUGIN_DIRS = ('platforminputcontexts', 'xcbglintegrations', 'wayland-decoration-client',
                    'wayland-graphics-integration-client', 'wayland-shell-integration', 'generic')


def run(*cmd, **kw):
    print('+', ' '.join(map(str, cmd)))
    subprocess.run(cmd, check=True, **kw)


def find_tool():
    """appimagetool as python-appimage caches it. It only downloads it when it packs an AppImage itself, which we do not
    let it do, so fetch it with the Python that has python-appimage when it is missing."""
    pattern = os.path.expanduser('~/.cache/python-appimage/bin/.appimagetool-*.appdir.x86_64/AppRun')
    if not glob.glob(pattern):
        bindir = os.path.dirname(shutil.which('python-appimage'))
        interpreter = next((p for p in (os.path.join(bindir, 'python3'), os.path.join(bindir, 'python'))
                            if os.path.exists(p)), sys.executable)      # next to the script in a venv, else this one
        run(interpreter, '-c', 'from python_appimage.utils.deps import ensure_appimagetool; ensure_appimagetool()')
    return glob.glob(pattern)[0]


def needed(path):
    out = subprocess.run(['readelf', '-d', path], capture_output=True, text=True).stdout
    return [line.split('[')[1].split(']')[0] for line in out.splitlines() if '(NEEDED)' in line]


def trim(appdir, version):
    """Delete the parts of PySide6 and of Python that the configurator never loads."""
    site = os.path.join(appdir, f'opt/python{version}/lib/python{version}/site-packages')
    pyside, qt = os.path.join(site, 'PySide6'), os.path.join(site, 'PySide6/Qt')
    libs = {}                                    # every shared library by file name
    for base in (pyside, os.path.join(site, 'shiboken6')):
        for root, _, files in os.walk(base):
            libs.update({f: os.path.join(root, f) for f in files if '.so' in f})
    roots = [os.path.join(pyside, f'{m}.abi3.so') for m in ('QtCore', 'QtGui', 'QtWidgets')]
    roots += [p for f, p in libs.items() if p.startswith(os.path.join(site, 'shiboken6'))]
    roots += [os.path.join(qt, 'plugins', kind, n) for kind, names in KEEP_PLUGINS.items() for n in names]
    for d in KEEP_PLUGIN_DIRS:
        roots += [os.path.join(qt, 'plugins', d, f) for f in os.listdir(os.path.join(qt, 'plugins', d))
                  if 'virtualkeyboard' not in f]        # that one needs QML, which goes
    keep, todo = set(), list(roots)
    while todo:                                  # the libraries those need, and so on
        path = todo.pop()
        if path not in keep and os.path.exists(path):
            keep.add(path)
            todo += [libs[n] for n in needed(path) if n in libs]
    for path in libs.values():
        if path not in keep:
            os.remove(path)
    for d in os.listdir(os.path.join(qt, 'plugins')):
        if d not in KEEP_PLUGIN_DIRS and d not in KEEP_PLUGINS:
            shutil.rmtree(os.path.join(qt, 'plugins', d))
    for d in ('qml', 'translations', 'metatypes', 'libexec'):
        shutil.rmtree(os.path.join(qt, d), ignore_errors=True)
    for d in ('include', 'typesystems', 'glue', 'scripts'):
        shutil.rmtree(os.path.join(pyside, d), ignore_errors=True)
    for f in os.listdir(pyside):                 # stubs, and the Qt tools (designer, linguist, qmlls...)
        full = os.path.join(pyside, f)
        if f.endswith('.pyi') or (os.path.isfile(full) and os.access(full, os.X_OK) and '.' not in f):
            os.remove(full)
    for d in os.listdir(site):                   # pip and the build tools that came with the base image
        if d.split('-')[0] in ('pip', 'build', 'pyproject_hooks', 'packaging', 'certifi'):
            shutil.rmtree(os.path.join(site, d))
    lib = os.path.join(appdir, f'opt/python{version}/lib')
    for d in UNUSED_PYTHON:
        shutil.rmtree(os.path.join(lib, f'python{version}', d.format(v=version)), ignore_errors=True)
    for pattern in ('python*/lib-dynload/_tkinter*', 'libtcl*', 'libtk*', 'tcl8*', 'tk8*', 'itcl*', 'thread*'):
        for path in glob.glob(os.path.join(lib, pattern)):
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    # libxcb-cursor is the one library Qt needs on X11 that many systems do not have installed
    cursor = subprocess.run(['sh', '-c', "ldconfig -p | awk '/libxcb-cursor.so.0 .*x86-64/ {print $NF; exit}'"],
                            capture_output=True, text=True).stdout.strip()
    if cursor:
        shutil.copy(os.path.realpath(cursor), os.path.join(qt, 'lib', 'libxcb-cursor.so.0'))
    else:
        print('warning: no libxcb-cursor on this machine, so the AppImage will not bring one')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--python', default='3.12', help='the Python the AppImage carries (default 3.12)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'dist'), help='where the AppImage goes (default dist/)')
    ap.add_argument('--tool', default=os.environ.get('APPIMAGETOOL'),
                    help='appimagetool, if it is not the one python-appimage downloaded')
    args = ap.parse_args()
    version = args.python
    with tempfile.TemporaryDirectory() as tmp:
        wheels, app = os.path.join(tmp, 'wheels'), os.path.join(tmp, 'app')
        run(sys.executable, '-m', 'pip', 'wheel', '-q', '--no-deps', '-w', wheels, ROOT)
        os.makedirs(app)
        wheel = glob.glob(os.path.join(wheels, '*.whl'))[0]
        open(os.path.join(app, 'requirements.txt'), 'w').write(wheel + '\n')
        open(os.path.join(app, 'entrypoint.sh'), 'w').write('#! /bin/bash\n{{ python-executable }} -m hm80 "$@"\n')
        shutil.copy(os.path.join(ROOT, 'data', f'{NAME}.desktop'), app)
        shutil.copy(os.path.join(ROOT, 'hm80', 'icon.svg'), os.path.join(app, f'{NAME}.svg'))
        run('python-appimage', 'build', 'app', '-p', version, '-n', NAME, '--no-packaging', app, cwd=tmp)
        appdir = glob.glob(os.path.join(tmp, '*-x86_64'))[0]            # python-appimage names it after the app
        trim(appdir, version)
        tool = args.tool or find_tool()
        os.makedirs(args.out, exist_ok=True)
        out = os.path.join(args.out, OUT_NAME)
        if os.path.exists(out):
            os.remove(out)
        run(tool, '--no-appstream', appdir, out, env={**os.environ, 'ARCH': 'x86_64'})
    print(f'built {out} ({os.path.getsize(out) / 1e6:.0f} MB)')


if __name__ == '__main__':
    main()
