"""
kicad2ems as a KiCad plugin, through KiCad's IPC API (KiCad 9 and later; the kicad-python package): a button in
the PCB editor opens a window with the open board seen from above, its traces coloured by their impedance (the
line model's Z0, read on hover; a click selects the track in KiCad), a region to simulate (drawn, or around
KiCad's selection) and ports (clicked on traces and pads, or where tracks cross the region edge), and runs
kicad2ems on them (or opens the set-up in kicad2ems's GUI).

    python plugin/kicad2ems_plugin.py install    # the button in KiCad (install.py; or the Plugin and Content Manager)
    python plugin/kicad2ems_plugin.py            # the window, with a KiCad running

  ipc.py      the link to KiCad (kipy): the board as text, the selection, selecting tracks
  model.py    no Qt: the board, the impedance map, the session (region, ports, band), the run's settings
  window.py   the window
  install.py  the plugin folder KiCad loads (plugin/), copied, or a launcher for this Python (--exec)

It is kicad2ems's KiCad plugin, kept apart from kicad2ems (github.com/lowprocess/kicad2ems), which it uses as a
library: the board, the KiCad importer, the line model, and the GUI's plan view and run handling.
"""
__version__ = '0.1.0'

