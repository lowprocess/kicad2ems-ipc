"""The Plugin and Content Manager package: dist/kicad2ems-<version>-pcm.zip (plugins/: the plugin/ folder;
resources/icon.png; metadata.json), and dist/metadata.json, the entry to submit to
gitlab.com/kicad/addons/metadata (packages/<identifier>/metadata.json) with the download's URL, hash and sizes.

    python tools/build_pcm.py [--status stable] [--url https://.../kicad2ems-<version>-pcm.zip]

The default URL is the GitHub release of this version (tag v<version>): upload the zip there."""
import argparse, hashlib, json, os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, 'plugin')
sys.path.insert(0, PLUGIN)
from kicad2ems_ipc import __version__                                  # noqa: E402

RELEASE = 'https://github.com/lowprocess/kicad2ems-ipc/releases/download/v{v}/{name}'


def files():
    """(path in the zip, path on disk): the plugin folder's files without caches, the icon, the metadata."""
    for d, dirs, fs in os.walk(PLUGIN):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for f in sorted(fs):
            if not f.endswith('.pyc'):
                p = os.path.join(d, f)
                yield 'plugins/' + os.path.relpath(p, PLUGIN).replace(os.sep, '/'), p
    yield 'resources/icon.png', os.path.join(ROOT, 'pcm', 'icon.png')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--status', default='testing', choices=('stable', 'testing', 'development', 'deprecated'))
    ap.add_argument('--url', help='where the zip is downloaded from (default: the GitHub release of this version)')
    a = ap.parse_args(argv)
    with open(os.path.join(ROOT, 'pcm', 'metadata.json')) as fh:
        meta = json.load(fh)
    ver = dict(meta['versions'][0], version=__version__, status=a.status)
    meta['versions'] = [ver]
    dist = os.path.join(ROOT, 'dist')
    os.makedirs(dist, exist_ok=True)
    name = f'kicad2ems-{__version__}-pcm.zip'
    path = os.path.join(dist, name)
    size = 0
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, p in files():
            z.write(p, arc)
            size += os.path.getsize(p)
        z.writestr('metadata.json', json.dumps(meta, indent=2) + '\n')     # in the archive: no download fields
    with open(path, 'rb') as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    ver.update(download_url=a.url or RELEASE.format(v=__version__, name=name), download_sha256=sha,
               download_size=os.path.getsize(path), install_size=size)
    with open(os.path.join(dist, 'metadata.json'), 'w') as fh:
        json.dump(meta, fh, indent=2)
        fh.write('\n')
    print(f"{path}: {os.path.getsize(path)} bytes, sha256 {sha}")
    print(f"{os.path.join(dist, 'metadata.json')}: the metadata to submit (download_url {ver['download_url']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
