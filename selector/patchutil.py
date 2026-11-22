"""Unified diff parsing and application."""

from __future__ import annotations

import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old_path", "new_path", "hunks", "binary")

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)
        self.binary = False

    @property
    def path(self):
        return self.new_path if self.new_path is not None else self.old_path

    def changed_old_lines(self):
        """Old-file line numbers removed/modified, and insertion points (old line after which
        new lines were inserted)."""
        removed, inserted_after = set(), set()
        for old_start, _ol, _ns, _nl, lines in self.hunks:
            o = old_start
            for ln in lines:
                tag = ln[:1]
                if tag == " ":
                    o += 1
                elif tag == "-":
                    removed.add(o)
                    o += 1
                elif tag == "+":
                    inserted_after.add(o - 1)
        return removed, inserted_after


def touched_lines(fp):
    """(old_touched, new_touched): line numbers changed or adjacent to an edit, per side."""
    old_t, new_t = set(), set()
    for old_start, _ol, new_start, _nl, lines in fp.hunks:
        o, n = old_start, new_start
        for ln in lines:
            tag = ln[:1]
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                old_t.add(o)
                new_t.add(n)
                o += 1
            elif tag == "+":
                new_t.add(n)
                old_t.add(o)
                n += 1
    return old_t, new_t


def _strip_prefix(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            patches.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                patches.append(cur)
            cur.old_path = _strip_prefix(line[4:])
            cur.new_path = _strip_prefix(lines[i + 1][4:])
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.new_path = None
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to "):]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
        m = HUNK_RE.match(line)
        if m and cur is not None:
            os_, ol, ns, nl = m.groups()
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < n and (ro > 0 or rn > 0):
                ln = lines[i]
                if ln.startswith("\\"):
                    i += 1
                    continue
                tag = ln[:1]
                if tag == " " or ln == "":
                    body.append(" " + ln[1:])
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    body.append(ln)
                    ro -= 1
                elif tag == "+":
                    body.append(ln)
                    rn -= 1
                else:
                    break
                i += 1
            # trailing "\ No newline at end of file" markers
            while i < n and lines[i].startswith("\\"):
                body.append("\\")
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return [p for p in patches if p.hunks or p.binary or p.old_path != p.new_path or True]


def apply(old: str | None, fp: FilePatch) -> str | None:
    """Apply a file patch to old text. Returns new text (None if deleted)."""
    if fp.new_path is None:
        return None
    old_lines = [] if old is None else old.split("\n")
    # keep track of whether old ended with newline: split gives trailing ''
    out = []
    pos = 0  # index into old_lines (0-based)
    no_newline_new = False
    for old_start, ol, _ns, _nl, body in fp.hunks:
        start = old_start - 1 if ol > 0 else old_start
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        prev = None
        for ln in body:
            tag = ln[:1]
            if tag == "\\":
                if prev == "+" or prev == " ":
                    no_newline_new = True
                continue
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else ln[1:])
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(ln[1:])
            prev = tag
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    if old is None:
        text = text + ("" if no_newline_new else "\n")
    elif no_newline_new and text.endswith("\n"):
        text = text[:-1]
    return text
