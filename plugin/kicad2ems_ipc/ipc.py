"""
The plugin's link to KiCad through its IPC API (KiCad 9 and later, the kicad-python package: kipy). KiCad starts
a plugin with the socket and a token in its environment (KICAD_API_SOCKET, KICAD_API_TOKEN), which kipy reads; run
from a shell, it finds a running KiCad on its default socket.

    live = Live.connect()                 # raises LinkError with what to do when there is no KiCad to talk to
    text = live.board_text()              # the open board as KiCad has it now, unsaved edits included
    live.board_path(), live.project_dir()
    live.selection_box()                  # (x0, y0, x1, y1) KiCad mm of the selected items, or None
    live.net_classes()                    # {net: [class names]}, as KiCad resolves them
    live.show_track(layer, net, a, b)     # select the track a map section came from (KiCad mm), in KiCad

Lengths are nm in the API and mm here (KiCad coordinates, y down).
"""
import os

NM = 1e-6                   # mm per nm


class LinkError(RuntimeError):
    pass


class Live:
    def __init__(self, kicad):
        self.kicad = kicad
        self.board = kicad.get_board()

    @classmethod
    def connect(cls):
        """The KiCad that started the plugin (or the one running here) and its open board."""
        try:
            from kipy import KiCad
        except ImportError as e:
            raise LinkError("the kicad-python package is missing: pip install kicad-python") from e
        try:
            k = KiCad(timeout_ms=10000)
            k.ping()
        except Exception as e:                                      # no socket, refused, timed out
            raise LinkError(f"no KiCad to talk to ({type(e).__name__}: {e}). Open the board in KiCad's PCB "
                            f"editor and enable its API server: Preferences > Plugins > Enable KiCad API") from e
        try:
            return cls(k)
        except Exception as e:
            raise LinkError(f"KiCad has no board open in the PCB editor ({type(e).__name__}: {e})") from e

    def version(self):
        return str(self.kicad.get_version())

    def refresh(self):
        self.board = self.kicad.get_board()

    def board_text(self):
        return self.board.get_as_string()

    def project_dir(self):
        try:
            return self.board.get_project().path or None
        except Exception:                                           # a board without a project
            return None

    def board_path(self):
        d = self.project_dir()
        return os.path.join(d, self.board.name) if d else self.board.name

    def stem(self):
        return os.path.splitext(os.path.basename(self.board.name or 'board'))[0]

    def net_classes(self):
        """{net: [its net classes]} as KiCad resolves them (assignments and patterns; several classes per net in
        KiCad 9 and later: the constituents of its effective class)."""
        nets = list(self.board.get_nets())
        out = {}
        for name, nc in self.board.get_netclass_for_nets(nets).items():
            cons = [c for c in (nc.constituents or []) if c]
            out[name] = cons or [c.strip() for c in nc.name.split(',') if c.strip()]
        return out

    # ---- the selection ----
    def selection_box(self):
        """(x0, y0, x1, y1) KiCad mm around the items selected in KiCad, or None."""
        items = list(self.board.get_selection())
        boxes = []
        for it in items:
            try:
                b = self.board.get_item_bounding_box(it)
            except Exception:                                       # an item without a box (a net, a group...)
                continue
            if b is not None:
                boxes.append((b.pos.x * NM, b.pos.y * NM, (b.pos.x + b.size.x) * NM, (b.pos.y + b.size.y) * NM))
        if not boxes:
            return None
        bx = list(zip(*boxes))
        return min(bx[0]), min(bx[1]), max(bx[2]), max(bx[3])

    def selection_nets(self):
        out = set()
        for it in self.board.get_selection():
            net = getattr(it, 'net', None)
            if net is not None and getattr(net, 'name', ''):
                out.add(net.name)
        return out

    # ---- showing things in KiCad ----
    def show_track(self, layer, net, a, b):
        """Select (in KiCad) the track of `net` on `layer` that the segment a - b (KiCad mm) lies on; True when
        one was found."""
        from kipy.util.board_layer import canonical_name
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        best = None
        for t in self.board.get_tracks():
            if canonical_name(t.layer) != layer or (net and t.net.name != net):
                continue
            p = _seg_dist(mid, (t.start.x * NM, t.start.y * NM), (t.end.x * NM, t.end.y * NM))
            if p <= t.width * NM / 2 + 1e-3 and (best is None or p < best[0]):
                best = (p, t)
        if best is None:
            return False
        self.board.clear_selection()
        self.board.add_to_selection([best[1]])
        return True


def _seg_dist(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    s = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return ((p[0] - ax - s * dx) ** 2 + (p[1] - ay - s * dy) ** 2) ** 0.5
