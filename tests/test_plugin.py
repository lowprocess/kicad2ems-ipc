"""The KiCad plugin: its model (the board from text as KiCad's API gives it, the impedance map, ports, the run's
settings), its installer (KiCad's plugin schema), and its window offscreen with a stand-in for KiCad."""
import json, os, sys

import numpy as np
import pytest

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'examples')
SCHEMA = '/usr/share/kicad/schemas/api.v1.schema.json'


def _text(name='rf_coupler'):
    """A board's text: an example by name, or a .kicad_pcb path."""
    path = name if name.endswith('.kicad_pcb') else os.path.join(EX, f'{name}.kicad_pcb')
    with open(path, encoding='utf-8') as fh:
        return fh.read()


@pytest.fixture(scope='module')
def lowpass(boards):
    """The generated low-pass demo board (examples/demo_lowpass.kicad_pcb is generated too, and not in git)."""
    return boards['lowpass'] + '.kicad_pcb'


@pytest.fixture(scope='module')
def plan():
    from kicad2ems.kicadplugin.model import Plan
    p = Plan(_text(), os.path.join(EX, 'rf_coupler.kicad_pcb'))
    steps = list(p.map_steps(jobs=1))
    assert steps[-1][:2] == (len(p.signal), len(p.signal))
    return p


def test_impedance_map_of_the_board(plan):
    """Every routed signal net, no ports needed; the coupler's traces between 49 and 55 ohm."""
    assert plan.signal == ['RF'] and plan.board_ports == ['P1', 'P2', 'P3', 'P4']
    traces = [e for e in plan.lines['elements'] if e['kind'] == 'trace' and e['z0']]
    assert traces and all(45 < e['z0'] < 60 for e in traces)
    assert plan.board['outline'] and plan.board['stack']['copper']


def test_ports_snap_to_traces_and_pads(plan):
    from kicad2ems.kicadplugin.model import Session
    s = Session()
    e = next(e for e in plan.lines['elements'] if e['kind'] == 'trace' and e['length'] > 2)
    mid = np.asarray(e['pts'][len(e['pts']) // 2])
    p = plan.port_at(mid[0], mid[1] + 0.2, e['layer'], 0.5, s)          # a little off the centre line
    assert p['name'] == 'P5' and p['spec'].startswith('P5@') and p['net'] == 'RF'
    x, y = [float(v) for v in p['spec'].split('@')[1].split(',')[:2]]
    assert abs(y + mid[1]) < 0.3 and p['layer'] == e['layer']        # on the centre line, KiCad y (down)
    s.ports.append(p)
    assert plan.port_at(mid[0], mid[1], e['layer'], 0.5, s)['name'] == 'P6'
    with pytest.raises(ValueError):
        plan.port_at(-50.0, 50.0, e['layer'], 0.5, s)                  # nothing there


def test_net_class_targets():
    """The target impedance from the class name; SE / diff from the name, else from the class holding a pair; a
    pair class's single nets get none; #PWR nets and classes are disabled."""
    from kicad2ems.kicadplugin.netclass import parse_target, NetClasses, partner
    assert parse_target('50R_SE') == (50.0, 'se') and parse_target('100R_DIFF') == (100.0, 'diff')
    assert parse_target('100 Ohm diff') == (100.0, 'diff') and parse_target('Z90_DP') == (90.0, 'diff')
    assert parse_target('50ohm') == (50.0, None)
    assert all(parse_target(n) is None for n in ('Default', 'DDR4', 'USB2', 'HS', ''))
    assert partner('/USB_D+') == '/USB_D-' and partner('ETH_TX_N') == 'ETH_TX_P' and partner('CLK') is None
    nets = ['D_P', 'D_N', 'WAKE', 'RF', 'Net-(#PWR01-PWR)', 'X']
    nc = NetClasses({'D_P': ['90ohm'], 'D_N': ['90ohm'], 'WAKE': ['90ohm'], 'RF': ['50ohm'], 'X': ['#PWR_rail']},
                    nets)
    assert nc.target('D_P') == (90.0, 'diff') and nc.target('WAKE') is None and nc.target('RF') == (50.0, 'se')
    assert nc.of('X') == '#PWR_rail' and nc.disabled('X') and nc.disabled('Net-(#PWR01-PWR)')
    assert not nc.disabled('RF') and nc.target('Net-(#PWR01-PWR)') is None


def test_project_file_classes(tmp_path):
    """The classes of a .kicad_pro: explicit assignments, then patterns, then Default."""
    from kicad2ems.kicadplugin.netclass import NetClasses
    pro = tmp_path / 'b.kicad_pro'
    pro.write_text(json.dumps({'net_settings': {
        'classes': [{'name': 'Default'}, {'name': '100ohm'}, {'name': '50R_SE'}],
        'netclass_assignments': {'/RF_IN': '50R_SE'},
        'netclass_patterns': [{'netclass': '100ohm', 'pattern': '/*ETH_*'}]}}))
    nc = NetClasses.from_project(str(pro), ['/RF_IN', '/CM5/ETH_TX_P', '/CM5/ETH_TX_N', '/LED'])
    assert nc.of('/RF_IN') == '50R_SE' and nc.of('/CM5/ETH_TX_P') == '100ohm' and nc.of('/LED') == 'Default'
    assert nc.target('/CM5/ETH_TX_N') == (100.0, 'diff') and nc.target('/LED') is None


def test_diff_pair_impedance_and_pwr_nets():
    """A 100 ohm pair (the rf_diffvia example) in a 100R_DIFF class: where the legs couple, Zdiff (2 Zodd) is
    within 3 % of 100 ohm and the map colours by it; a #PWR net on the board is never mapped."""
    from kicad2ems.kicadplugin.model import Plan
    from kicad2ems import overlay as ov
    text = _text('rf_diffvia')
    p = Plan(text, os.path.join(EX, 'rf_diffvia.kicad_pcb'), classes={'D_P': ['100R_DIFF'], 'D_N': ['100R_DIFF']})
    assert p.classes() == [('100R_DIFF', ['D_N', 'D_P'], '100 ohm diff')]
    units = [chunk for _, _, chunk in p.map_steps(jobs=1)]
    assert [sorted(u) for u in units] == [['D_N', 'D_P']]               # the legs mapped together
    coupled = [e for e in p.lines['elements'] if e['kind'] == 'trace' and e['zdiff']]
    assert coupled and all(abs(e['zdiff'] - 100) < 3 and e['tkind'] == 'diff' for e in coupled)
    assert all(abs(ov.deviation(e)) < 0.03 for e in coupled) and 'Zdiff' in ov.describe(coupled[0])
    pwr = Plan(text.replace('"D_N"', '"Net-(#PWR01-PWR)"'), '', classes={})
    assert pwr.disabled == ['Net-(#PWR01-PWR)'] and pwr.signal == ['D_P']
    list(pwr.map_steps(nets=['D_P', 'Net-(#PWR01-PWR)']))
    assert {e['net'] for e in pwr.lines['elements']} == {'D_P'}


def _key(p):
    return sorted((e['net'], e['kind'], e['layer'], round(e['z0'] or 0, 6), round(e['length'] or 0, 6))
                  for e in p.lines['elements'])


def test_parallel_map_matches_one_process(lowpass):
    """Worker processes give the same map as one process (a net or pair at a time: no dependence on grouping)."""
    from kicad2ems.kicadplugin.model import Plan
    text, path = _text(lowpass), lowpass
    one, two = Plan(text, path), Plan(text, path)
    list(one.map_steps(jobs=1))
    steps = list(two.map_steps(jobs=2))
    assert len(two.signal) == 3 and len(steps) == 3 and _key(one) == _key(two) and one.lines['elements']


def test_tiled_planes_give_the_same_map(lowpass):
    """Ray casts against reference copper cut into tiles (tlines.TILE) find what the whole planes give."""
    import kicad2ems.solvers.lines.tlines as tl
    from kicad2ems.kicadplugin.model import Plan
    text, path = _text(lowpass), lowpass
    keep = tl.TILE
    try:
        out = []
        for tile in (None, 0.5):                                       # small tiles: rays cross several
            tl.TILE = tile
            p = Plan(text, path)
            list(p.map_steps(jobs=1))
            out.append([(e['net'], e['xs'], e['z0'], e['length'], str(e['pts'])) for e in p.lines['elements']])
        assert out[0] == out[1] and out[0]
    finally:
        tl.TILE = keep


def test_map_cache(tmp_path, lowpass):
    """The map kept between sessions: everything from the cache the second time, and after an edit only the net
    it touched is mapped again."""
    import re
    from kicad2ems.kicadplugin.model import Plan
    text, path, cache = _text(lowpass), lowpass, str(tmp_path / 'm.json.gz')
    first = Plan(text, path)
    list(first.map_steps(jobs=1, cache=cache))
    again = Plan(text, path)
    steps = list(again.map_steps(jobs=1, cache=cache))
    assert len(steps) == 1 and steps[0][0] == 3 and _key(again) == _key(first)
    m = re.search(r'\(segment\s*\(start ([\d.]+) ', text)
    edited = text[:m.start(1)] + f"{float(m.group(1)) + 0.01:.4f}" + text[m.end(1):]
    after = Plan(edited, path)
    steps = list(after.map_steps(jobs=1, cache=cache))
    assert steps[0][0] == 2 and len(steps) == 2 and len(steps[1][2]) == 1     # 2 nets cached, 1 mapped again


def test_session_and_run_settings(plan, tmp_path):
    from kicad2ems.kicadplugin.model import Session, run_values, write_config, session_path, outside
    from kicad2ems.config import build_settings
    s = Session(region=[-1.0, -1.0, 14.0, 12.0], fmin=1.0, fmax=6.0, solvers=['openems'])
    e = next(e for e in plan.lines['elements'] if e['kind'] == 'trace')
    s.ports.append(dict(name='P5', spec='P5@3,2.82,F.Cu', x=3.0, y=-2.82, layer='F.Cu', what='', net='RF'))
    assert not outside(s, s.ports[0]) and outside(s, dict(x=30.0, y=-2.0))
    s.save(session_path(str(tmp_path), 'b'))
    back = Session.load(session_path(str(tmp_path), 'b'))
    assert back.to_dict() == s.to_dict()
    vals = run_values(s, plan, _text(), str(tmp_path / 'run'), 'rf_coupler')
    assert os.path.exists(vals['board']) and vals['region'] == s.region and vals['cut_ports'] is True
    cfg = write_config(vals, str(tmp_path / 'run' / 'rf_coupler.toml'))
    st = build_settings({}, cfg)
    assert st.ports == ['P5@3,2.82,F.Cu'] and st.fmax == 6.0 and st.solver_plan()[0] == ['openems']
    assert e is not None


def _without_ports(text):
    """A board's text without its port footprints (RF_Port): a board as designed, before any set-up."""
    out, i = [], 0
    while True:
        j = text.find('(footprint ', i)
        if j < 0:
            out.append(text[i:])
            return ''.join(out)
        depth, k = 0, j
        while True:
            depth += {'(': 1, ')': -1}.get(text[k], 0)
            k += 1
            if depth == 0:
                break
        out += [text[i:j], '' if 'RF_Port' in text[j:k] else text[j:k]]
        i = k


def _net_plan(name, text=None):
    from kicad2ems.kicadplugin.model import Plan
    path = name if name.endswith('.kicad_pcb') else os.path.join(EX, f'{name}.kicad_pcb')
    return Plan(text or _without_ports(_text(name)), path)


def test_net_run_takes_parts_and_the_nets_beyond(tmp_path, lowpass):
    from kicad2ems.kicadplugin import model as md
    from kicad2ems.region import parse_port_spec
    p = _net_plan(lowpass)
    s = p.setup_for_nets(['RF_IN'], margin=1.0)
    assert s.nets == ['RF_IN'] and s.run_nets == ['RF_IN', 'N1', 'RF_OUT']       # through L1 and L3
    assert [(q['ref'], q['role']) for q in s.parts] == [('L1', 'series'), ('C2', 'shunt'), ('L3', 'series')]
    assert [(q['name'], q['net']) for q in s.ports] == [('P1', 'RF_IN'), ('P2', 'RF_OUT')]   # the open ends
    assert all(parse_port_spec(q['spec'])[1] == 'at' for q in s.ports)
    x0, y0, x1, y1 = s.region
    assert all(x0 < q['x'] < x1 and y0 < -q['y'] < y1 for q in s.ports)
    sess = md.Session(solvers=['openems'])
    sess.use(s)
    v = md.run_values(sess, p, _text(lowpass), str(tmp_path), 'demo_lowpass')
    assert v['nets'] == ['GND', 'N1', 'RF_IN', 'RF_OUT'] and v['cut_ports'] is False and v['region'] == s.region
    assert v['fast'] == 'auto' and v['solvers'] == ['openems']             # the line model as a reference
    sess.drop_nets()
    assert md.run_values(sess, p, _text(lowpass), str(tmp_path), 'demo_lowpass')['nets'] == ['auto']


def test_net_run_ports_at_components(lowpass):
    text = _without_ports(_text(lowpass)).replace('"Reference" "L1"', '"Reference" "U1"')
    s = _net_plan(lowpass, text).setup_for_nets(['N1'])
    assert s.run_nets == ['N1', 'RF_OUT'] and [q['ref'] for q in s.parts] == ['C2', 'L3']
    assert [q['spec'] for q in s.ports][0] == 'P1=U1.2'                 # U1 is no lumped part: a port on its pad
    assert len(s.ports) == 2 and s.ports[1]['net'] == 'RF_OUT'


def test_net_run_of_a_pair():
    s = _net_plan('rf_diffvia').setup_for_nets(['D_P'])
    assert s.nets == ['D_P', 'D_N']                                    # the other leg comes along
    assert [(q['name'], q['net']) for q in s.ports] == [('P1+', 'D_P'), ('P1-', 'D_N'), ('P2+', 'D_P'),
                                                        ('P2-', 'D_N')]
    assert {q['layer'] for q in s.ports[2:]} == {'In2.Cu'}


def test_differential_ports_from_specs():
    """A pair's legs given as specs (P1+ / P1-, as a net run writes them) are two legs of one port, not one."""
    from kicad2ems.board import Board
    from kicad2ems.importers.kicad import KiCadBoard
    text = _without_ports(_text('rf_diffvia'))
    s = _net_plan('rf_diffvia', text).setup_for_nets(['D_N'])
    path = os.path.join(EX, 'rf_diffvia.kicad_pcb')
    for nets in (['D_P', 'D_N', 'GND'], 'auto'):
        m = Board(KiCadBoard(path=path, text=text), nets=nets, ports=[q['spec'] for q in s.ports], parts='none')
        assert [q.name for q in m.ports] == ['P1+', 'P1-', 'P2+', 'P2-']
        assert [n for n, _ in m.user_ports] == ['P1', 'P2'] and m.signal_nets == {'D_P', 'D_N'}


def test_net_run_keeps_the_boards_ports(lowpass):
    s = _net_plan(lowpass, _text(lowpass)).setup_for_nets(['RF_IN'])
    assert s.ports == [] and s.board_ports == ['P1', 'P2']
    with pytest.raises(ValueError):
        s = _net_plan(lowpass).setup_for_nets(['GND'])


def _fake_results(folder, stem='b'):
    from kicad2ems.touchstone import write_touchstone
    f = np.linspace(1e9, 5e9, 41)
    S = np.zeros((41, 2, 2), complex)
    S[:, 0, 0] = S[:, 1, 1] = 0.1
    S[:, 1, 0] = S[:, 0, 1] = 0.9 * np.exp(-1j * f / 1e9)
    L = S.copy()
    L[:, 1, 0] = L[:, 0, 1] = 0.8 * np.exp(-1j * f / 1e9)
    for key, X in (('gfdtd', S), ('qucs', L)):
        write_touchstone(os.path.join(folder, f"{stem}_{key}.s2p"), f, X, [50.0, 50.0], names=['P1', 'P2'])
    solved = {k: dict(label=k, touchstone=f"{stem}_{k}.s2p", names=['P1', 'P2'], seconds=1.0) for k in ('gfdtd', 'qucs')}
    with open(os.path.join(folder, f"{stem}_run.json"), 'w') as fh:
        json.dump(dict(solved=solved, results=dict(reference='gfdtd'), warnings=[]), fh)


def test_results_against_the_line_model(tmp_path):
    from kicad2ems.kicadplugin import model as md
    assert md.results(str(tmp_path), 'b') is None
    _fake_results(str(tmp_path))
    r = md.results(str(tmp_path), 'b')
    assert [(s['key'], s['line']) for s in r['solved']] == [('gfdtd', False), ('qucs', True)]
    c = {lab: (ddb, dv) for lab, ddb, _, dv in md.comparison(r)}
    assert c['gfdtd: S(P2,P1)'][0] == pytest.approx(20 * np.log10(0.9 / 0.8), abs=1e-6)
    assert c['gfdtd: S(P1,P1)'][0] is None                              # below -20 dB: no dB difference
    assert md.terms(['P1d', 'P1c', 'P2d', 'P2c']) == [(0, 0, 'Sdd(P1,P1)'), (2, 0, 'Sdd(P2,P1)'), (2, 2, 'Sdd(P2,P2)')]


def test_install_writes_a_valid_plugin(tmp_path):
    from kicad2ems.kicadplugin import install
    dest = install.install(str(tmp_path / 'kicad2ems'))
    pj = json.load(open(os.path.join(dest, 'plugin.json')))
    entry = os.path.join(dest, pj['actions'][0]['entrypoint'])
    assert os.access(entry, os.X_OK) and 'kicad2ems.kicadplugin' in open(entry).read()
    assert all(os.path.getsize(os.path.join(dest, f)) > 0 for f in pj['actions'][0]['icons-light'])
    if os.path.exists(SCHEMA):
        jsonschema = pytest.importorskip('jsonschema')
        jsonschema.validate(pj, json.load(open(SCHEMA)))
    assert install.uninstall(dest) and not os.path.exists(dest)


def test_live_link_reads_and_selects_through_kipy(monkeypatch):
    """ipc.Live over a stand-in for kipy's KiCad: the board text, path and stem (with and without a project), net
    classes (KiCad 9's constituents, else the comma list of the name), the selection's box (items without one
    skipped) and nets, show_track picking the nearest track of the net on the layer (nm in the API, mm here), and
    connect's errors saying what to do."""
    pytest.importorskip('kipy')
    from types import SimpleNamespace as NS
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from kicad2ems.kicadplugin import ipc
    MM = 1_000_000                                                  # nm

    def track(layer, net, x0, x1, y, w=0.4):
        return NS(layer=layer, net=NS(name=net), start=NS(x=x0 * MM, y=y * MM), end=NS(x=x1 * MM, y=y * MM),
                  width=w * MM)
    rf_a, rf_b = track(BoardLayer.BL_F_Cu, 'RF', 0, 10, 0), track(BoardLayer.BL_F_Cu, 'RF', 0, 10, 2)
    tracks = [rf_a, rf_b, track(BoardLayer.BL_B_Cu, 'RF', 0, 10, 0), track(BoardLayer.BL_F_Cu, 'GND', 0, 10, 0)]
    a, b, n = NS(net=NS(name='RF')), NS(net=NS(name='GND')), NS()
    boxes = {id(a): NS(pos=NS(x=1 * MM, y=2 * MM), size=NS(x=3 * MM, y=1 * MM)),
             id(b): NS(pos=NS(x=-1 * MM, y=0), size=NS(x=1 * MM, y=1 * MM))}

    class Board:
        name, selected = 'amp.kicad_pcb', []

        def __init__(self, project):
            self.project = project

        def get_as_string(self):
            return '(kicad_pcb)'

        def get_project(self):
            if self.project is None:
                raise RuntimeError('no project')
            return NS(path=self.project)

        def get_nets(self):
            return ['RF', 'USB_P']

        def get_netclass_for_nets(self, nets):
            return {'RF': NS(constituents=['50R', ''], name='50R'), 'USB_P': NS(constituents=None, name='USB, 90R')}

        def get_selection(self):
            return [a, b, n]

        def get_item_bounding_box(self, it):
            if it is n:
                raise ValueError('no box')
            return boxes[id(it)]

        def get_tracks(self):
            return tracks

        def clear_selection(self):
            Board.selected = []

        def add_to_selection(self, items):
            Board.selected = list(items)
    kicad = NS(get_board=lambda: Board('/work/amp'), get_version=lambda: '9.0.4')
    live = ipc.Live(kicad)
    assert live.version() == '9.0.4' and live.board_text() == '(kicad_pcb)' and live.stem() == 'amp'
    assert live.project_dir() == '/work/amp' and live.board_path() == os.path.join('/work/amp', 'amp.kicad_pcb')
    assert live.net_classes() == {'RF': ['50R'], 'USB_P': ['USB', '90R']}
    assert live.selection_box() == pytest.approx((-1.0, 0.0, 4.0, 3.0)) and live.selection_nets() == {'RF', 'GND'}
    assert live.show_track('F.Cu', 'RF', (2, 1.9), (3, 1.9)) and Board.selected == [rf_b]     # the nearer of two
    assert not live.show_track('F.Cu', 'RF', (2, 5), (3, 5))                                  # nothing under it
    assert live.show_track('B.Cu', None, (2, 0), (3, 0)) and Board.selected == [tracks[2]]   # any net
    live.kicad = NS(get_board=lambda: Board(None))
    live.refresh()
    assert live.project_dir() is None and live.board_path() == 'amp.kicad_pcb'
    assert ipc._seg_dist((5, 5), (0, 0), (0, 0)) == pytest.approx(50 ** 0.5)          # a zero-length segment

    class Refused:
        def __init__(self, timeout_ms):
            pass

        def ping(self):
            raise ConnectionRefusedError('no socket')
    import kipy
    monkeypatch.setattr(kipy, 'KiCad', Refused)
    with pytest.raises(ipc.LinkError, match='Enable KiCad API'):
        ipc.Live.connect()
    monkeypatch.setattr(kipy, 'KiCad', type('NoBoard', (Refused,), dict(ping=lambda self: None,
                                                                         get_board=lambda self: 1 / 0)))
    with pytest.raises(ipc.LinkError, match='no board open'):
        ipc.Live.connect()
    monkeypatch.setitem(sys.modules, 'kipy', None)                  # not installed
    with pytest.raises(ipc.LinkError, match='pip install kicad-python'):
        ipc.Live.connect()


class FakeLive:
    """ipc.Live without KiCad: the board text of a file, a selection box, and the tracks it was asked to show."""
    def __init__(self, folder):
        self.folder, self.shown = folder, []

    def version(self):
        return 'test'

    def refresh(self):
        pass

    def board_text(self):
        return _text()

    def project_dir(self):
        return self.folder

    def board_path(self):
        return os.path.join(EX, 'rf_coupler.kicad_pcb')

    def stem(self):
        return 'rf_coupler'

    def selection_box(self):
        return (0.0, 0.0, 10.0, 5.0)

    def show_track(self, layer, net, a, b):
        self.shown.append((layer, net))
        return True


def test_window_offscreen(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    import time
    from PySide6.QtWidgets import QApplication
    from kicad2ems.kicadplugin.window import Window
    from kicad2ems.kicadplugin import model as md
    app = QApplication.instance() or QApplication([])
    live = FakeLive(str(tmp_path))
    w = Window(live)
    assert w.connect_kicad()
    t0 = time.time()
    while (w.plan is None or not w.loader.isFinished()) and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert w.plan is not None and w.lines['elements'] and w.map_bar.value() == 1
    w._region_from_selection()
    assert w.session.region == [-1.0, -1.0, 11.0, 6.0]                # the selection, 1 mm beyond
    assert w.plan.nets_in(w.session.region) == ['RF'] and w.plan.nets_in([40, 40, 50, 50]) == []
    w.scope.setCurrentIndex(w.scope.findData('region'))              # show the region's nets only
    t0 = time.time()
    while not w.loader.isFinished() and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert w.lines['elements'] and w.map_bar.value() == 1
    e = next(e for e in w.lines['elements'] if e['kind'] == 'trace' and e['layer'] == w.view.current_layer())
    w.port_btn.setChecked(True)
    mid = e['pts'][len(e['pts']) // 2]
    w._tool_click(mid[0], mid[1], 0.3)
    assert [p['name'] for p in w.session.ports] == ['P5']
    w.port_btn.setChecked(False)
    w._section_clicked(e['id'])
    assert live.shown == [(e['layer'], e['net'])]
    saved = json.load(open(os.path.join(str(tmp_path), 'kicad2ems', 'rf_coupler', 'rf_coupler_plugin.json')))
    assert saved['region'] == w.session.region and len(saved['ports']) == 1
    vals = w._values()
    assert vals['ports'] == [w.session.ports[0]['spec']] and os.path.exists(vals['board'])
    w.pick_btn.setChecked(True)                                        # a net run: pick the net, set it up
    w._tool_click(mid[0], mid[1], 0.3)
    assert w.picked == [e['net']] and not w.port_btn.isChecked()
    w._tool_click(mid[0], mid[1], 0.3)
    assert w.picked == []
    w._set_nets([e['net']])
    w._setup_nets()
    assert w.session.run_nets and w.session.cut_ports is False and w.session.nets == [e['net']]
    assert 'nets:' in w.setup_lab.text()
    assert w._values()['nets'][0] != 'auto'
    w._set_region(None, by_hand=True)
    assert w.session.run_nets == []
    _fake_results(str(tmp_path), 'x')
    w.results.show_results(md.results(str(tmp_path), 'x'), 'test')
    assert w.results.table.rowCount() == 3 and len(w.results.plot.listDataItems()) == 6
    w.close()
