# KiCad plugin

[← Index](index.md)

kicad2ems has a plugin for KiCad's PCB editor. It talks to KiCad through KiCad's IPC API (KiCad 9 and later) and
reads the board as it is open in KiCad, unsaved edits included. A toolbar button opens a window with:

- **The impedance map:** the board seen from above, every trace coloured by how far its impedance is from its
  net class's target ([line model](transmission-lines.md) Z0, or Zdiff for a pair; blue below, grey on target,
  red above). Hover a trace to read its cross-section, width, Z0, Zdiff, class, target and deviation. Click it
  to select its track in KiCad. Tick the net classes to show; `#PWR` nets are never mapped.
- **Nets to simulate:** a net, a differential pair or a set of nets (picked on the map or taken from KiCad's
  selection). Their region, ports and parts are set up from the board: see [Simulating nets](#simulating-nets).
- **A region to simulate:** two clicks on the plan, or the box around the items selected in KiCad (1 mm beyond).
  Optionally, ports wherever a signal track crosses the region's edge.
- **Ports:** click a trace (the port goes on its centre line, along it) or a pad (on the pad). The board's own
  port footprints (Fab text `P<n>`) count as well.
- **A simulation:** the band and the solver, then Run (progress and the report in the window), or Open in
  kicad2ems (the same set-up in [kicad2ems's own window](gui.md), with every setting). The S-parameters tab shows
  the results against the line model.

## Installing

kicad2ems and the `kicad` extra (kicad-python, the Qt packages) in one Python environment, then the plugin into
KiCad:

```bash
pip install '.[kicad]'                     # in kicad2ems's environment
kicad2ems-kicad install                    # or: python -m kicad2ems.kicadplugin install
```

In KiCad: Preferences > Plugins > **Enable KiCad API**, then restart KiCad (or Reload there). The kicad2ems
button appears in the PCB editor's toolbar.

`install` writes a folder `kicad2ems` into KiCad's plugins folder, `~/.local/share/kicad/<version>/plugins` on
Linux and `Documents/KiCad/<version>/plugins` on Windows and macOS. It goes into every KiCad 9 or later found;
`--kicad-version 10.0` picks one and `--dir` gives the plugins folder itself. The folder holds:

| File | |
|---|---|
| `plugin.json` | One action in the PCB editor, runtime `exec` (KiCad builds no Python environment for it) |
| `kicad2ems-plugin` (`.cmd` on Windows) | The launcher: the Python `install` ran in, `-m kicad2ems.kicadplugin`. KiCad passes it its API socket and token |
| `icon-24.png`, `icon-48.png` | The button |

Reinstall after moving kicad2ems to another Python. `kicad2ems-kicad uninstall` removes the folder.

## Using it

1. Open the board in KiCad's PCB editor and press the kicad2ems button. The window reads the board and maps the
   impedance of every routed signal net, a group of nets at a time; the map fills in as it goes.
2. Read the map: the layer box switches copper layers. Hover a trace for its numbers; click it to find it in
   KiCad.
3. Set a region: **Draw** and click two corners, or select the parts of interest in KiCad and press **From KiCad
   selection**. Tick **Ports where signal tracks cross the region edge** to terminate the lines the edge cuts.
4. Add ports: **Add**, then click traces or pads (on the layer shown). Ports outside the region are marked: the
   run leaves them out.
5. Set the band and the solver, and **Run**. A run uses the board as KiCad has it at that moment. Or
   **Open in kicad2ems** for every setting, the plots and the field views.

### Simulating nets

1. Pick the nets: **From KiCad selection** takes the nets of what is selected in KiCad (tracks, pads, a net
   highlighted). **Pick** adds the net of each trace you click on the plan; clicking it again removes it. A
   pair's other leg always comes along.
2. Set **Board around them** (2 mm by default: enough for the planes and ground beside the lines) and press
   **Set up region and ports**. This sets:
   - **The nets:** the ones picked, plus the net on the far side of each series R / L / C, and so on (up to 16
     nets).
   - **The parts:** two-pad R / L / C footprints whose value kicad2ems reads (not DNP). One to ground is a shunt
     part, one between two of the nets a series part. Both are lumped elements in the run. A part to a supply
     (`#PWR`) or to an unconnected pad is left out.
   - **The ports:** one on the pad of every other footprint on the nets (ICs, connectors, ESD parts; one per
     footprint and net), and one at each track end that reaches no pad, via or track. Where a footprint has pads
     on both legs of a pair, or the two legs' open ends lie within 3 mm of each other, they become one
     differential port (`P1+` / `P1-`). Nets with port footprints on the board keep those instead.
   - **The region:** the box around the nets' copper and their parts' pads, plus the margin, within the board. No
     ports at the region edge (the nets end inside it).
3. Press **Run**. The run models only these nets and the ground nets. It also solves the line model as a reference,
   so the comparison works with any solver you choose.
4. The **S-parameters** tab: |S| in dB of each full-wave solver (solid) and of the line model (dashed, same colour
   for the same term). For a pair it shows the differential terms (Sdd). The table below gives, for each term,
   the largest difference in dB (only where the full-wave |S| is above -20 dB) and the largest complex
   difference |dS|.

Drawing a region by hand, or taking one from the selection, turns a net run back into an ordinary run (nets under
the ports). A port clicked by hand during a net run adds its net to the run.

On the `rf_diffvia` example without its port footprints, picking `D_P` sets up the pair as `P1±` at the open ends
on F.Cu and `P2±` on In2.Cu. With gfdtd, Sdd21 matches the coupled line model within 0.27 dB from 0.5 to 6 GHz
(46 s on an RX 7800 XT). On `demo_lowpass`, picking `RF_IN` brings in `N1` and `RF_OUT` through L1 and L3, and
keeps C2 as a shunt part.

**Reload from KiCad** reads the board again after edits. The region, ports, band and solver are kept per board.

### Net classes and impedance targets

The map's class list shows each net class with its nets and its target. The target comes from the class name:
a number with `ohm`, `Ohm`, `R` or `Ω` after it (or `Z` before it), and `SE` / `single` or `diff` / `DP` /
`differential` for the kind:

| Class name | Target |
|---|---|
| `50R_SE`, `50 Ohm SE`, `Z50_SE` | 50 Ω single-ended |
| `100R_DIFF`, `100ohm_diff`, `Z90_DP` | 100 / 90 Ω differential |
| `90ohm`, `100ohm` (no kind, as KiCad's CM5 demo names them) | differential if the class holds a pair, else single-ended |
| `Default`, `DDR4`, `HS` | none: judged against 50 Ω single-ended |

A pair is two nets whose names end in `+` / `-` or `P` / `N` (KiCad's rule). In a differential class a net that
is not a pair's leg (a class pattern caught it too) gets no target. KiCad reports the classes through its API
(assignments and patterns resolved); without it the plugin reads them from the `.kicad_pro`.

A differential target is checked against the pair's differential impedance where the legs run side by side
(Zdiff = 2 Zodd, from the coupled-line model), and against 2 Z0 where they part. On the `rf_diffvia` example
(a 100 Ω pair) the coupled sections come out at 100.0 Ω. The colours run from -x % to +x % of the target, x
covering 95 % of the traces shown (at least 10 %).

Anything named `#PWR` (a net or a class: KiCad's power symbols) is disabled: never mapped, listed apart.

**Show** narrows the map to KiCad's selection or the region, within the ticked classes. Nets are mapped the
first time they are shown, in the background, and kept.

### Speed

The map is built a net (or a pair) at a time, over worker processes (one per performance core), and kept: the
next time the board is opened, and after **Reload from KiCad**, only the nets whose copper changed are mapped
again (a change of the planes or the stack re-maps everything). On an i7-13700K (8 workers):

| KiCad demo | Signal nets | Before | Now | From the cache |
|---|---|---|---|---|
| CM5 Minima | 94 | 8.5 s | 0.8 s | 0.35 s |
| video | 369 | 87 s | 2.7 s | 1.0 s |
| Jetson AGX Thor baseboard | 862 | 19 min | 6.6 s | 4.2 s |

Mapping one net at a time is faster than in groups as well as steadier: in a group the line model also coupled
unrelated neighbours (a cost growing with the square of the group) and split their traces where they ran side by
side. Now only a pair's legs couple, and the map does not depend on how the nets were grouped. The planes are
cut into 4 mm tiles once, so that a ray testing for nearby ground meets a few small pieces instead of a whole
plane with its holes: the same map, 1.4-2.8 times faster in one process. Past 150 signal
nets the window still starts with the classes that name an impedance (or KiCad's selection).

## Files

Everything goes to `<project>/kicad2ems/<board>/`:

| File | |
|---|---|
| `<board>_plugin.json` | The session: region, ports, cut ports, band, solver, the classes shown, the nets of a net run |
| `<board>_impedance.json.gz` | The map kept between sessions: each net's sections, under a hash of its copper and of the board's planes and stack |
| `<board>.kicad_pcb` | The board as KiCad had it at the last Run (the run's input) |
| `<board>.toml` | The run's kicad2ems config: `kicad2ems <board>.toml` repeats it, `kicad2ems-gui <board>.toml` opens it |
| `out/` | The run's results: S-parameters, plots, the report |

The config is plain kicad2ems: `region = [x0, y0, x1, y1]` (KiCad mm), `ports = ["P5@x,y,F.Cu", "P6=J1.1"]`
([extra ports](usage.md)), `cut_ports`, `nets = ["auto"]` (a net run: its nets and the ground nets, and
`fast = "auto"`), `fmin`, `fmax`, `solvers`.

## How it works

| Module | |
|---|---|
| `kicad2ems/kicadplugin/ipc.py` | The link to KiCad (kipy): the board as text (`Board.get_as_string`), its path, the selection's box and nets, selecting a track |
| `kicad2ems/kicadplugin/model.py` | No Qt: the board read by kicad2ems's KiCad importer from that text, the impedance map, ports snapped to traces and pads, the session, the run's settings |
| `kicad2ems/kicadplugin/window.py` | The window: kicad2ems's plan view with the region and port tools, the map built in a thread, runs as a separate process (the GUI's event stream) |
| `kicad2ems/kicadplugin/install.py` | The plugin folder |

The map is the [line model](transmission-lines.md) of every signal net (routed, not ground), built without ports.
The reference copper excludes every signal net, so a trace is referenced to the planes and grounds, never to a
neighbouring trace.

Not yet: writing the region or the ports back into the board, differential ports clicked by hand (a net run
makes them), clicking ports on layers other than the one shown, and footprint-editor or schematic actions.
