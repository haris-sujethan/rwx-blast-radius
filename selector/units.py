"""Fixture-case ("unit") filtering: run only the fixture cases a change can reach."""

from __future__ import annotations

import pickle
import zlib

FULL = "full"
SKIP = "skip"


def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def pair_cases(text):
    statements = _filter_comments(text).split(";")
    size = len(statements)
    out = []
    for i in range(0, size, 2):
        if i + 1 < size:
            out.append((statements[i].strip(), statements[i + 1].strip()))
    return out


def line_cases(text):
    out = []
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines = []
    return out


def changed_cases(old, new, n_units):
    """Indices of fixture cases whose text changed, for the scheme with n_units cases.
    None if the case structure changed (or the scheme is unknown)."""
    if old is None or new is None:
        return None
    for fn in (pair_cases, line_cases):
        a = fn(old)
        if len(a) != n_units:
            continue
        b = fn(new)
        if len(a) != len(b):
            return None
        return {i for i, (x, y) in enumerate(zip(a, b)) if x != y}
    return None


class UnitDB:
    def __init__(self, path):
        m = pickle.loads(zlib.decompress(open(path, "rb").read()))
        self.sets = m["sets"]
        self.tests = m["tests"]
        self.implines = m["implines"]

    def frac_for(self, test_id, rel, idxs):
        """Share of a test's CPU spent on the given fixture rows (None if unknown)."""
        u = self.tests.get(test_id)
        if u is None or not u.get("w"):
            return None
        f = u.get("w0", 0.0)
        for pos, (r, i) in enumerate(u["units"]):
            if r == rel and i in idxs:
                f += u["w"][pos]
        return min(1.0, f)

    def plan(self, test_id, test_bit, req, fixture_texts):
        """Returns (FULL | SKIP | {fixture_rel: set(idx)}, fraction_of_units)."""
        u = self.tests.get(test_id)
        if u is None or req["full"] & test_bit:
            return FULL, 1.0
        lines_req = req["lines"]
        for rel, lines in lines_req.items():
            nl = u["nl"].get(rel)
            if nl and not nl.isdisjoint(lines):
                return FULL, 1.0
            im = self.implines.get(rel)
            if im and not im.isdisjoint(lines):
                return FULL, 1.0
        if not u["nc"].isdisjoint(req["classes"]):
            return FULL, 1.0
        for cls, clines in req.get("conj", ()):
            if cls in u["nc"]:
                for rel, lines in clines.items():
                    nl = u["nl"].get(rel)
                    if nl and not nl.isdisjoint(lines):
                        return FULL, 1.0
        S = self.sets
        bits = 0
        explained = False
        for rel, lines in lines_req.items():
            d = u["lines"].get(rel)
            if not d:
                continue
            for ln in lines:
                i = d.get(ln)
                if i is not None:
                    bits |= S[i]
        for c in req["classes"]:
            i = u["classes"].get(c)
            if i is not None:
                bits |= S[i]
        for cls, clines in req.get("conj", ()):
            ci = u["classes"].get(cls)
            in_nonunit = cls in u["nc"]
            if ci is None and not in_nonunit:
                continue
            lb = 0
            for rel, lines in clines.items():
                d = u["lines"].get(rel)
                if not d:
                    continue
                for ln in lines:
                    i = d.get(ln)
                    if i is not None:
                        lb |= S[i]
            # an instance made outside the units may be used inside any of them
            bits |= lb if in_nonunit or ci is None else lb & S[ci]
            explained = True
        if bits:
            explained = True
        units = u["units"]
        counts = u.get("counts", {})
        for rel in req["fixtures"]:
            idxs = [k for k, (r, _) in enumerate(units) if r == rel]
            total = counts.get(rel)
            if total is None:
                continue  # the test never iterates this fixture's rows
            texts = fixture_texts.get(rel)
            if texts is None:
                return FULL, 1.0
            changed = changed_cases(texts[0], texts[1], total)
            if changed is None:
                return FULL, 1.0
            explained = True  # rows the test loads but never works on cannot affect it
            for pos in idxs:
                if units[pos][1] in changed:
                    bits |= 1 << pos
        if not explained:
            return FULL, 1.0
        if not bits:
            return SKIP, 0.0
        filt = {}
        w = u.get("w")
        frac = u.get("w0", 0.0)
        while bits:
            low = bits & -bits
            pos = low.bit_length() - 1
            bits ^= low
            rel, idx = units[pos]
            filt.setdefault(rel, set()).add(idx)
            frac += w[pos] if w else 1.0 / max(1, len(units))
        return filt, min(1.0, frac)
