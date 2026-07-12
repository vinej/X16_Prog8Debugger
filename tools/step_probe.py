#!/usr/bin/env python3
"""M1: proof of source-level stepping over the binary monitor.

Using the M0 source map (p8map.py), this probe:
  1. sets an exec checkpoint on the address of a given .p8 line,
  2. waits for the running program to hit it,
  3. maps the stop PC back to the .p8 line (must round-trip),
  4. steps (step-over, so jsr = one step) until the mapped line changes,
  5. removes the checkpoint and resumes.

Attach mode (default) expects Box16 already running:
  box16.exe -ignore_ini -binarymonitor -rom rom.bin -prg bounce.prg -run

Or let the probe run the whole thing: --launch [--box16 EXE --rom ROM].

Typical use against the shared bounce demo (line 84 = move_axis_x(),
executed every frame):
  python tools/step_probe.py --map ...\bounce.p8map.json --line 84 --launch
"""

import argparse
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binmon import Monitor
from p8map import SourceMap

REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
DEF_MAP = os.path.join(REPO, "build", "bounce.p8map.json")
DEF_PRG = os.path.join(REPO, "build", "bounce.prg")
DEF_BOX16 = os.path.join(REPO, "emulator", "box16.exe")
DEF_ROM = os.path.join(REPO, "emulator", "rom.bin")


def fmt(smap, pc):
    e = smap.addr_to_entry(pc)
    if e is None:
        return f"${pc:04x} -> (unmapped)"
    return f"${pc:04x} -> {e['file']}:{e['line']}  {e['text']}"


def wait_for_port(host, port, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"monitor port {host}:{port} never opened")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEF_MAP, help="p8map.json from p8map.py")
    ap.add_argument("--file", default="bounce.p8", help=".p8 file (suffix match)")
    ap.add_argument("--line", type=int, default=84, help=".p8 line to break on")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6502)
    ap.add_argument("--launch", action="store_true",
                    help="launch Box16 with the PRG instead of attaching")
    ap.add_argument("--box16", default=DEF_BOX16)
    ap.add_argument("--rom", default=DEF_ROM)
    ap.add_argument("--prg", default=DEF_PRG)
    ap.add_argument("--keep", action="store_true",
                    help="with --launch: leave Box16 running afterwards")
    ap.add_argument("--max-steps", type=int, default=200)
    args = ap.parse_args()

    smap = SourceMap.load(args.map)
    entry = smap.line_to_entry(args.file, args.line)
    if entry is None:
        adjusted = smap.next_mapped_line(args.file, args.line)
        if adjusted is None:
            sys.exit(f"{args.file}:{args.line} has no mapped statement")
        print(f"{args.file}:{args.line} is not a statement; "
              f"adjusted to line {adjusted['line']} (as a DAP adapter would)")
        entry = adjusted
    addr = entry["addr"]
    print(f"target {entry['file']}:{entry['line']} -> ${addr:04x}   "
          f"[{entry['text']}]")

    box16 = None
    if args.launch:
        cmd = [args.box16, "-ignore_ini", "-binarymonitor",
               "-rom", args.rom, "-prg", args.prg, "-run", "-scale", "1"]
        print(f"launching {os.path.basename(args.box16)} ...")
        box16 = subprocess.Popen(cmd, cwd=os.path.dirname(args.box16),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        wait_for_port(args.host, args.port)

    mon = Monitor(args.host, args.port)
    ok = False
    try:
        mon.ping()
        cp = mon.checkpoint_set(addr)   # exec, low RAM, no bank needed
        print(f"checkpoint {cp} set, waiting for hit ...")
        pc = mon.wait_stopped(timeout=60)
        print(f"HIT  {fmt(smap, pc)}")
        hit = smap.addr_to_entry(pc)
        if pc != addr or hit is None or hit["addr"] != addr:
            raise AssertionError("stop PC did not round-trip to the target line")
        if hit["line"] != entry["line"]:
            print(f"  (address shared with {hit['file']}:{hit['line']}; "
                  "stepping until the line leaves that one)")
            entry = hit

        print("stepping (step-over) until the line changes:")
        steps = 0
        while steps < args.max_steps:
            pc = mon.advance(step_over=True)
            steps += 1
            cur = smap.addr_to_entry(pc)
            print(f"  step {steps}: {fmt(smap, pc)}")
            if cur is not None and (cur["line"] != entry["line"]
                                    or cur["file"] != entry["file"]):
                print(f"LINE CHANGED after {steps} step(s): "
                      f"{entry['line']} -> {cur['line']}")
                ok = True
                break
        else:
            raise AssertionError(f"line never changed in {args.max_steps} steps")

        mon.checkpoint_delete(cp)
        mon.resume()
        print("checkpoint removed, program resumed")
    finally:
        mon.close()
        if box16 is not None and not args.keep:
            box16.terminate()
            try:
                box16.wait(timeout=5)
            except subprocess.TimeoutExpired:
                box16.kill()
            print("box16 terminated")

    print("M1 PASS" if ok else "M1 FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
