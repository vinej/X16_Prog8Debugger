#!/usr/bin/env python3
"""M0: prog8 source map generator -- p8 line <-> machine address.

prog8c embeds `; source: <file>.p8:NN <text>` comments in its generated
64tass assembly (default behavior), and the 64tass listing gives asm line
-> address. This tool joins the two into a JSON table:

    { "addr": 0x0836, "file": "examples\\bounce.p8", "line": 68,
      "asm_line": 100, "text": "txt.print(...)" }

The join is exact, not fuzzy: 64tass `--line-numbers` prefixes every
listing row with the asm line number. The listing prog8c itself writes
(via -asmlist) does NOT carry line numbers, so when the given listing
lacks them this tool re-assembles the .asm with `--line-numbers` into a
temp dir, reusing the exact 64tass flags recorded in the listing header.
The regenerated PRG is byte-compared against the original as proof that
the addresses match the program actually running.

Usage:
    python p8map.py build\bounce.asm [--list build\bounce.list]
                    [--out build\bounce.p8map.json] [--tass 64tass.exe]
                    [--dump]

Also importable: SourceMap.load(json_path), .addr_to_entry(pc),
.line_to_entry(file, line), .next_mapped_line(file, line).
"""

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SOURCE_RE = re.compile(r"\s*; source: (\S+):(\d+)\s*(.*)$")
# numbered listing code/data row: "<asmline>\t<.|>hhhh>\t..."
NUMBERED_ROW_RE = re.compile(r"^(\d+)\t([.>])([0-9a-fA-F]{4})\t")
# variable declarations in prog8-generated asm
VAR_ZP_RE = re.compile(r"^(p8v_\w+)\s*=\s*(\$[0-9a-fA-F]+|\d+)\s*;\s*zp\s+(\w+)")
VAR_MEM_RE = re.compile(r"^(p8v_\w+)\s+\.(byte|word|fill)\s+(.*)$")
SCOPE_OPEN_RE = re.compile(r"^(\w+)\s+\.(proc|block)\b")
SCOPE_CLOSE_RE = re.compile(r"^\s*\.(pend|bend)\b")

TYPE_SIZES = {"ubyte": 1, "byte": 1, "bool": 1, "uword": 2, "word": 2,
              "float": 5}

DEFAULT_TASS_FLAGS = ["--cbm-prg", "--ascii", "--case-sensitive",
                      "--long-branch", "--no-monitor"]


def parse_asm(asm_path):
    """-> [(asm_line, file, p8_line, text), ...] from `; source:` comments."""
    refs = []
    with open(asm_path, "r", encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            m = SOURCE_RE.match(line)
            if m:
                refs.append((n, m.group(1), int(m.group(2)), m.group(3).rstrip()))
    return refs


def listing_is_numbered(list_path):
    with open(list_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(";Line"):
                return True
            if line.startswith(";Offset"):
                return False
    return False


def listing_recorded_flags(list_path):
    """Extract the 64tass flags recorded in the listing header, minus any
    output/list/labels options (we substitute our own)."""
    cmdline = None
    with open(list_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("; ") and "64tass" in line and "--" in line:
                cmdline = line[2:].strip()
                break
            if line.startswith(";Line") or line.startswith(";Offset"):
                break
    if not cmdline:
        return None
    tokens = cmdline.split()
    flags, skip_next = [], False
    consume_value = {"--output", "-o", "--list", "-L", "--labels", "-l"}
    drop_prefix = ("--output=", "--list=", "--labels=", "--list-append=",
                   "--labels-append=")
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in consume_value:
            skip_next = True
            continue
        if tok.startswith(drop_prefix) or tok in ("--dump-labels", "--vice-labels"):
            continue
        if tok.startswith("-"):
            flags.append(tok)
        # non-flag tokens (the input file) are dropped
    return flags or None


def find_tass(explicit, asm_path):
    if explicit:
        return explicit
    found = shutil.which("64tass")
    if found:
        return found
    # conventional layout: <project>\prog8\build\x.asm with <project>\prog8-sdk\
    d = os.path.dirname(os.path.abspath(asm_path))
    for _ in range(4):
        cand = os.path.join(d, "prog8-sdk", "64tass.exe")
        if os.path.isfile(cand):
            return cand
        d = os.path.dirname(d)
    return None


def parse_numbered_listing(list_path):
    """-> {asm_line: first_address} for rows that occupy memory (. or >)."""
    addr_by_line = {}
    with open(list_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = NUMBERED_ROW_RE.match(line)
            if m:
                n = int(m.group(1))
                if n not in addr_by_line:
                    addr_by_line[n] = int(m.group(3), 16)
    return addr_by_line


def reassemble_numbered(tass, asm_path, flags, workdir):
    """Run 64tass with --line-numbers (+ VICE labels for variable
    addresses); return (listing_path, prg_path, labels_path)."""
    lst = os.path.join(workdir, "p8map.list")
    prg = os.path.join(workdir, "p8map.prg")
    lbl = os.path.join(workdir, "p8map.lbl")
    cmd = [tass] + flags + ["--line-numbers", "--list", lst,
                            "--vice-labels", "--labels", lbl,
                            "--output", prg, os.path.abspath(asm_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(lst):
        sys.exit(f"64tass failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return lst, prg, lbl


def build_entries(refs, addr_by_line):
    """Join source refs with listing addresses.

    A ref at asm line L maps to the first memory-occupying listing row
    before the ref's window closes. For a library ref the window ends at
    the next ref of any kind; for a program (user-file) ref it extends
    PAST library refs to the next program ref -- prog8 inlines library
    subs (sys.waitvsync -> wai, txt.clear_screen -> chrout, GETIN2 ...)
    and emits only the library's `; source:` next to the code, so the
    inlined code must also be claimed by the user statement it implements
    or that statement would be unmappable (invisible to breakpoints and
    stepping). Refs with no code in their window (const/var declaration
    comments) produce no entry."""
    code_lines = sorted(addr_by_line)
    n = len(refs)
    next_user = [float("inf")] * n     # next non-library ref's asm line
    nu = float("inf")
    for i in range(n - 1, -1, -1):
        next_user[i] = nu
        if not refs[i][1].startswith("library:"):
            nu = refs[i][0]
    entries = []
    for i, (asm_line, fname, p8_line, text) in enumerate(refs):
        if fname.startswith("library:"):
            limit = refs[i + 1][0] if i + 1 < n else float("inf")
        else:
            limit = next_user[i]
        j = bisect.bisect_right(code_lines, asm_line)
        if j < len(code_lines) and code_lines[j] < limit:
            entries.append({
                "addr": addr_by_line[code_lines[j]],
                "file": fname,
                "line": p8_line,
                "asm_line": asm_line,
                "text": text,
            })
    # Entries may share an address (a `sub x() {` header and the sub's
    # first statement): keep both so breakpoints work on either line;
    # addr_to_entry resolves to the innermost (last) one.
    return sorted(entries, key=lambda e: (e["addr"], e["asm_line"]))


def parse_variables(asm_path):
    """Collect prog8 variables (p8v_*) with their scope, type and layout.

    ZP variables are equates with a `; zp <type>` comment; memory
    variables are labeled .byte/.word/.fill declarations whose addresses
    come from the VICE label file (resolve_variable_addresses). Scope is
    the .proc/.block nesting, e.g. p8b_main.p8s_move_axis_x."""
    variables = []
    stack = []
    with open(asm_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SCOPE_OPEN_RE.match(line)
            if m:
                stack.append(m.group(1))
                continue
            if SCOPE_CLOSE_RE.match(line):
                if stack:
                    stack.pop()
                continue
            m = VAR_ZP_RE.match(line)
            if m:
                name, addr, vtype = m.groups()
                addr = int(addr[1:], 16) if addr.startswith("$") else int(addr)
                variables.append({"name": name, "scope": ".".join(stack),
                                  "addr": addr, "type": vtype, "count": 1})
                continue
            m = VAR_MEM_RE.match(line)
            if m:
                name, directive, rest = m.groups()
                rest = rest.split(";")[0].strip()
                if directive == "fill":
                    vtype, count = "ubyte", int(rest.split()[0])
                else:
                    vtype = "ubyte" if directive == "byte" else "uword"
                    count = rest.count(",") + 1 if rest else 1
                variables.append({"name": name, "scope": ".".join(stack),
                                  "addr": None, "type": vtype, "count": count})
    return variables


def parse_vice_labels(path):
    """-> {'scope:sub:name': addr} from a VICE label file (`al hhhh .x:y`)."""
    labels = {}
    with open(path, "r", encoding="ascii", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "al":
                labels[parts[2].lstrip(".")] = int(parts[1], 16)
    return labels


def resolve_variable_addresses(variables, labels):
    """Fill memory variables' addresses from the label table; drop the
    ones that cannot be resolved."""
    resolved = []
    for v in variables:
        if v["addr"] is None:
            key = ":".join(v["scope"].split(".") + [v["name"]])
            v["addr"] = labels.get(key)
        if v["addr"] is not None:
            resolved.append(v)
    return resolved


def interpolate_inlined(entries, source_root):
    """Recover user lines that prog8 inlined without a trace.

    Statements that are pure library calls (sys.waitvsync() -> wai,
    txt.clear_screen() -> chrout, txt.nl() ...) get NO `; source:` ref of
    their own -- only the library's. Where library entries sit between two
    mapped user lines X..Y and exactly ONE code-like source line lies in
    (X, Y), that line must be what the inlined code implements: synthesize
    an entry for it so breakpoints and stepping see it. Multi-candidate
    gaps (wrapped argument lists, declaration clusters) are left alone."""
    def code_like(text):
        t = text.strip()
        return t and not t.startswith(";") and t not in ("{", "}", "}}")

    sources = {}

    def source_lines(fname):
        if fname not in sources:
            path = fname if os.path.isabs(fname) else os.path.join(source_root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    sources[fname] = f.read().splitlines()
            except OSError:
                sources[fname] = None
        return sources[fname]

    by_addr = sorted(entries, key=lambda e: (e["addr"], e["asm_line"]))
    user = [e for e in by_addr if not e["file"].startswith("library:")]
    mapped_lines = {(e["file"], e["line"]) for e in user}
    synthesized = []
    for a, b in zip(user, user[1:]):
        if a["file"] != b["file"] or b["line"] <= a["line"] + 1:
            continue
        libs = [e for e in by_addr
                if e["file"].startswith("library:")
                and a["addr"] <= e["addr"] < b["addr"]
                and e["asm_line"] > a["asm_line"]]
        if not libs:
            continue
        lines = source_lines(a["file"])
        if lines is None:
            continue
        candidates = [n for n in range(a["line"] + 1, b["line"])
                      if n <= len(lines) and code_like(lines[n - 1])
                      and (a["file"], n) not in mapped_lines]
        if len(candidates) != 1:
            continue
        synthesized.append({
            "addr": libs[0]["addr"],
            "file": a["file"],
            "line": candidates[0],
            "asm_line": libs[0]["asm_line"],
            "text": lines[candidates[0] - 1].strip(),
            "inlined": True,
        })
    return sorted(entries + synthesized,
                  key=lambda e: (e["addr"], e["asm_line"]))


class SourceMap:
    """Lookup helper over the generated entries (also used by tools/DAP)."""

    def __init__(self, entries, code_end=None, variables=None):
        # code_end bounds addr_to_entry so PCs outside the program
        # (KERNAL ROM, BASIC) don't map to the nearest lower statement
        self.code_end = code_end
        self.variables = variables or []
        self.entries = sorted(entries, key=lambda e: (e["addr"], e["asm_line"]))
        # several entries can share an address (sub header + first statement,
        # loop head + inlined library code): for PC display prefer the last
        # non-library entry at that address, else the last one.
        self._by_addr = {}
        for e in self.entries:
            cur = self._by_addr.get(e["addr"])
            if cur is None or not e["file"].startswith("library:") \
                    or cur["file"].startswith("library:"):
                self._by_addr[e["addr"]] = e
        self._addrs = sorted(self._by_addr)

    @classmethod
    def load(cls, json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
            return cls(d["entries"], d.get("code_end"), d.get("variables"))

    def addr_to_entry(self, pc):
        """Greatest entry with addr <= pc (a statement spans until the next
        mapped statement). None if pc is outside the program's code."""
        if self.code_end is not None and pc > self.code_end:
            return None
        i = bisect.bisect_right(self._addrs, pc) - 1
        return self._by_addr[self._addrs[i]] if i >= 0 else None

    def _file_matches(self, entry_file, file_suffix):
        return entry_file.replace("/", "\\").lower().endswith(
            file_suffix.replace("/", "\\").lower())

    def line_to_entry(self, file_suffix, line):
        for e in self.entries:
            if e["line"] == line and self._file_matches(e["file"], file_suffix):
                return e
        return None

    def next_mapped_line(self, file_suffix, line):
        """Smallest mapped line >= line in that file (breakpoint adjust)."""
        best = None
        for e in self.entries:
            if e["line"] >= line and self._file_matches(e["file"], file_suffix):
                if best is None or e["line"] < best["line"]:
                    best = e
        return best


class MapError(Exception):
    pass


def generate(asm, listing=None, out=None, tass=None, source_root=None):
    """Build the map for a prog8 build and write the JSON.

    -> (SourceMap, out_path, summary string). Raises MapError on failure.
    Used by the CLI below and by the DAP adapter at launch time.
    source_root: directory the source paths in the asm are relative to
    (the prog8c working directory); default: the asm's parent's parent,
    matching the conventional <root>\\build\\<name>.asm layout."""
    base = os.path.splitext(asm)[0]
    listing = listing or (base + ".list")
    out = out or (base + ".p8map.json")
    if source_root is None:
        source_root = os.path.dirname(os.path.dirname(os.path.abspath(asm)))

    refs = parse_asm(asm)
    if not refs:
        raise MapError("no '; source:' comments found -- was the program "
                       "compiled with -nosourcelines?")

    variables = parse_variables(asm)
    labels = {}
    if os.path.isfile(base + ".vice-mon-list"):
        labels = parse_vice_labels(base + ".vice-mon-list")

    prg_note = ""
    if os.path.isfile(listing) and listing_is_numbered(listing):
        addr_by_line = parse_numbered_listing(listing)
    else:
        tass_exe = find_tass(tass, asm)
        if not tass_exe:
            raise MapError("listing has no line numbers and 64tass was not "
                           "found; pass --tass or put 64tass on PATH")
        flags = None
        if os.path.isfile(listing):
            flags = listing_recorded_flags(listing)
        flags = flags or DEFAULT_TASS_FLAGS
        with tempfile.TemporaryDirectory() as workdir:
            lst, prg, lbl = reassemble_numbered(tass_exe, asm, flags, workdir)
            addr_by_line = parse_numbered_listing(lst)
            if os.path.isfile(lbl):
                labels = parse_vice_labels(lbl)
            ref_prg = base + ".prg"
            if os.path.isfile(ref_prg):
                with open(prg, "rb") as f1, open(ref_prg, "rb") as f2:
                    same = f1.read() == f2.read()
                prg_note = (f"; reassembled PRG {'==' if same else '!='} "
                            f"{os.path.basename(ref_prg)}")
                if not same:
                    raise MapError(
                        f"reassembly of {asm} does not reproduce {ref_prg} "
                        "-- stale build artifacts? Rebuild and retry.")

    entries = build_entries(refs, addr_by_line)
    entries = interpolate_inlined(entries, source_root)
    variables = resolve_variable_addresses(variables, labels)
    # +3 = generous size of the last emitting row's instruction/data
    code_end = max(addr_by_line.values()) + 3
    library = sum(1 for e in entries if e["file"].startswith("library:"))

    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "asm": asm, "code_end": code_end,
                   "entries": entries, "variables": variables}, f, indent=1)

    summary = (f"{len(refs)} source refs -> {len(entries)} mapped statements "
               f"({library} in library files), {len(variables)} variables "
               f"{prg_note}")
    return SourceMap(entries, code_end, variables), out, summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("asm", help="prog8-generated .asm (with ; source: comments)")
    ap.add_argument("--list", dest="listing",
                    help="64tass listing (default: <asm>.list next to the asm)")
    ap.add_argument("--out", help="output JSON (default: <asm>.p8map.json)")
    ap.add_argument("--tass", help="64tass executable (default: PATH, then "
                    "a prog8-sdk\\64tass.exe found above the asm)")
    ap.add_argument("--source-root", help="directory the asm's source paths "
                    "are relative to (default: the asm's parent's parent)")
    ap.add_argument("--dump", action="store_true", help="print the full table")
    args = ap.parse_args()

    try:
        smap, out, summary = generate(args.asm, args.listing, args.out,
                                      args.tass, args.source_root)
    except MapError as e:
        sys.exit(str(e))

    program_files = sorted({e["file"] for e in smap.entries
                            if not e["file"].startswith("library:")})
    print(summary)
    print(f"files: {', '.join(program_files)}")
    print(f"wrote {out}")

    if args.dump:
        for e in smap.entries:
            print(f"${e['addr']:04x}  {e['file']}:{e['line']:<4} {e['text']}")


if __name__ == "__main__":
    main()
