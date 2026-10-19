"""python -m kicad2ems.kicadplugin [install | uninstall] [--kicad-version 10.0] [--dir FOLDER]: the window (what
KiCad's button runs), or the plugin installed into / removed from KiCad's plugins folder."""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('install', 'uninstall'):
        from .install import main as install_main
        return install_main(argv)
    from .window import main as window_main
    return window_main(argv)


if __name__ == '__main__':
    sys.exit(main())
