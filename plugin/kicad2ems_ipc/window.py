"""
The plugin's window: the board KiCad has open, seen from above, its traces coloured by their impedance (Z0) and
read on hover; a region to simulate (drawn, taken from KiCad's selection, or set up around a set of nets with
ports at their components) and ports (clicked on traces and pads); a run of kicad2ems on them, its S-parameters
against the line model's, or the same set-up opened in kicad2ems's GUI.

    python plugin/kicad2ems_plugin.py        # what KiCad's toolbar button runs (install.py)
"""
import os, sys

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QProcess, QUrl
from PySide6.QtGui import QColor, QIcon, QDesktopServices, QLinearGradient, QBrush, QPainter
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
                               QPushButton, QCheckBox, QDoubleSpinBox, QComboBox, QListWidget, QListWidgetItem,
                               QProgressBar, QSplitter, QScrollArea, QMessageBox, QFormLayout, QTabWidget,
                               QTableWidget, QTableWidgetItem, QHeaderView)
import pyqtgraph as pg

from kicad2ems_gui.planview import PlanView
from . import model as md


def solver_choices():
    """The solver menu: auto, each full-wave solver installed (kicad2ems's registry), the line model."""
    from kicad2ems.api.frontend import registry
    return [('auto (the fastest full-wave + line model)', ['auto'])] + \
        [(sv.label, [n]) for n, sv in registry().items() if sv.consumes == 'board'] + [('line model only', ['lines'])]


SOLVERS = solver_choices()


class Loader(QThread):
    """Reads the board (Plan) and maps the impedance of `nets`, a group of nets at a time, off the GUI thread. With
    a plan already read: only the map. nets None: none yet (the window chooses once the board is read)."""
    loaded = Signal(object)                 # the Plan
    progress = Signal(object, object)       # the group's nets, its map sections
    failed = Signal(str)

    def __init__(self, text, path, plan=None, nets=None, classes=None, cache=None):
        super().__init__()
        self.text, self.path, self.plan, self.nets, self.classes = text, path, plan, nets, classes
        self.cache, self._stop = cache, False

    def stop(self):
        self._stop = True

    def run(self):
        plan = self.plan
        if plan is None:
            try:
                plan = md.Plan(self.text, self.path, classes=self.classes)
            except Exception as e:                                  # a board kicad2ems cannot read: say why
                self.failed.emit(f"{type(e).__name__}: {e}")
                return
            self.loaded.emit(plan)
        if not self.nets:
            return
        try:
            n = 0
            for _, _, chunk in plan.map_steps(nets=self.nets, stop=lambda: self._stop, cache=self.cache):
                new = plan.lines['elements'][n:]
                n = len(plan.lines['elements'])
                self.progress.emit(list(chunk), list(new))
        except Exception as e:
            self.failed.emit(f"impedance map: {type(e).__name__}: {e}")


class Legend(QWidget):
    """The colour scale of the map: blue below each section's target impedance (its net class's, else the
    reference), grey on it, red above; `half` a fraction of the target."""
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(30)
        self.half = 0.1

    def set(self, half):
        self.half = half
        self.update()

    def paintEvent(self, ev):
        from kicad2ems.api.frontend import overlay as ov
        p = QPainter(self)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, w, 0)
        for i in range(11):
            r, gg, b = ov.z0_colour((i / 5 - 1) * self.half, 0.0, self.half)
            g.setColorAt(i / 10, QColor.fromRgbF(r, gg, b))
        p.fillRect(0, 0, w, h - 14, QBrush(g))
        p.setPen(QColor('#333'))
        for frac, text in ((0.0, f"{-self.half * 100:.0f} %"), (0.5, 'on target'), (1.0, f"+{self.half * 100:.0f} %")):
            x = int(frac * (w - 1))
            p.drawText(max(0, min(w - 60, x - 26)), h - 2, text)


class Results(QWidget):
    """A run's S-parameters: |S| in dB of each full-wave solver (solid) and of the line model (dashed, the same
    colour per term), and the largest differences between them."""
    COLOURS = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd', '#ff7f0e', '#17becf', '#8c564b', '#e377c2']

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        self.title = QLabel('No results yet: set up a run and press Run')
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.plot = pg.PlotWidget(background='w')
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel('bottom', 'frequency', 'GHz')
        self.plot.setLabel('left', '|S|', 'dB')
        self.plot.addLegend(offset=(-10, 10))
        v.addWidget(self.plot, 3)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['against the line model', 'max d|S| dB', 'at GHz', 'max |dS|'])
        self.table.horizontalHeaderItem(1).setToolTip('The largest difference of |S| in dB where the full-wave |S| '
                                                      'is above -20 dB (below that a dB difference says little)')
        self.table.horizontalHeaderItem(3).setToolTip('The largest complex difference of S (linear): phase too')
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        v.addWidget(self.table, 1)
        self.res = None

    def show_results(self, res, title=''):
        self.res = res
        self.plot.clear()
        self.table.setRowCount(0)
        if not res:
            self.title.setText('No results: the run solved nothing')
            return
        full = [s for s in res['solved'] if not s['line']]
        line = next((s for s in res['solved'] if s['line']), None)
        styles = [Qt.SolidLine, Qt.DotLine, Qt.DashDotLine]
        ts = md.terms(res['solved'][0]['names'])
        for k, (i, j, lab) in enumerate(ts):
            c = self.COLOURS[k % len(self.COLOURS)]
            for n, sv in enumerate(full):
                if sv['names'] == res['solved'][0]['names']:
                    self.plot.plot(sv['f'] / 1e9, _db(sv['S'][:, i, j]), name=f"{lab} {sv['key']}",
                                   pen=pg.mkPen(c, width=2, style=styles[n % len(styles)]))
            if line is not None and line['names'] == res['solved'][0]['names']:
                self.plot.plot(line['f'] / 1e9, _db(line['S'][:, i, j]), name=f"{lab} line model",
                               pen=pg.mkPen(c, width=1.5, style=Qt.DashLine))
        rows = md.comparison(res)
        self.table.setRowCount(len(rows))
        for r, (lab, ddb, f, dv) in enumerate(rows):
            for c, text in enumerate((lab, '-' if ddb is None else f"{ddb:.2f}", f"{f:.3g}", f"{dv:.3f}")):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, it)
        what = ', '.join(f"{s['label']}" + (f" ({s['seconds']:.0f} s)" if s.get('seconds') else '') for s in res['solved'])
        self.title.setText((title + ': ' if title else '') + what +
                           ('' if line else '  - no line model result to compare with') +
                           ''.join(f"\n{w}" for w in res.get('warnings', [])[:3]))


def _db(x):
    import numpy as np
    return 20 * np.log10(np.maximum(np.abs(x), 1e-9))


class PluginPlan(PlanView):
    """The plan view with the plugin's tools: hover a section to read its Z0, click it to select its track in
    KiCad; 'region': two clicks give the corners; 'port': a click puts a port on the trace or pad under it; 'net':
    a click picks the net of the trace under it."""
    clicked_at = Signal(float, float, float)        # x, y (model mm), the tolerance (mm) at this zoom
    copper_colour, other_colour = QColor(214, 140, 64, 70), QColor(214, 140, 64, 20)   # faint: the map stands out

    def __init__(self):
        super().__init__()
        self.mode, self.region, self.ports, self.corner = 'inspect', None, [], None
        self._over = []
        self.mesh.hide()
        for w in self.findChildren(QLabel):
            if w.text() == 'Mesh':
                w.hide()
        self.band = pg.PlotCurveItem(pen=pg.mkPen('#2a7', width=1.5, style=Qt.DashLine))
        self.band.setZValue(40)
        self.plot.addItem(self.band)

    def set_mode(self, mode):
        self.mode, self.corner = mode, None
        self.band.setData([], [])
        self.plot.setCursor(Qt.CrossCursor if mode != 'inspect' else Qt.ArrowCursor)

    def redraw(self, *a):
        super().redraw(*a)
        for it in self._over:
            self.plot.removeItem(it)
        self._over = []
        if self.region:
            x0, y0, x1, y1 = self.region                            # KiCad mm: y down
            c = pg.PlotCurveItem([x0, x1, x1, x0, x0], [-y0, -y0, -y1, -y1, -y0], pen=pg.mkPen('#2a7', width=2))
            c.setZValue(40)
            self.plot.addItem(c)
            self._over.append(c)
        lay = self.current_layer()
        for p in self.ports:
            if p['layer'] != lay:
                continue
            t = pg.TextItem(p['name'], color='w', fill=pg.mkBrush('#c0392b'), anchor=(0.5, 0.5))
            t.setPos(p['x'], p['y'])
            t.setZValue(45)
            self.plot.addItem(t)
            self._over.append(t)

    def _view_point(self, scene_pos):
        vb = self.plot.getPlotItem().vb
        p = vb.mapSceneToView(scene_pos)
        return p.x(), p.y(), 6 * vb.viewPixelSize()[0]

    def _moved(self, pos):
        if self.mode == 'region' and self.corner is not None:
            x, y, _ = self._view_point(pos)
            x0, y0 = self.corner
            self.band.setData([x0, x, x, x0, x0], [y0, y0, y, y, y0])
        super()._moved(pos)

    def _clicked(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        if self.mode == 'inspect':
            return super()._clicked(ev)
        x, y, tol = self._view_point(ev.scenePos())
        self.clicked_at.emit(x, y, tol)


class Window(QMainWindow):
    def __init__(self, live=None):
        super().__init__()
        self.live, self.plan, self.loader, self.proc, self.run_out = live, None, None, None, None
        self.text, self.session, self.folder, self.stem = None, md.Session(), None, 'board'
        self.lines = dict(format='kicad2ems-lines', version=1, elements=[], paths=[], port_z0={'': 50.0},
                          relative=True)
        self.store, self.mapped = {}, set()             # map sections per net, the nets mapped so far
        self.setWindowTitle('kicad2ems for KiCad')
        icon = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gui', 'kicad2ems.svg')
        if os.path.exists(icon):
            self.setWindowIcon(QIcon(icon))
        split = QSplitter()
        self.view = PluginPlan()
        self.view.selected.connect(self._section_clicked)
        self.view.clicked_at.connect(self._tool_click)
        self.results = Results()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.view, 'Board')
        self.tabs.addTab(self.results, 'S-parameters')
        split.addWidget(self.tabs)
        side = QWidget()
        lay = QVBoxLayout(side)
        lay.addWidget(self._board_box())
        lay.addWidget(self._map_box())
        lay.addWidget(self._nets_box())
        lay.addWidget(self._region_box())
        lay.addWidget(self._ports_box())
        lay.addWidget(self._run_box())
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(side)
        scroll.setMinimumWidth(360)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        self.setCentralWidget(split)
        self.resize(1400, 860)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_run)
        self.map_timer = QTimer(self)                   # map chunks come faster than a redraw: one every 0.25 s
        self.map_timer.setSingleShot(True)
        self.map_timer.setInterval(250)
        self.map_timer.timeout.connect(self._show_map)

    # ---- the side panel ----
    def _board_box(self):
        g = QGroupBox('Board')
        v = QVBoxLayout(g)
        self.board_lab = QLabel('connecting to KiCad ...')
        self.board_lab.setWordWrap(True)
        v.addWidget(self.board_lab)
        b = QPushButton('Reload from KiCad')
        b.setToolTip('Read the board again as KiCad has it now (unsaved edits included)')
        b.clicked.connect(self.reload)
        v.addWidget(b)
        return g

    def _map_box(self):
        g = QGroupBox('Impedance map')
        v = QVBoxLayout(g)
        self.map_bar = QProgressBar()
        self.map_bar.setFormat('%v / %m nets')
        v.addWidget(self.map_bar)
        self.legend = Legend()
        v.addWidget(self.legend)
        self.map_lab = QLabel('Traces coloured by their impedance against their net class\'s target (50 ohm SE '
                              'without one): hover one to read it, click it to select its track in KiCad')
        self.map_lab.setWordWrap(True)
        self.map_lab.setStyleSheet('color: #555')
        v.addWidget(self.map_lab)
        row = QHBoxLayout()
        row.addWidget(QLabel('Show'))
        self.scope = QComboBox()
        for label, key in (('all nets', 'all'), ('nets selected in KiCad', 'selection'), ('nets in the region', 'region')):
            self.scope.addItem(label, key)
        self.scope.setToolTip('Which nets the map shows (within the classes ticked below)')
        self.scope.currentIndexChanged.connect(self._filter_changed)
        row.addWidget(self.scope, 1)
        b = QPushButton('Update')
        b.setToolTip("Read KiCad's selection again")
        b.clicked.connect(self._filter_changed)
        row.addWidget(b)
        v.addLayout(row)
        self.class_list = QListWidget()
        self.class_list.setMinimumHeight(90)
        self.class_list.setToolTip('Net classes: tick the ones to map. The target impedance comes from the class '
                                   'name (50R_SE, 100R_DIFF, 90ohm ...); #PWR nets are never mapped')
        self.class_list.itemChanged.connect(self._filter_changed)
        v.addWidget(self.class_list)
        return g

    def _nets_box(self):
        g = QGroupBox('Nets to simulate')
        v = QVBoxLayout(g)
        lab = QLabel('A net, a pair or a set of nets: the region goes around them, their R / L / C parts stay in '
                     '(in series: the nets beyond come too), and every other component on them gets a port')
        lab.setWordWrap(True)
        lab.setStyleSheet('color: #555')
        v.addWidget(lab)
        row = QHBoxLayout()
        b = QPushButton('From KiCad selection')
        b.setToolTip("The nets of the items selected in KiCad (a pair's other leg comes along)")
        b.clicked.connect(self._nets_from_selection)
        row.addWidget(b)
        self.pick_btn = QPushButton('Pick')
        self.pick_btn.setCheckable(True)
        self.pick_btn.setToolTip('Click traces on the plan: each click adds its net (or takes it out again)')
        row.addWidget(self.pick_btn)
        b = QPushButton('Clear')
        b.clicked.connect(lambda: self._set_nets([]))
        row.addWidget(b)
        v.addLayout(row)
        self.nets_lab = QLabel('none')
        self.nets_lab.setWordWrap(True)
        v.addWidget(self.nets_lab)
        row = QHBoxLayout()
        row.addWidget(QLabel('Board around them'))
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0.0, 50.0)
        self.margin.setDecimals(1)
        self.margin.setSuffix(' mm')
        self.margin.setValue(md.NET_MARGIN)
        self.margin.setToolTip('How far the region reaches beyond the nets\' copper and parts (the planes and '
                               'ground beside them)')
        row.addWidget(self.margin)
        self.setup_btn = QPushButton('Set up region and ports')
        self.setup_btn.clicked.connect(self._setup_nets)
        row.addWidget(self.setup_btn)
        v.addLayout(row)
        self.setup_lab = QLabel(' ')
        self.setup_lab.setWordWrap(True)
        self.setup_lab.setStyleSheet('color: #333')
        v.addWidget(self.setup_lab)
        self.picked = []
        return g

    def _region_box(self):
        g = QGroupBox('Region to simulate')
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.draw_btn = QPushButton('Draw')
        self.draw_btn.setCheckable(True)
        self.draw_btn.setToolTip('Click two corners on the plan')
        row.addWidget(self.draw_btn)
        b = QPushButton('From KiCad selection')
        b.setToolTip('The box around the items selected in KiCad, 1 mm beyond')
        b.clicked.connect(self._region_from_selection)
        row.addWidget(b)
        b = QPushButton('Clear')
        b.clicked.connect(lambda: self._set_region(None, by_hand=True))
        row.addWidget(b)
        v.addLayout(row)
        self.region_lab = QLabel('the whole board')
        v.addWidget(self.region_lab)
        self.cut = QCheckBox('Ports where signal tracks cross the region edge')
        self.cut.toggled.connect(self._cut_changed)
        v.addWidget(self.cut)
        return g

    def _ports_box(self):
        g = QGroupBox('Ports')
        v = QVBoxLayout(g)
        self.port_btn = QPushButton('Add: click a trace or a pad')
        self.port_btn.setCheckable(True)
        v.addWidget(self.port_btn)
        self.port_list = QListWidget()
        self.port_list.setMinimumHeight(110)
        v.addWidget(self.port_list)
        b = QPushButton('Remove the selected port')
        b.clicked.connect(self._remove_port)
        v.addWidget(b)
        self.draw_btn.toggled.connect(lambda on: self._tool('region', on))
        self.port_btn.toggled.connect(lambda on: self._tool('port', on))
        self.pick_btn.toggled.connect(lambda on: self._tool('net', on))
        return g

    def _run_box(self):
        g = QGroupBox('Simulation')
        f = QFormLayout(g)
        self.fmin, self.fmax = QDoubleSpinBox(), QDoubleSpinBox()
        for w, val in ((self.fmin, 0.5), (self.fmax, 10.0)):
            w.setRange(0.001, 500.0)
            w.setDecimals(3)
            w.setSuffix(' GHz')
            w.setValue(val)
            w.valueChanged.connect(self._save)
        f.addRow('f min', self.fmin)
        f.addRow('f max', self.fmax)
        self.solver = QComboBox()
        for label, names in SOLVERS:
            self.solver.addItem(label, names)
        self.solver.currentIndexChanged.connect(self._save)
        f.addRow('Solver', self.solver)
        row = QHBoxLayout()
        self.run_btn = QPushButton('Run')
        self.run_btn.clicked.connect(self.run)
        row.addWidget(self.run_btn)
        self.stop_btn = QPushButton('Stop')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.stop_btn)
        b = QPushButton('Open in kicad2ems')
        b.setToolTip('The same set-up in kicad2ems\'s own window: every setting, the results and the field views')
        b.clicked.connect(self.open_gui)
        row.addWidget(b)
        f.addRow(row)
        self.run_bar = QProgressBar()
        self.run_bar.setRange(0, 1000)
        f.addRow(self.run_bar)
        self.run_lab = QLabel(' ')
        self.run_lab.setWordWrap(True)
        f.addRow(self.run_lab)
        self.report_btn = QPushButton('Open the report')
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(self._open_report)
        f.addRow(self.report_btn)
        self.report = None
        return g

    # ---- the board ----
    def connect_kicad(self):
        from .ipc import Live, LinkError
        if self.live is None:
            try:
                self.live = Live.connect()
            except LinkError as e:
                self.board_lab.setText(str(e))
                return False
        self.stem = self.live.stem()
        self.folder = md.work_folder(self.live.project_dir(), self.stem)
        self.session = md.Session.load(md.session_path(self.folder, self.stem)) or md.Session()
        self._show_session()
        self.reload()
        return True

    def reload(self):
        if self.live is None:
            return
        if self.loader is not None:
            self.loader.stop()
            self.loader.wait()
        try:
            self.live.refresh()
            self.text = self.live.board_text()
        except Exception as e:                                      # KiCad closed the board, or went away
            self.board_lab.setText(f"cannot read the board from KiCad: {type(e).__name__}: {e}")
            return
        self.board_lab.setText(f"{self.live.board_path()}\nKiCad {self.live.version()}: reading ...")
        self.lines['elements'], self.store, self.mapped = [], {}, set()
        self.map_bar.setValue(0)
        try:
            classes = self.live.net_classes()
        except Exception:                                           # an older KiCad: the project file instead
            classes = None
        self.loader = Loader(self.text, self.live.board_path(), classes=classes)
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(lambda msg: self.board_lab.setText(f"{self.live.board_path()}\n{msg}"))
        self.loader.start()

    def _loaded(self, plan):
        self.plan = plan
        self.lines['port_z0'] = dict(plan.lines['port_z0'])
        m = plan.m
        self.board_lab.setText(f"{self.live.board_path()}\nKiCad {self.live.version()}: {len(m.cu_order)} copper "
                               f"layers, {len(plan.signal)} signal nets, ground {', '.join(m.ground_nets) or '-'}")
        self.view.region, self.view.ports = self.session.region, self.session.ports
        self.view.set_board(plan.board, self.lines)
        self._list_classes()
        self._list_ports()
        self.picked = [n for n in self.session.nets if n in plan.signal]
        self._show_nets()
        res = md.results(os.path.join(self.folder, 'out'), self.stem) if self.folder else None
        if res:
            self.results.show_results(res, 'the last run')
        if len(plan.signal) > md.LARGE:
            self.map_lab.setText(f"{len(plan.signal)} signal nets: a large board. The map starts with the classes "
                                 f"that name an impedance (or KiCad's selection); tick others below (all of them may "
                                 f"take minutes)")
        self._filter_changed()

    def _list_classes(self):
        """The class filter: each class with its nets and target, ticked as the session had them (first time: all;
        on a large board the classes with a target, or none and KiCad's selection)."""
        rows = self.plan.classes()
        keep = self.session.classes
        if keep is None:
            targeted = [c for c, _, t in rows if t]
            keep = [c for c, _, _ in rows] if len(self.plan.signal) <= md.LARGE else targeted
            if len(self.plan.signal) > md.LARGE and not targeted:
                self.scope.blockSignals(True)
                self.scope.setCurrentIndex(self.scope.findData('selection'))
                self.scope.blockSignals(False)
        self.class_list.blockSignals(True)
        self.class_list.clear()
        for c, nets, t in rows:
            it = QListWidgetItem(f"{c}  -  {len(nets)} net{'s' if len(nets) != 1 else ''}" + (f", {t}" if t else ''))
            it.setData(Qt.UserRole, c)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if c in keep else Qt.Unchecked)
            self.class_list.addItem(it)
        if self.plan.disabled:
            it = QListWidgetItem(f"#PWR  -  {len(self.plan.disabled)} net(s), never mapped")
            it.setFlags(it.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsUserCheckable)
            it.setToolTip(', '.join(self.plan.disabled))
            self.class_list.addItem(it)
        self.class_list.blockSignals(False)

    def _checked_classes(self):
        out = []
        for i in range(self.class_list.count()):
            it = self.class_list.item(i)
            if it.data(Qt.UserRole) and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _visible_nets(self):
        """The nets the map shows: the ticked classes', within the scope (all, KiCad's selection, the region)."""
        nets = self.plan.nets_of(self._checked_classes())
        scope = self.scope.currentData()
        if scope == 'selection':
            try:
                sel = self.live.selection_nets() if self.live else set()
            except Exception:                                       # KiCad went away
                sel = set()
            nets = [n for n in nets if n in sel]
        elif scope == 'region':
            inside = set(self.plan.nets_in(self.session.region)) if self.session.region else set()
            nets = [n for n in nets if n in inside]
        return nets

    def _filter_changed(self, *_):
        """Show what the filter asks for; map (in the background) the nets of it that are not mapped yet."""
        if self.plan is None:
            return
        self.session.classes = self._checked_classes()
        self._save()
        want = self._visible_nets()
        missing = [n for n in want if n not in self.mapped]
        busy = self.loader is not None and self.loader.isRunning()
        if missing and not (busy and set(missing) <= set(self.loader.nets or [])):
            if busy:
                self.loader.stop()
                self.loader.wait()
            self.loader = Loader(None, None, plan=self.plan, nets=missing, cache=self._cache_path())
            self.loader.progress.connect(self._mapped)
            self.loader.finished.connect(self._map_done)
            self.loader.failed.connect(lambda msg: self.map_lab.setText(msg))
            self.loader.start()
        self._show_map()

    def _cache_path(self):
        """The map kept between sessions (model.MapCache), beside the board's session."""
        return os.path.join(self.folder, f"{self.stem}_impedance.json.gz") if self.folder else None

    def _mapped(self, chunk, new):
        for n in chunk:
            self.mapped.add(n)
            self.store.setdefault(n, [])
        for e in new:
            self.store.setdefault(e.get('net'), []).append(e)
        if not self.map_timer.isActive():
            self.map_timer.start()

    def _map_done(self):
        if self.map_timer.isActive():
            self.map_timer.stop()
            self._show_map()

    def _show_map(self):
        """The view's map: the stored sections of the visible nets, numbered as the view expects."""
        want = self._visible_nets()
        els = []
        for n in want:
            for e in self.store.get(n, []):
                els.append(dict(e, id=len(els)))
        self.lines['elements'] = els
        z0 = [e['z0'] for e in els if e.get('z0')]
        self.lines['z0_range'] = [min(z0), max(z0)] if z0 else None
        done = sum(1 for n in want if n in self.mapped)
        self.map_bar.setRange(0, max(len(want), 1))
        self.map_bar.setValue(done if want else 0)
        self.map_bar.setFormat('%v / %m nets' if want else 'no nets to show')
        self.legend.set(md.legend(self.lines)[1])
        self.view.redraw()

    # ---- tools ----
    def _tool(self, mode, on):
        if on:
            for m, other in (('region', self.draw_btn), ('port', self.port_btn), ('net', self.pick_btn)):
                if m != mode:
                    other.blockSignals(True)
                    other.setChecked(False)
                    other.blockSignals(False)
            self.tabs.setCurrentWidget(self.view)
        self.view.set_mode(mode if on else 'inspect')
        self.statusBar().showMessage({'region': 'Click the first corner of the region',
                                      'port': 'Click a trace or a pad: a port goes there',
                                      'net': 'Click a trace: its net is added (or taken out)'}.get(self.view.mode, ''))

    def _tool_click(self, x, y, tol):
        if self.plan is None:
            return
        if self.view.mode == 'region':
            if self.view.corner is None:
                self.view.corner = (x, y)
                self.statusBar().showMessage('Click the opposite corner')
                return
            x0, y0 = self.view.corner
            self.draw_btn.setChecked(False)                         # back to inspecting (clears the band)
            self._set_region(md.region_of_box(x0, -y0, x, -y, margin=0.0), by_hand=True)
        elif self.view.mode == 'net':
            from kicad2ems.api.frontend import overlay as ov
            i = ov.nearest(self.lines['elements'], x, y, tol, layer=self.view.current_layer())
            net = self.lines['elements'][i].get('net') if i is not None else None
            if not net:
                self.statusBar().showMessage('No mapped trace here (on the layer shown)', 5000)
                return
            from .netclass import partner
            pair = [n for n in (net, partner(net)) if n in self.plan.signal]
            self._set_nets([n for n in self.picked if n not in pair] if net in self.picked else self.picked + pair)
        elif self.view.mode == 'port':
            try:
                p = self.plan.port_at(x, y, self.view.current_layer(), tol, self.session,
                                      elements=[e for es in self.store.values() for e in es])
            except ValueError as e:
                self.statusBar().showMessage(f"No port here: {e}", 6000)
                return
            self.session.ports.append(p)
            if self.session.run_nets and p['net'] and p['net'] not in self.session.run_nets:
                self.session.run_nets.append(p['net'])              # a net run: its copper comes into the run
            self._list_ports()
            self.view.redraw()
            self._save()
            warn = '  - outside the region: left out of the run' if md.outside(self.session, p) else ''
            self.statusBar().showMessage(f"{p['name']} on {p['what']}  ({p['spec']}){warn}", 8000)

    def _set_region(self, region, by_hand=False):
        if by_hand and self.session.run_nets:                       # no longer a net run: kicad2ems's auto nets
            self.session.drop_nets()
            self.setup_lab.setText('(the region was changed by hand: the run takes the nets under the ports)')
        self.session.region = region
        self.view.region = region
        self.view.redraw()
        self._list_ports()
        if self.scope.currentData() == 'region':
            self._filter_changed()
        self._show_session()
        self._save()

    def _region_from_selection(self):
        box = self.live.selection_box() if self.live else None
        if box is None:
            self.statusBar().showMessage('Nothing is selected in KiCad', 5000)
            return
        self._set_region(md.region_of_box(*box, margin=1.0), by_hand=True)

    # ---- nets to simulate ----
    def _set_nets(self, nets):
        self.picked = list(dict.fromkeys(nets))
        self._show_nets()

    def _show_nets(self):
        self.nets_lab.setText(', '.join(self.picked) if self.picked else 'none')
        self.setup_btn.setEnabled(bool(self.picked))
        if self.session.run_nets and set(self.session.nets) == set(self.picked):
            self.setup_lab.setText(f"set up: {', '.join(self.session.run_nets)}; {len(self.session.ports)} port(s)")

    def _nets_from_selection(self):
        from .netclass import partner
        try:
            sel = self.live.selection_nets() if self.live else set()
        except Exception:                                           # KiCad went away
            sel = set()
        sig = set(self.plan.signal) if self.plan else set()
        nets = sorted({q for n in sel for q in (n, partner(n)) if q in sig})
        if not nets:
            self.statusBar().showMessage('No signal nets are selected in KiCad (select tracks, pads or a net)', 5000)
            return
        self._set_nets(nets)

    def _setup_nets(self):
        if self.plan is None or not self.picked:
            return
        try:
            setup = self.plan.setup_for_nets(self.picked, margin=self.margin.value())
        except ValueError as e:
            QMessageBox.warning(self, 'kicad2ems', str(e))
            return
        self.session.use(setup)
        self.session.margin = self.margin.value()
        self.view.region, self.view.ports = self.session.region, self.session.ports
        self.view.redraw()
        self._list_ports()
        self._show_session()
        self._save()
        if self.scope.currentData() == 'region':
            self._filter_changed()
        self.setup_lab.setText(setup.describe())

    def _cut_changed(self, on):
        self.session.cut_ports = on
        self._save()

    def _remove_port(self):
        it = self.port_list.currentItem()
        name = it.data(Qt.UserRole) if it else None
        if not name:
            return
        self.session.ports = [p for p in self.session.ports if p['name'] != name]
        self.view.ports = self.session.ports
        self._list_ports()
        self.view.redraw()
        self._save()

    def _list_ports(self):
        self.port_list.clear()
        for name in (self.plan.board_ports if self.plan else []):
            it = QListWidgetItem(f"{name}  (port footprint on the board)")
            it.setForeground(QColor('#777'))
            self.port_list.addItem(it)
        for p in self.session.ports:
            out = md.outside(self.session, p)
            it = QListWidgetItem(f"{p['name']}  {p['what']}  [{p['layer']}]" + ('  - outside the region' if out else ''))
            it.setData(Qt.UserRole, p['name'])
            if out:
                it.setForeground(QColor('#b04020'))
            self.port_list.addItem(it)

    def _section_clicked(self, i):
        if i is None or self.plan is None or self.live is None:
            return
        e = self.lines['elements'][i]
        layer, net, a, b = self.plan.section_track(e)
        try:
            found = self.live.show_track(layer, net, a, b)
        except Exception as ex:                                     # KiCad went away
            found = False
            self.statusBar().showMessage(f"KiCad: {type(ex).__name__}: {ex}", 5000)
        if found:
            self.statusBar().showMessage(f"selected in KiCad: the {net} track on {layer}", 4000)

    # ---- the session ----
    def _show_session(self):
        s = self.session
        self.region_lab.setText('the whole board' if not s.region else
                                f"x {s.region[0]:g} .. {s.region[2]:g}, y {s.region[1]:g} .. {s.region[3]:g} mm (KiCad)")
        for w, v in ((self.cut, bool(s.cut_ports)), (self.fmin, s.fmin), (self.fmax, s.fmax), (self.margin, s.margin)):
            w.blockSignals(True)
            (w.setChecked if isinstance(w, QCheckBox) else w.setValue)(v)
            w.blockSignals(False)
        i = next((k for k, (_, names) in enumerate(SOLVERS) if names == list(s.solvers)), 0)
        self.solver.blockSignals(True)
        self.solver.setCurrentIndex(i)
        self.solver.blockSignals(False)
        self.cut.setEnabled(bool(s.region))

    def _save(self, *_):
        s = self.session
        s.fmin, s.fmax, s.solvers = self.fmin.value(), self.fmax.value(), self.solver.currentData()
        self.cut.setEnabled(bool(s.region))
        if self.folder:
            s.save(md.session_path(self.folder, self.stem))

    # ---- runs ----
    def _values(self):
        if self.plan is None:
            raise ValueError('the board is not read yet')
        self._save()
        return md.run_values(self.session, self.plan, self.text, self.folder, self.stem)

    def run(self):
        from kicad2ems.api.frontend import Run
        try:
            vals = self._values()
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, 'kicad2ems', str(e))
            return
        md.write_config(vals, os.path.join(self.folder, f"{self.stem}.toml"))
        self.run_out = vals['out']
        r = Run(vals, self.run_out)
        from kicad2ems.api.frontend import EventTail
        self.tail = EventTail(r.events)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.finished.connect(self._finished)
        argv = r.argv()
        self.proc.start(argv[0], argv[1:])
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.report_btn.setEnabled(False)
        self.run_bar.setValue(0)
        self.run_lab.setText('starting ...')
        self.timer.start(300)

    def _poll_run(self):
        for ev in self.tail.read():
            k = ev.get('kind')
            if k == 'stage':
                self.run_lab.setText(ev.get('name', ''))
            elif k == 'log':
                self.run_lab.setText(ev.get('text', '')[:200])
            elif k == 'status' and ev.get('fraction') is not None:
                self.run_bar.setValue(int(1000 * ev['fraction']))
                eta = ev.get('eta')
                self.run_lab.setText(f"{ev.get('run', '')}: step {ev.get('step', 0):,}" +
                                     (f", ~{eta:.0f} s left" if eta else ''))
            elif k == 'file' and str(ev.get('path', '')).endswith('_report.html'):
                self.report = ev['path']

    def _finished(self, code, _status):
        self.timer.stop()
        self._poll_run()
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.run_bar.setValue(1000 if code == 0 else self.run_bar.value())
        self.report_btn.setEnabled(bool(self.report and os.path.exists(self.report)))
        if code == 0:
            self.run_lab.setText(f"done: results in {self.run_out}")
            res = md.results(self.run_out, self.stem)
            self.results.show_results(res, 'this run')
            if res:
                self.tabs.setCurrentWidget(self.results)
        else:
            tail = bytes(self.proc.readAll()).decode('utf-8', 'replace').strip().splitlines()[-4:]
            self.run_lab.setText("the run failed:\n" + "\n".join(tail))

    def stop(self):
        from kicad2ems.api.frontend import stop_runs
        if self.run_out:
            stop_runs(self.run_out)
        self.run_lab.setText('stopping ...')

    def open_gui(self):
        try:
            vals = self._values()
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, 'kicad2ems', str(e))
            return
        cfg = md.write_config(vals, os.path.join(self.folder, f"{self.stem}.toml"))
        QProcess.startDetached(sys.executable, ['-m', 'kicad2ems_gui', cfg])
        self.statusBar().showMessage(f"opened {cfg} in kicad2ems", 5000)

    def _open_report(self):
        if self.report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.report))

    def closeEvent(self, ev):
        if self.loader is not None:
            self.loader.stop()
            self.loader.wait(2000)
        super().closeEvent(ev)


def main(argv=None):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('kicad2ems for KiCad')
    w = Window()
    w.show()
    QTimer.singleShot(0, w.connect_kicad)
    return app.exec()
