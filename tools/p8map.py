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
    """Run 64tass with --line-numbers; return (listing_path, prg_path)."""
    lst = os.path.join(workdir, "p8map.list")
    prg = os.path.join(workdir, "p8map.prg")
    cmd = [tass] + flags + ["--line-numbers", "--list", lst,
                            "--output", prg, os.path.abspath(asm_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(lst):
        sys.exit(f"64tass failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return lst, prg


def build_entries(refs, addr_by_line):
    """Join source refs with listing addresses.

    A ref at asm line L maps to the first memory-occupying listing row with
    asm line in (L, next_ref_L). Refs with no code in their window (const/
    var declaration comments) produce no entry."""
    code_lines = sorted(addr_by_line)
    entries = []
    for i, (asm_line, fname, p8_line, text) in enumerate(refs):
        limit = refs[i + 1][0] if i + 1 < len(refs) else float("inf")
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


class SourceMap:
    """Lookup helper over the generated entries (also used by tools/DAP)."""

    def __init__(self, entries):
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
            return cls(json.load(f)["entries"])

    def addr_to_entry(self, pc):
        """Greatest entry with addr <= pc (a statement spans until the next
        mapped statement). None if pc is before the first entry."""
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("asm", help="prog8-generated .asm (with ; source: comments)")
    ap.add_argument("--list", dest="listing",
                    help="64tass listing (default: <asm>.list next to the asm)")
    ap.add_argument("--out", help="output JSON (default: <asm>.p8map.json)")
    ap.add_argument("--tass", help="64tass executable (default: PATH, then "
                    "a prog8-sdk\\64tass.exe found above the asm)")
    ap.add_argument("--dump", action="store_true", help="print the full table")
    args = ap.parse_args()

    base = os.path.splitext(args.asm)[0]
    listing = args.listing or (base + ".list")
    out = args.out or (base + ".p8map.json")

    refs = parse_asm(args.asm)
    if not refs:
        sys.exit("no '; source:' comments found -- was the program compiled "
                 "with -nosourcelines?")

    prg_note = ""
    if os.path.isfile(listing) and listing_is_numbered(listing):
        addr_by_line = parse_numbered_listing(listing)
    else:
        tass = find_tass(args.tass, args.asm)
        if not tass:
            sys.exit("listing has no line numbers and 64tass was not found; "
                     "pass --tass or put 64tass on PATH")
        flags = None
        if os.path.isfile(listing):
            flags = listing_recorded_flags(listing)
        flags = flags or DEFAULT_TASS_FLAGS
        with tempfile.TemporaryDirectory() as workdir:
            lst, prg = reassemble_numbered(tass, args.asm, flags, workdir)
            addr_by_line = parse_numbered_listing(lst)
            ref_prg = base + ".prg"
            if os.path.isfile(ref_prg):
                with open(prg, "rb") as f1, open(ref_prg, "rb") as f2:
                    same = f1.read() == f2.read()
                prg_note = (f"; reassembled PRG {'==' if same else '!='} "
                            f"{os.path.basename(ref_prg)}")
                if not same:
                    sys.exit(f"FATAL: reassembly of {args.asm} does not "
                             f"reproduce {ref_prg} -- stale build artifacts? "
                             "Rebuild and retry.")

    entries = build_entries(refs, addr_by_line)
    program_files = sorted({e["file"] for e in entries
                            if not e["file"].startswith("library:")})
    library = sum(1 for e in entries if e["file"].startswith("library:"))

    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "asm": args.asm, "entries": entries}, f, indent=1)

    print(f"{len(refs)} source refs -> {len(entries)} mapped statements "
          f"({library} in library files) {prg_note}")
    print(f"files: {', '.join(program_files)}")
    print(f"wrote {out}")

    if args.dump:
        for e in entries:
            print(f"${e['addr']:04x}  {e['file']}:{e['line']:<4} {e['text']}")


if __name__ == "__main__":
    main()
