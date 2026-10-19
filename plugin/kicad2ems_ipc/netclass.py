"""
Net classes and the impedance each one asks for (no Qt here).

    nc = NetClasses.from_project('board.kicad_pro', nets)   # the classes of the project file (KiCad 7 and later)
    nc = NetClasses.from_map({net: ['50R_SE'], ...}, nets)  # the classes KiCad reports through its API
    nc.of(net)            # 'Default', '100ohm', ...: the net's class (the first of several, KiCad 9+)
    nc.target(net)        # (100.0, 'diff') / (50.0, 'se') / None
    nc.disabled(net)      # True for anything #PWR: never mapped

The target comes from the class name: a number with ohm / Ohm / R / Ω after it, or Z before it ('50R_SE',
'100ohm_diff', 'Z90 DIFF', '50 Ohm'). SE / single in the name make it single-ended, diff / dp / differential
differential. A name that says neither ('100ohm', as KiCad's CM5 demo names its classes) is differential when the
class holds a pair (a net whose partner, by KiCad's rule: the name ending in + / - or P / N, is on the board), and
single-ended otherwise; in a differential class the nets that are not a pair's legs (a class pattern caught them
too) get no target. Nets or classes with #pwr in their names (KiCad's power symbols) are disabled.
"""
import fnmatch, json, re

DEFAULT = 'Default'
_OHM = re.compile(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:ohms?|r(?![a-z])|Ω)', re.I)
_Z = re.compile(r'\bz0?[\s_=-]*(\d+(?:\.\d+)?)', re.I)
_DIFF = re.compile(r'(diff|differential|(?<![a-z])dp(?![a-z]))', re.I)
_SE = re.compile(r'((?<![a-z])se(?![a-z])|single)', re.I)
_PWR = re.compile(r'#pwr', re.I)


def partner(net):
    """The other leg of a differential pair, by KiCad's rule (the name ends in + / - or P / N), or None."""
    if not net:
        return None
    for a, b in (('+', '-'), ('-', '+'), ('P', 'N'), ('N', 'P'), ('p', 'n'), ('n', 'p')):
        if net.endswith(a):
            return net[:-1] + b
    return None


def parse_target(name):
    """(ohm, 'se' | 'diff' | None) of a class name, or None when it names no impedance."""
    m = _OHM.search(name or '') or _Z.search(name or '')
    if not m:
        return None
    kind = 'diff' if _DIFF.search(name) else ('se' if _SE.search(name) else None)
    return float(m.group(1)), kind


def label(t):
    return 'no impedance target' if not t else f"{t[0]:g} ohm {'diff' if t[1] == 'diff' else 'SE'}"


class NetClasses:
    def __init__(self, classes, nets):
        """classes: {net: [class names]} (nets missing: Default); nets: every net of the board."""
        self.nets = set(nets)
        self.classes = {n: list(classes.get(n) or [DEFAULT]) for n in self.nets}
        self._paired = {c for n, cs in self.classes.items() if partner(n) in self.nets for c in cs}

    @classmethod
    def from_map(cls, classes, nets):
        return cls(classes, nets)

    @classmethod
    def from_project(cls, path, nets):
        """The classes of a .kicad_pro: explicit assignments first, then the patterns (wildcards), then Default."""
        try:
            with open(path, encoding='utf-8') as fh:
                ns = json.load(fh).get('net_settings', {})
        except (OSError, ValueError):
            return cls({}, nets)
        assigned = ns.get('netclass_assignments') or {}
        patterns = [(p.get('pattern', ''), p.get('netclass', DEFAULT)) for p in ns.get('netclass_patterns') or []]
        out = {}
        for n in nets:
            a = assigned.get(n)
            if a:
                out[n] = [a] if isinstance(a, str) else list(a)
                continue
            hit = [c for pat, c in patterns if pat and fnmatch.fnmatchcase(n, pat)]
            if hit:
                out[n] = list(dict.fromkeys(hit))
        return cls(out, nets)

    def of(self, net):
        """The net's class: the first that is not Default, else Default."""
        names = self.classes.get(net, [DEFAULT])
        return next((c for c in names if c != DEFAULT), names[0] if names else DEFAULT)

    def disabled(self, net):
        return bool(_PWR.search(net or '')) or any(_PWR.search(c) for c in self.classes.get(net, []))

    def target(self, net):
        """(ohm, 'se' | 'diff') the net's class asks for, or None."""
        for c in self.classes.get(net, []):
            t = parse_target(c)
            if t:
                kind = t[1] or ('diff' if c in self._paired else 'se')
                if kind == 'diff' and partner(net) not in self.nets:
                    return None                                     # a single net in a pair class
                return t[0], kind
        return None

    def summary(self, nets):
        """[(class, its nets among `nets`, target label or '')] sorted by name, Default last."""
        by = {}
        for n in nets:
            by.setdefault(self.of(n), []).append(n)
        rows = []
        for c, ns in by.items():
            ts = {self.target(n) for n in ns} - {None}
            rows.append((c, sorted(ns), ', '.join(sorted(label(t) for t in ts))))
        return sorted(rows, key=lambda r: (r[0] == DEFAULT, r[0].lower()))
