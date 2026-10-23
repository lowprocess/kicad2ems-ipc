"""What KiCad's toolbar button runs (plugin.json's entry point): the kicad2ems window, with KiCad's API socket and
token in the environment. Also: python kicad2ems_plugin.py install | uninstall [--exec] (kicad2ems_ipc.install)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))         # kicad2ems_ipc beside this file

from kicad2ems_ipc.__main__ import main                                # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
