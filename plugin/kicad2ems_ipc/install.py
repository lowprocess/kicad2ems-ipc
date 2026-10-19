"""
The plugin folder KiCad loads (KiCad 9 and later): <KiCad's plugins folder>/kicad2ems/ with

  plugin.json        one action in the PCB editor's toolbar; runtime "exec": KiCad runs the launcher with its API
                     socket and token in the environment
  kicad2ems-plugin   the launcher (.cmd on Windows): this Python, -m kicad2ems.kicadplugin. So the plugin runs in
                     the environment kicad2ems is installed in (and KiCad builds no Python environment for it)
  icon-24.png, icon-48.png

    python -m kicad2ems.kicadplugin install [--kicad-version 10.0] [--dir FOLDER]
    python -m kicad2ems.kicadplugin uninstall

The plugins folder: <documents>/KiCad/<version>/plugins on Windows and macOS, ~/.local/share/kicad/<version>/plugins
on Linux ($XDG_DATA_HOME). Without --kicad-version: every KiCad 9 or later found there, else 10.0. KiCad reads it at
start (or Preferences > Plugins > Reload).
"""
import argparse, glob, json, os, re, stat, sys

IDENTIFIER = 'org.kicad2ems.plugin'
FOLDER = 'kicad2ems'


def kicad_base():
    """KiCad's user data folder (the one that holds a folder per version)."""
    if sys.platform.startswith('win') or sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Documents', 'KiCad')
    return os.path.join(os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share'),
                        'kicad')


def versions(base=None):
    """The KiCad versions (9.0 or later) with a user data folder."""
    out = []
    for d in glob.glob(os.path.join(base or kicad_base(), '*')):
        m = re.fullmatch(r'(\d+)\.(\d+)', os.path.basename(d))
        if m and int(m.group(1)) >= 9 and os.path.isdir(d):
            out.append(os.path.basename(d))
    return sorted(out, key=lambda v: tuple(map(int, v.split('.'))))


def plugin_json(entry):
    icons = ['icon-24.png', 'icon-48.png']
    return {'$schema': 'https://go.kicad.org/api/schemas/v1', 'identifier': IDENTIFIER, 'name': 'kicad2ems',
            'description': 'Transmission-line impedance of the board, and EM simulation of a region with kicad2ems',
            'runtime': {'type': 'exec'},
            'actions': [{'identifier': 'kicad2ems.open', 'name': 'kicad2ems',
                         'description': 'Impedance map, region and ports, and a kicad2ems simulation',
                         'show-button': True, 'scopes': ['pcb'], 'entrypoint': entry,
                         'icons-light': icons, 'icons-dark': icons}]}


def launcher(folder):
    """The script KiCad runs: this Python, the plugin's module. Returns its file name."""
    py = sys.executable
    if sys.platform.startswith('win'):
        name = 'kicad2ems-plugin.cmd'
        text = f'@echo off\r\nset PYTHONHOME=\r\nset PYTHONPATH=\r\n"{py}" -m kicad2ems.kicadplugin %*\r\n'
    else:
        name = 'kicad2ems-plugin'
        text = f'#!/bin/sh\n# the kicad2ems KiCad plugin: kicad2ems\'s own Python\nunset PYTHONHOME PYTHONPATH\n' \
               f'exec "{py}" -m kicad2ems.kicadplugin "$@"\n'
    path = os.path.join(folder, name)
    with open(path, 'w', newline='') as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return name


def icons(folder):
    """The toolbar icons, drawn from kicad2ems's own (gui/kicad2ems.svg)."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import Qt
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])                # noqa: F841 (painting needs one; a full
    # QApplication, so that widgets can follow in the same process)
    svg = QSvgRenderer(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gui', 'kicad2ems.svg'))
    for n in (24, 48):
        img = QImage(n, n, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        svg.render(p)
        p.end()
        img.save(os.path.join(folder, f'icon-{n}.png'))


def install(dest):
    os.makedirs(dest, exist_ok=True)
    entry = launcher(dest)
    with open(os.path.join(dest, 'plugin.json'), 'w') as fh:
        json.dump(plugin_json(entry), fh, indent=2)
    icons(dest)
    return dest


def uninstall(dest):
    import shutil
    if os.path.isfile(os.path.join(dest, 'plugin.json')):
        shutil.rmtree(dest)
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m kicad2ems.kicadplugin', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('install', 'uninstall'))
    ap.add_argument('--kicad-version', help='KiCad version folder, e.g. 10.0 (default: every one found)')
    ap.add_argument('--dir', help='the plugins folder itself (overrides the version)')
    a = ap.parse_args(argv)
    if a.dir:
        targets = [os.path.join(a.dir, FOLDER)]
    else:
        vs = [a.kicad_version] if a.kicad_version else (versions() or ['10.0'])
        targets = [os.path.join(kicad_base(), v, 'plugins', FOLDER) for v in vs]
    for t in targets:
        if a.action == 'install':
            install(t)
            print(f"installed: {t}")
        else:
            print(f"{'removed' if uninstall(t) else 'not installed'}: {t}")
    try:
        import kipy                                                 # noqa: F401
    except ImportError:
        print("note: the plugin needs the kicad-python package in this Python: pip install kicad-python")
    if a.action == 'install':
        print("Restart KiCad (or Preferences > Plugins > Reload), and enable its API server there: the kicad2ems "
              "button appears in the PCB editor's toolbar.")
    return 0
