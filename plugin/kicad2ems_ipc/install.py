"""
The plugin folder KiCad loads (KiCad 9 and later): <KiCad's plugins folder>/kicad2ems/. The repository's plugin/
folder is that folder, and the Plugin and Content Manager installs the same files from its package
(tools/build_pcm.py):

  plugin.json          one action in the PCB editor's toolbar
  kicad2ems_plugin.py  the entry point: the window (kicad2ems_ipc.window)
  requirements.txt     what KiCad installs into the plugin's own Python environment (kicad2ems, kicad-python, Qt)
  kicad2ems_ipc/       the plugin
  icon-24.png, icon-48.png

    python plugin/kicad2ems_plugin.py install [--kicad-version 10.0] [--dir FOLDER] [--exec]
    python plugin/kicad2ems_plugin.py uninstall

install copies the folder (runtime "python": KiCad builds the environment from requirements.txt, as for a
package from the Plugin and Content Manager). --exec instead writes a plugin.json and a launcher that run this
checkout's entry point with this Python (runtime "exec"), for working on the plugin or using a kicad2ems
installed here (KiCad then builds no environment).

The plugins folder: <documents>/KiCad/<version>/plugins on Windows and macOS, ~/.local/share/kicad/<version>/plugins
on Linux ($XDG_DATA_HOME). Without --kicad-version: every KiCad 9 or later found there, else 10.0. KiCad reads it at
start (or Preferences > Plugins > Reload).
"""
import argparse, glob, json, os, re, shutil, stat, sys

IDENTIFIER = 'org.kicad2ems.plugin'
FOLDER = 'kicad2ems'
PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the plugin/ folder
ENTRY = 'kicad2ems_plugin.py'
ICONS = ['icon-24.png', 'icon-48.png']
FILES = ['plugin.json', ENTRY, 'requirements.txt'] + ICONS


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


def plugin_json(runtime='python', entry=ENTRY):
    """plugin.json: runtime 'python' (plugin/plugin.json, the package's) or 'exec' (a launcher, --exec)."""
    return {'$schema': 'https://go.kicad.org/api/schemas/v1', 'identifier': IDENTIFIER, 'name': 'kicad2ems',
            'description': 'Transmission-line impedance of the board, and EM simulation of a region with kicad2ems',
            'runtime': {'type': runtime},
            'actions': [{'identifier': 'kicad2ems.open', 'name': 'kicad2ems',
                         'description': 'Impedance map, region and ports, and a kicad2ems simulation',
                         'show-button': True, 'scopes': ['pcb'], 'entrypoint': entry,
                         'icons-light': ICONS, 'icons-dark': ICONS}]}


def launcher(folder):
    """--exec: the script KiCad runs, this Python on this checkout's entry point. Returns its file name."""
    py, entry = sys.executable, os.path.join(PLUGIN, ENTRY)
    if sys.platform.startswith('win'):
        name = 'kicad2ems-plugin.cmd'
        text = f'@echo off\r\nset PYTHONHOME=\r\nset PYTHONPATH=\r\n"{py}" "{entry}" %*\r\n'
    else:
        name = 'kicad2ems-plugin'
        text = f'#!/bin/sh\n# the kicad2ems KiCad plugin: this checkout, run by the Python that installed it\n' \
               f'unset PYTHONHOME PYTHONPATH\nexec "{py}" "{entry}" "$@"\n'
    path = os.path.join(folder, name)
    with open(path, 'w', newline='') as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return name


def install(dest, exec_=False):
    if os.path.exists(dest):
        uninstall(dest)
    os.makedirs(dest)
    for f in ICONS:
        shutil.copy2(os.path.join(PLUGIN, f), dest)
    if exec_:
        with open(os.path.join(dest, 'plugin.json'), 'w') as fh:
            json.dump(plugin_json('exec', launcher(dest)), fh, indent=2)
        return dest
    for f in FILES:
        shutil.copy2(os.path.join(PLUGIN, f), dest)
    shutil.copytree(os.path.join(PLUGIN, 'kicad2ems_ipc'), os.path.join(dest, 'kicad2ems_ipc'),
                    ignore=shutil.ignore_patterns('__pycache__'))
    return dest


def uninstall(dest):
    if os.path.isfile(os.path.join(dest, 'plugin.json')):
        shutil.rmtree(dest)
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python plugin/kicad2ems_plugin.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('install', 'uninstall'))
    ap.add_argument('--kicad-version', help='KiCad version folder, e.g. 10.0 (default: every one found)')
    ap.add_argument('--dir', help='the plugins folder itself (overrides the version)')
    ap.add_argument('--exec', dest='exec_', action='store_true',
                    help="run this checkout with this Python (KiCad builds no environment)")
    a = ap.parse_args(argv)
    if a.dir:
        targets = [os.path.join(a.dir, FOLDER)]
    else:
        vs = [a.kicad_version] if a.kicad_version else (versions() or ['10.0'])
        targets = [os.path.join(kicad_base(), v, 'plugins', FOLDER) for v in vs]
    for t in targets:
        if a.action == 'install':
            install(t, a.exec_)
            print(f"installed: {t}")
        else:
            print(f"{'removed' if uninstall(t) else 'not installed'}: {t}")
    if a.action == 'install' and a.exec_:
        try:
            import kipy, kicad2ems, kicad2ems_gui                   # noqa: F401
        except ImportError as e:
            print(f"note: --exec runs this Python, which lacks {e.name}: pip install -r "
                  f"{os.path.join(PLUGIN, 'requirements.txt')}")
    if a.action == 'install':
        print("Restart KiCad (or Preferences > Plugins > Reload), and enable its API server there: the kicad2ems "
              "button appears in the PCB editor's toolbar." + ('' if a.exec_ else
              " The first start builds the plugin's Python environment (a few minutes)."))
    return 0
