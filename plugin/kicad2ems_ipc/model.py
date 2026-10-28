"""
The KiCad plugin's model (no Qt here: testable without a display): the board as kicad2ems sees it, its impedance
map, the region and ports the user picks, and the run they make.

    plan = Plan(text, path)                      # the board's text from KiCad's API (ipc.Live.board_text)
    for done, total, nets in plan.map_steps(cache=path): ...   # the impedance map, a net (or pair) at a time,
                                                 # over worker processes, kept in a cache file: plan.lines
    s = Session.load(path) or Session()          # region (KiCad mm, y down), ports, band, solvers
    s.ports.append(plan.port_at(x, y, layer, tol, s))   # a port where the user clicked (model mm, y up)
    s.use(plan.setup_for_nets(['D_P']))          # or: a run of a net / pair / set of nets (region, ports, parts)
    values = run_values(s, plan, text, folder)   # the board snapshot and the settings of a kicad2ems run
    res = results(out, stem)                     # after the run: its S-parameters; comparison(res): against the
                                                 # line model

Coordinates: the plan view and the line model are in model mm (y up); KiCad, the region and the port specs in
KiCad mm (y down): y_kicad = -y_model.
"""
import gzip, hashlib, json, os, pickle, re

import numpy as np
from shapely.geometry import Point, LineString

CACHE_VERSION = 1           # of the line model's map sections: a change of the model clears the cache
NET_MARGIN = 2.0            # mm of board kept around the nets of a net run (the planes and ground beside them)
MAX_EXTEND = 16             # nets a net run takes in through series parts, at most
PAIR_GAP = 3.0              # mm: open ends of a pair's legs closer than this make one differential port
LARGE = 150                 # signal nets past which the map starts with KiCad's selection, not every net (a board
                            # of 862 nets took 19 minutes, one of 94 nets 13 s)


class Plan:
    """A board read from KiCad: raw (the importer's form), m (board.Board with every net, no ports needed, no
    parts), board (m.to_dict(): what the plan view draws), nc (the net classes and their impedance targets,
    netclass.NetClasses), signal (the routed nets that are neither ground nor #PWR), disabled (the #PWR ones),
    lines (the impedance map so far: the line model's drawing, each section with its class and target).

    classes: {net: [class names]} as KiCad reports them (ipc.Live.net_classes); None: the project file next to
    the board (<board>.kicad_pro)."""
    def __init__(self, text, path='', classes=None):
        from kicad2ems.api.frontend import KiCadBoard
        from kicad2ems.api.frontend import Board
        from .netclass import NetClasses
        self.path = path
        self.notes = []
        self.raw = KiCadBoard(path=path, text=text)
        self.m = Board(self.raw, nets=None, need_ports=False, parts='none', log=self.notes.append)
        self.board = self.m.to_dict()
        nets = {n for _, n, _ in self.raw.items if n} | {v['net'] for v in self.raw.vias if v['net']}
        self.nc = NetClasses.from_map(classes, nets) if classes is not None else \
            NetClasses.from_project(os.path.splitext(path)[0] + '.kicad_pro', nets)
        routed = {t['net'] for t in self.m.prims.tracks if t['layer'] in self.m.cu_z}
        cand = sorted(n for n in routed - set(self.m.ground_nets) if n)
        self.disabled = [n for n in cand if self.nc.disabled(n)]
        self.signal = [n for n in cand if not self.nc.disabled(n)]
        self.lines = dict(format='kicad2ems-lines', version=1, elements=[], paths=[], z0_range=None,
                          port_z0={p.name: p.z0 for p in self.m.ports} or {'': 50.0}, relative=True)
        self.board_ports = [p.name for p in self.m.ports]

    def classes(self):
        """[(class, its signal nets, target label)]: the map's class filter."""
        return self.nc.summary(self.signal)

    def nets_of(self, classes):
        """The signal nets of the given classes."""
        cs = set(classes)
        return [n for n in self.signal if self.nc.of(n) in cs]

    def _units(self, nets):
        """The nets as mapping units: a pair's two legs together (they couple), every other net alone."""
        from .netclass import partner
        have, seen, out = set(nets), set(), []
        for n in nets:
            if n in seen:
                continue
            q = partner(n)
            unit = [n] + ([q] if q in have and q not in seen else [])
            seen.update(unit)
            out.append(unit)
        return out

    def _finish(self, els):
        """Map sections as the window shows them: numbered, with their net's class and target."""
        out = self.lines['elements']
        for e in els:
            e = dict(e, id=len(out))
            t = self.nc.target(e.get('net'))
            e['netclass'] = self.nc.of(e.get('net'))
            e['target'], e['tkind'] = (t[0], t[1]) if t else (None, None)
            out.append(e)
        z0 = [e['z0'] for e in out if e['z0']]
        self.lines['z0_range'] = [min(z0), max(z0)] if z0 else None

    def map_steps(self, nets=None, stop=lambda: False, jobs=None, cache=None):
        """Build the impedance map of `nets` (default: every signal net; #PWR nets never); yields (nets done, nets
        in all, the nets just done) as it goes, the sections in self.lines. The reference copper excludes every
        signal net, so a trace is referenced to the planes and grounds, never to its neighbours.

        Nets the cache (a file: MapCache) holds for this copper come first, at once. The rest are mapped a unit at
        a time (a net, or a pair's two legs: only a pair couples, so the map does not depend on how nets are
        grouped), over `jobs` worker processes (default: one per performance core; 1: here, in this process).
        A unit at a time was also the fastest: 4.9 s for KiCad's CM5 demo, against 5.8 s in groups of 24 nets."""
        from kicad2ems.api.frontend import RefCopper
        nets = [n for n in (self.signal if nets is None else nets) if not self.nc.disabled(n)]
        refcu = RefCopper(self.m, set(self.signal) | set(self.disabled))
        self.lines['elements'] = []
        units = self._units(nets)
        store = MapCache(cache, self, refcu) if cache else None
        done = 0
        todo = []
        for u in units:
            hit = store.get(u) if store else None
            if hit is None:
                todo.append(u)
            else:
                self._finish(hit)
                done += len(u)
        if done:
            yield done, len(nets), [n for u in units if u not in todo for n in u]
        try:
            for unit, els in _run_groups(self.m, refcu, todo, jobs, stop):
                self._finish(els)
                if store:
                    store.put(unit, els)
                done += len(unit)
                yield done, len(nets), unit
        finally:
            if store:
                store.save()

    def nets_in(self, region):
        """The signal nets with a track in a region (KiCad mm)."""
        if not region:
            return list(self.signal)
        from shapely.geometry import box
        b = box(region[0], -region[3], region[2], -region[1])          # model mm: y up
        sig = set(self.signal)
        return sorted({t['net'] for t in self.m.prims.tracks if t['net'] in sig and LineString(t['pts']).intersects(b)})

    # ---- simulating a set of nets ----------------------------------------------------------------------------
    def setup_for_nets(self, nets, margin=NET_MARGIN):
        """The set-up of a run of `nets` (a net, a pair, a set of nets; a pair's other leg comes along): the
        region (KiCad mm) around their copper, `margin` mm beyond; the parts on them; ports at their other
        components. Returns NetSetup. Raises ValueError when there is nothing to simulate.

        Components: a two-pad R / L / C (a value kicad2ems reads, not DNP) from a net to ground stays in as a
        shunt part; one in series takes the net on its far side in too (and so on, up to MAX_EXTEND nets), a
        lumped part in the run. Every other footprint (ICs, connectors, other two-pad parts) gets a port on each of
        its pads on the nets: one leg of a differential port where it has pads on both legs of a pair. A track end
        that reaches nothing (no pad, via or track) gets a port too. Nets the board's own port footprints sit on
        keep those ports instead."""
        from kicad2ems.api.frontend import kind_value
        from .netclass import partner
        sig, gnd = set(self.signal), set(self.m.ground_nets)
        want = []
        for n in nets:
            for q in (n, partner(n)):
                if q in sig and q not in want:
                    want.append(q)
        if not want:
            raise ValueError("no signal nets to simulate: pick routed nets that are neither ground nor #PWR")
        fps = [fp for fp in self.raw.footprints if fp['ref'] not in self.m.port_refs]
        kinds = {fp['ref']: kind_value(fp)[0] if not (fp.get('dnp') or fp.get('exclude_from_sim')) else None
                 for fp in fps}
        run, notes, parts, added = list(want), [], {}, []

        def two_pad(fp):
            pads = [q for q in fp['pads'] if q['layers']]
            return pads if len(pads) == 2 and kinds[fp['ref']] else None

        grow = True
        while grow:                                         # take in the nets beyond series parts
            grow = False
            for fp in fps:
                pads = two_pad(fp)
                if not pads or fp['ref'] in parts:
                    continue
                a, b = pads[0]['net'], pads[1]['net']
                if a not in run and b not in run:
                    continue
                far = b if a in run else a
                what = f"{fp['ref']} {kinds[fp['ref']]} {fp['value']}"
                if far in run:
                    parts[fp['ref']] = dict(ref=fp['ref'], kind=kinds[fp['ref']], value=fp['value'], role='series',
                                            nets=[a, b])
                elif far in gnd:
                    parts[fp['ref']] = dict(ref=fp['ref'], kind=kinds[fp['ref']], value=fp['value'], role='shunt',
                                            nets=[a, b])
                elif not far:
                    notes.append(f"{what}: its other pad is not connected - left out")
                    parts[fp['ref']] = None
                elif self.nc.disabled(far) or far not in sig and not any(n == far for _, n, _ in self.m.prims.items):
                    notes.append(f"{what}: to {far} (a supply, or no copper) - left out")
                    parts[fp['ref']] = None
                elif len(added) >= MAX_EXTEND:
                    notes.append(f"{what}: to {far} - not followed (past {MAX_EXTEND} nets taken in); a port there")
                    parts[fp['ref']] = None
                else:
                    run.append(far)
                    added.append(far)
                    parts[fp['ref']] = dict(ref=fp['ref'], kind=kinds[fp['ref']], value=fp['value'], role='series',
                                            nets=[a, b])
                    grow = True
        parts = {k: v for k, v in parts.items() if v}
        left_out = {fp['ref'] for fp in fps if two_pad(fp) and fp['ref'] not in parts
                    and any(q['net'] in run for q in fp['pads'])}
        board_net = {p.net for p in self.m.ports if p.net}
        # ---- where the nets end: the pads of the other footprints, and open track ends ----
        ends = []                                           # (net, layer, KiCad x, y, spec arg, what)
        for fp in fps:
            if fp['ref'] in parts:
                continue
            seen = set()                                    # one port per net on a footprint (a pass-through
            for q in fp['pads']:                            # ESD part has two pads on the line)
                if q['net'] in run and q['net'] not in board_net and q['layers'] and q['net'] not in seen:
                    seen.add(q['net'])
                    lay = next((l for l in q['layers'] if l.endswith('.Cu')), 'F.Cu')
                    if lay not in self.m.cu_z:
                        lay = next((l for l in self.m.cu_order if l in q['layers']), self.m.cu_order[0])
                    ends.append((q['net'], lay, float(q['xy'][0]), float(q['xy'][1]), f"{fp['ref']}.{q['name']}",
                                 f"pad {fp['ref']}.{q['name']}" + (' (left-out part)' if fp['ref'] in left_out else '')))
        for net, lay, x, y in self._open_ends(set(run) - board_net):
            ends.append((net, lay, x, -y, None, f"open end of {net}"))
        ports, used = [], set()
        n = max([int(m.group(1)) for nm in self.board_ports for m in [re.match(r'P(\d+)', nm)] if m], default=0)
        for i, e in enumerate(ends):
            if i in used:
                continue
            used.add(i)
            j = self._leg_partner(ends, i, used)
            n += 1
            legs = [(e, '')] if j is None else sorted([(e, ''), (ends[j], '')], key=lambda t: not _positive(t[0][0]))
            if j is not None:
                used.add(j)
                legs = [(legs[0][0], '+'), (legs[1][0], '-')]
            for (net, lay, x, y, pad, what), leg in legs:
                spec = f"P{n}{leg}={pad}" if pad else f"P{n}{leg}@{x:.4f},{y:.4f},{lay}"
                ports.append(dict(name=f"P{n}{leg}", spec=spec, net=net, layer=lay, x=x, y=-y, what=what))
        from kicad2ems.api.frontend import extra_port_fps
        ok = []
        for p in ports:
            try:
                extra_port_fps(self.raw, [p['spec']])
                ok.append(p)
            except ValueError as ex:
                notes.append(f"{p['name']} ({p['what']}): no port - {ex}")
        ports = _renumber_pairs(ok)
        if not ports and not board_net & set(run):
            raise ValueError(f"no ports for {', '.join(want)}: no other footprints and no open track ends on them")
        region = self._box_of(run, [fp for fp in fps if fp['ref'] in parts] +
                              [fp for fp in fps if any(q['net'] in run for q in fp['pads'])], margin)
        return NetSetup(nets=want, run_nets=run, region=region, ports=ports, parts=list(parts.values()), notes=notes,
                        board_ports=[p.name for p in self.m.ports if p.net in run],
                        grounds=sorted(gnd))

    def _open_ends(self, nets):
        """(net, layer, x, y model mm) of the track ends of `nets` that reach no pad, via or other track."""
        tracks = [t for t in self.m.prims.tracks if t['net'] in nets and t['layer'] in self.m.cu_z]
        pads = {}
        for fp in self.raw.footprints:
            for q in fp['pads']:
                if q['net'] in nets:
                    pads.setdefault(q['net'], []).append(q)
        out = []
        for i, t in enumerate(tracks):
            pts = np.asarray(t['pts'], float)
            for p in (pts[0], pts[-1]):
                pt, kpt = Point(p), Point(p[0], -p[1])
                if any(j != i and u['net'] == t['net'] and u['layer'] == t['layer'] and
                       LineString(u['pts']).distance(pt) <= u['width'] / 2 + 1e-4 for j, u in enumerate(tracks)):
                    continue
                if any(v['net'] == t['net'] and np.hypot(v['x'] - p[0], v['y'] - p[1]) <= v['size'] / 2 + 1e-4
                       for v in self.m.prims.vias):
                    continue
                if any(q['geom'].distance(kpt) <= 1e-4 for q in pads.get(t['net'], [])):
                    continue
                out.append((t['net'], t['layer'], float(p[0]), float(p[1])))
        return out

    @staticmethod
    def _leg_partner(ends, i, used):
        """The end of the pair's other leg that goes with end i: on the same footprint (a pad) or the nearest
        open end of the other leg within PAIR_GAP mm; None for a single-ended port."""
        from .netclass import partner
        net, lay, x, y, pad, _ = ends[i]
        q = partner(net)
        best = None
        for j, (n2, l2, x2, y2, pad2, _) in enumerate(ends):
            if j in used or n2 != q or bool(pad) != bool(pad2):
                continue
            if pad and pad.split('.')[0] != pad2.split('.')[0]:
                continue
            if not pad and l2 != lay:
                continue
            d = np.hypot(x2 - x, y2 - y)
            if (pad or d <= PAIR_GAP) and (best is None or d < best[0]):
                best = (d, j)
        return best[1] if best else None

    def _box_of(self, nets, fps, margin):
        """The region (KiCad mm) around the copper of `nets` and the pads of `fps`, `margin` mm beyond, within
        the board."""
        from shapely.ops import unary_union
        gs = [g for _, n, g in self.m.prims.items if n in nets]
        gs += [LineString(np.asarray(t['pts'], float)).buffer(t['width'] / 2) for t in self.m.prims.tracks
               if t['net'] in nets]
        b = unary_union(gs).bounds if gs else None
        if b is None:
            raise ValueError(f"no copper on {', '.join(nets)}")
        x0, y0, x1, y1 = b[0], -b[3], b[2], -b[1]                   # KiCad mm: y down
        for fp in fps:
            for q in fp['pads']:
                if q['net'] in nets or q['net'] in self.m.ground_nets:
                    bx = q['geom'].bounds
                    x0, y0, x1, y1 = min(x0, bx[0]), min(y0, bx[1]), max(x1, bx[2]), max(y1, bx[3])
        r = region_of_box(x0, y0, x1, y1, margin)
        o = self.m.outline.bounds                                   # model mm
        return [max(r[0], round(o[0], 4)), max(r[1], round(-o[3], 4)), min(r[2], round(o[2], 4)),
                min(r[3], round(-o[1], 4))]

    # ---- ports ----------------------------------------------------------------------------------------------
    def next_port(self, session):
        """The next free port number: past the board's own port footprints and the session's ports."""
        used = [int(m.group(1)) for n in self.board_ports + [p['name'] for p in session.ports]
                for m in [re.match(r'P(\d+)', n)] if m]
        return max(used, default=0) + 1

    def port_at(self, x, y, layer, tol, session, elements=None):
        """A port where the user clicked (model mm): on the pad under the point (P<n>=REF.PAD), else on the
        centre line of the nearest trace section of `layer` within `tol` (P<n>@x,y,layer: KiCad mm, pointing
        along the trace; elements: the map's sections, default plan.lines). Returns the port dict (name, spec, net,
        layer, x, y model mm, what) or raises ValueError with why not."""
        from kicad2ems.api.frontend import extra_port_fps
        n = self.next_port(session)
        kx, ky = x, -y
        for fp in self.raw.footprints:
            if fp['ref'] in self.m.port_refs:
                continue
            for q in fp['pads']:
                if layer in q['layers'] and q['geom'].distance(Point(kx, ky)) <= 1e-6:
                    spec = f"P{n}={fp['ref']}.{q['name']}"
                    extra_port_fps(self.raw, [spec])                # raises when it cannot be a port
                    cx, cy = q['xy']
                    return dict(name=f"P{n}", spec=spec, net=q['net'], layer=layer, x=float(cx), y=-float(cy),
                                what=f"pad {fp['ref']}.{q['name']}")
        best = None
        for e in (self.lines['elements'] if elements is None else elements):
            if e.get('kind') != 'trace' or e.get('layer') != layer or len(e['pts']) < 2:
                continue
            line = LineString(e['pts'])
            d = line.distance(Point(x, y))
            if d <= max(tol, (e.get('w') or 0) / 2) and (best is None or d < best[0]):
                best = (d, e, line)
        if best is None:
            raise ValueError(f"no trace or pad on {layer} here")
        _, e, line = best
        p = line.interpolate(line.project(Point(x, y)))
        spec = f"P{n}@{p.x:.4f},{-p.y:.4f},{layer}"
        extra_port_fps(self.raw, [spec])
        return dict(name=f"P{n}", spec=spec, net=e.get('net'), layer=layer, x=float(p.x), y=float(p.y),
                    what=f"{e.get('net')} trace {e.get('w') or 0:.3g} mm, Z0 {e['z0']:.1f} ohm" if e.get('z0')
                    else f"{e.get('net')} trace")

    def section_track(self, e):
        """(layer, net, KiCad mm start, end) of a map section, to find its track in KiCad."""
        pts = np.asarray(e['pts'], float)
        return e.get('layer'), e.get('net'), (pts[0][0], -pts[0][1]), (pts[-1][0], -pts[-1][1])


class NetSetup:
    """A run of a set of nets (Plan.setup_for_nets): nets (as picked, with pair partners), run_nets (those and the
    nets beyond series parts), region (KiCad mm), ports (port dicts, as Session.ports), parts ([dict(ref, kind,
    value, role 'series' | 'shunt', nets)]), board_ports (the board's own ports on the nets), grounds, notes."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def describe(self):
        lines = [f"nets: {', '.join(self.run_nets)}"]
        if self.parts:
            lines.append("parts: " + ", ".join(f"{p['ref']} {p['value']} ({p['role']})" for p in self.parts))
        lines.append("ports: " + ", ".join([f"{p['name']} {p['what']}" for p in self.ports] +
                                           [f"{n} (port footprint)" for n in self.board_ports]))
        return "\n".join(lines + self.notes)


def _positive(net):
    """Is a net a pair's positive leg (its name ends in + or P)?"""
    return bool(net) and net[-1] in '+Pp'


def _renumber_pairs(ports):
    """Ports numbered P1, P2 ... past the first one's number, in order, legs of a pair sharing a number."""
    out, last, num = [], None, None
    legs = {}
    for p in ports:
        legs.setdefault(re.match(r'P(\d+)', p['name']).group(1), []).append(p)
    for p in ports:
        m = re.match(r'P(\d+)([+-]?)$', p['name'])
        n0, leg = int(m.group(1)), m.group(2) if len(legs[m.group(1)]) == 2 else ''   # a lone leg: single-ended
        if num is None:
            num = n0
        elif n0 != last:
            num += 1
        last = n0
        name = f"P{num}{leg}"
        out.append(dict(p, name=name, spec=re.sub(r'^P\d+[+-]?', name, p['spec'])))
    return out


# ---- the map's work: groups of nets, here or in worker processes -------------------------------------------------
def _drawing(m, lm):
    """A line model's sections (drawing elements), with the pair impedance of coupled legs (Zdiff = 2 Zodd)."""
    from .netclass import partner
    for g in lm.groups:
        for k, pr in enumerate(g['pairs']):
            ea, eb = g['members'][k][0], g['members'][k + 1][0]
            if ea.net and partner(ea.net) == eb.net and pr.get('zo'):
                ea.zdiff = eb.zdiff = 2 * pr['zo']
    return lm.drawing(m.cu_z)['elements']


def _map_group(m, refcu, chunk):
    from kicad2ems.api.frontend import build_lines
    return _drawing(m, build_lines(m, nets=chunk, refcu=refcu))


_WORK = None                # a worker process's board and reference copper


def _worker_init(blob):
    global _WORK
    _WORK = pickle.loads(blob)


def _worker_map(chunk):
    return chunk, _map_group(*_WORK, chunk)


def _jobs():
    """Worker processes for the map: one per performance core (kicad2ems's rule, emdev's), at least 1."""
    from kicad2ems.api.frontend import core_threads
    return core_threads()


def _run_groups(m, refcu, groups, jobs, stop):
    """(group, its sections) for each group: in worker processes (each gets the board and the reference copper
    once), as they finish; here, in order, with one worker or one group."""
    jobs = jobs or _jobs()
    if jobs <= 1 or len(groups) <= 1:
        for g in groups:
            if stop():
                return
            yield g, _map_group(m, refcu, g)
        return
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    blob = pickle.dumps((m, refcu), protocol=pickle.HIGHEST_PROTOCOL)
    ctx = multiprocessing.get_context('spawn' if os.name == 'nt' else 'forkserver')   # never fork: Qt has threads
    done = set()
    ex = None
    try:
        ex = ProcessPoolExecutor(min(jobs, len(groups)), mp_context=ctx, initializer=_worker_init, initargs=(blob,))
        futs = {ex.submit(_worker_map, g): i for i, g in enumerate(groups)}
        for f in as_completed(futs):
            if stop():
                return
            out = f.result()
            done.add(futs[f])
            yield out
    except (OSError, EOFError, RuntimeError) as e:          # no workers here (BrokenProcessPool is a RuntimeError):
        import warnings                                     # the rest in this process
        warnings.warn(f"impedance map: worker processes failed ({type(e).__name__}: {e}); mapping in one process")
        for i, g in enumerate(groups):
            if i in done:
                continue
            if stop():
                return
            yield g, _map_group(m, refcu, g)
    finally:
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)


class MapCache:
    """Map sections kept between sessions (a gzipped JSON file): per net (a pair: both legs), under a hash of the
    net's own copper (tracks, vias, pads, zones) and of the board's reference copper, stack and outline. An edit
    re-maps the nets it touches; a change of the planes or the stack re-maps everything. Classes and targets are
    applied afresh: they are not part of a net's sections."""
    def __init__(self, path, plan, refcu):
        import shapely
        self.path, self.data, self.used = path, {}, {}
        m = plan.m
        h = hashlib.sha1(f"v{CACHE_VERSION}".encode())
        h.update(json.dumps(plan.board.get('stack'), sort_keys=True, default=str).encode())
        h.update(shapely.to_wkb(m.outline))
        for layer in sorted(refcu.geom):
            h.update(layer.encode())
            h.update(shapely.to_wkb(refcu.geom[layer]))
        h.update(str(getattr(m, 'ground', '')).encode())
        self.base = h.hexdigest()
        self.own = {}                                           # net: its copper, as bytes
        for t in m.prims.tracks:
            self.own.setdefault(t['net'], []).append(repr((t['layer'], round(float(t['width']), 6),
                                                          np.round(np.asarray(t['pts'], float), 6).tolist())).encode())
        for v in m.prims.vias:
            self.own.setdefault(v['net'], []).append(repr((round(v['x'], 6), round(v['y'], 6), v['size'], v['drill'],
                                                          sorted(v['layers']))).encode())
        for layer, net, g in m.prims.items:
            self.own.setdefault(net, []).append(layer.encode() + shapely.to_wkb(g))
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as fh:
                d = json.load(fh)
            if d.get('base') == self.base:
                self.data = d.get('units', {})
        except (OSError, ValueError, EOFError):
            pass

    def key(self, unit):
        h = hashlib.sha1(self.base.encode())
        for n in sorted(unit):
            h.update(b'\0net\0' + n.encode())
            for b in sorted(self.own.get(n, [])):
                h.update(b)
        return h.hexdigest()

    def get(self, unit):
        k = self.key(unit)
        if k in self.data:
            self.used[k] = self.data[k]
            return self.data[k]
        return None

    def put(self, unit, els):
        self.used[self.key(unit)] = [{k: v for k, v in e.items() if k not in ('id', 'netclass', 'target', 'tkind')}
                                     for e in els]

    def save(self):
        """Write the units used or made this time (with any others of this copper, kept for other filters)."""
        units = dict(self.data, **self.used)
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.part'
        with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
            json.dump(dict(base=self.base, units=units), fh)
        os.replace(tmp, self.path)


# ---- the session: what the user set up, kept next to the board -------------------------------------------------
class Session:
    """region (x0, y0, x1, y1 KiCad mm, or None), ports ([dict(name, spec, ...)]), cut_ports, fmin / fmax (GHz),
    solvers, classes (the net classes the map shows; None: the window's default), nets (the nets picked for a net
    run, [] for none), run_nets (those and the nets beyond their series parts: the run's nets with the grounds;
    [] for kicad2ems's auto), margin (mm around them), saved as <project>/kicad2ems/<board>/<board>_plugin.json."""
    KEYS = ('region', 'ports', 'cut_ports', 'fmin', 'fmax', 'solvers', 'classes', 'nets', 'run_nets', 'margin')

    def __init__(self, **kw):
        self.region, self.ports, self.cut_ports = None, [], True
        self.fmin, self.fmax, self.solvers, self.classes = 0.5, 10.0, ['auto'], None
        self.nets, self.run_nets, self.margin = [], [], NET_MARGIN
        for k, v in kw.items():
            if k in self.KEYS:
                setattr(self, k, v)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.KEYS}

    def use(self, setup):
        """Take a net run's set-up (NetSetup): its region, ports and nets; no cut ports (the nets end inside)."""
        self.region, self.ports, self.cut_ports = list(setup.region), list(setup.ports), False
        self.nets, self.run_nets = list(setup.nets), list(setup.run_nets)

    def drop_nets(self):
        """Back to kicad2ems's auto nets (the region or the ports were set by hand)."""
        self.nets, self.run_nets = [], []

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            json.dump(self.to_dict(), fh, indent=1)

    @classmethod
    def load(cls, path):
        try:
            with open(path) as fh:
                return cls(**json.load(fh))
        except (OSError, ValueError, TypeError):
            return None


def outside(session, port):
    """Is a port (model mm) outside the session's region? (kicad2ems leaves such ports out of the run.)"""
    r = session.region
    return bool(r) and not (r[0] <= port['x'] <= r[2] and r[1] <= -port['y'] <= r[3])


def session_path(folder, stem):
    return os.path.join(folder, f"{stem}_plugin.json")


def work_folder(project_dir, stem):
    """Where the plugin keeps a board's session and runs: <project>/kicad2ems/<board stem>."""
    return os.path.join(project_dir or os.getcwd(), 'kicad2ems', stem)


def region_of_box(x0, y0, x1, y1, margin=1.0):
    """A region (KiCad mm) from two corners, grown by `margin` mm on every side."""
    return [round(min(x0, x1) - margin, 4), round(min(y0, y1) - margin, 4),
            round(max(x0, x1) + margin, 4), round(max(y0, y1) + margin, 4)]


def run_values(session, plan, text, folder, stem):
    """The settings of a kicad2ems run of the session: the board as KiCad has it now (unsaved edits included),
    written to <folder>/<stem>.kicad_pcb; results in <folder>/out. Nets: the session's run_nets with the ground
    nets, else auto (those under the ports + ground). A net run also solves the line model, as a reference
    (fast = auto: compared in the report and the window; a warning, not a failure, when it cannot run)."""
    os.makedirs(folder, exist_ok=True)
    board = os.path.join(folder, f"{stem}.kicad_pcb")
    with open(board, 'w', encoding='utf-8') as fh:
        fh.write(text)
    if not (session.ports or plan.board_ports or (session.cut_ports and session.region)):
        raise ValueError("no ports: add ports (click a trace or a pad), or a region with ports at its edge")
    nets = sorted(set(session.run_nets) | set(plan.m.ground_nets)) if session.run_nets else ['auto']
    vals = dict(board=board, out=os.path.join(folder, 'out'), nets=nets, fmin=float(session.fmin),
                fmax=float(session.fmax), ports=[p['spec'] for p in session.ports])
    if session.region:
        vals['region'] = [float(v) for v in session.region]
        vals['cut_ports'] = bool(session.cut_ports)
    if session.solvers and session.solvers != ['auto']:
        vals['solvers'] = list(session.solvers)
    if session.run_nets and not set(vals.get('solvers', ['auto'])) & {'auto', 'lines'}:
        vals['fast'] = 'auto'
    return vals


def write_config(values, path):
    """A kicad2ems TOML config of the run's settings (kicad2ems-gui opens it)."""
    from kicad2ems.api.frontend import toml_value
    lines = ["# written by the kicad2ems KiCad plugin"]
    lines += [f"{k} = {toml_value(v)}" for k, v in values.items() if v not in (None, '', [])]
    with open(path, 'w') as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def legend(lines):
    """(reference ohm, half span ohm, low, high) of the map's colours."""
    from kicad2ems.api.frontend import overlay as ov
    ref = ov.reference(lines)
    return ref, ov.span(lines, ref), *(lines.get('z0_range') or (ref, ref))


def z0_at(lines, x, y, layer, tol):
    """The map section under a point (model mm) and its description, or (None, '')."""
    from kicad2ems.api.frontend import overlay as ov
    i = ov.nearest(lines['elements'], x, y, tol, layer=layer)
    return (i, ov.describe(lines['elements'][i])) if i is not None else (None, '')



# ---- results: the run's S-parameters, each solver against the line model ------------------------------------------
def results(out_dir, stem):
    """The S-parameters of a finished run (<out>/<stem>_run.json and its Touchstone files), or None: dict(freq Hz,
    solved [dict(key, label, names, S, line: is it the line model)], reference (the first solver's key),
    compare {key: the run's table of it against the reference}, warnings)."""
    from kicad2ems.api.frontend import read_touchstone
    try:
        with open(os.path.join(out_dir, f"{stem}_run.json")) as fh:
            sm = json.load(fh)
    except (OSError, ValueError):
        return None
    solved, freq = [], None
    for key, e in (sm.get('solved') or {}).items():
        try:
            f, S = read_touchstone(os.path.join(out_dir, e['touchstone']))
        except (OSError, ValueError, KeyError):
            continue
        freq = f if freq is None else freq
        solved.append(dict(key=key, label=e.get('label', key), names=e.get('names', []), S=S, f=f,
                           line=key in ('qucs', 'scikit'), seconds=e.get('seconds')))
    if not solved:
        return None
    return dict(freq=freq, solved=solved, reference=(sm.get('results') or {}).get('reference', solved[0]['key']),
                compare={k: e['compare'] for k, e in sm['solved'].items() if e.get('compare')},
                warnings=sm.get('warnings', []))


def terms(names):
    """[(i, j, label)] of the S-parameters worth plotting: each port's reflection and the transmission between
    each two ports; of a differential port its differential mode only (names ending d / c, mixed-mode)."""
    keep = [i for i, n in enumerate(names) if not (n.endswith('c') and n[:-1] + 'd' in names)]
    lab = {i: names[i][:-1] if names[i].endswith('d') and names[i][:-1] + 'c' in names else names[i] for i in keep}
    dd = {i: names[i].endswith('d') and names[i][:-1] + 'c' in names for i in keep}
    out = []
    for a, i in enumerate(keep):
        for j in keep[a:]:
            mode = 'dd' if dd[i] and dd[j] else ''
            out.append((j, i, f"S{mode}({lab[j]},{lab[i]})"))
    return out


def comparison(res, floor=-20.0):
    """[(term, max |S| difference in dB where the full-wave |S| is above `floor` dB (None: nowhere), at GHz,
    max |dS| (the complex difference, linear))] of each full-wave solver against the line model. Below the floor
    (a stop band, a well-matched reflection) a dB difference says little; |dS| covers it."""
    if not res:
        return []
    line = next((s for s in res['solved'] if s['line']), None)
    out = []
    for s in res['solved']:
        if s['line'] or line is None or s['names'] != line['names']:
            continue
        for i, j, lab in terms(s['names']):
            a = s['S'][:, i, j]
            b = np.interp(s['f'], line['f'], line['S'][:, i, j].real) + \
                1j * np.interp(s['f'], line['f'], line['S'][:, i, j].imag)
            da = 20 * np.log10(np.maximum(np.abs(a), 1e-9))
            db = 20 * np.log10(np.maximum(np.abs(b), 1e-9))
            ok = da > floor
            d = np.where(ok, np.abs(da - db), -1)
            k = int(np.argmax(d))
            out.append((f"{s['label']}: {lab}", float(d[k]) if ok.any() else None, float(s['f'][k] / 1e9),
                        float(np.nanmax(np.abs(a - b)))))
    return out
