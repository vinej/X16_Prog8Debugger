#!/usr/bin/env python3
"""M2: DAP debug adapter for Prog8 on the Commander X16 (Box16 fork).

Speaks the Debug Adapter Protocol on stdio (launched by the VSCode
extension in this repo) and the VICE binary monitor to the Box16 fork.
Reuses the proven pieces: p8map.py (M0 source map) and the monitor
framing from binmon.py (M1).

Launch config (see package.json for the schema):
    program     .p8 source file (the map is keyed to it)
    cwd         directory prog8c was run from (source paths in the asm
                are relative to it); default: the program's parent dir
                or its parent if the program sits in examples/
    buildDir    default <cwd>/build
    asm/prg     default <buildDir>/<stem>.asm / .prg
    box16/rom   default: the shared x16_CDebugger fork build + rom
    stopOnEntry break on the program's first mapped statement
    port/host   monitor endpoint (default 127.0.0.1:6502)
    scale       Box16 window scale (default 1)

Design notes: one reader thread per input (DAP stdin, monitor socket)
feeds a single work queue; the main loop is the only thing that talks
back to VSCode or issues monitor commands, so no locking is needed
around protocol state. Step-over/in/out loop at instruction level until
the mapped line changes, skipping library-inlined statements.
"""

import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binmon
from binmon import (CMD_ADVANCE, CMD_AUTOSTART, CMD_CHECKPOINT_DELETE,
                    CMD_CHECKPOINT_SET, CMD_EXIT, CMD_PING, CMD_RESET,
                    CMD_UNTIL_RETURN, EVENT_ID, RESP_CHECKPOINT_INFO,
                    RESP_RESUMED, RESP_STOPPED)
import p8map

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CDEBUGGER = os.path.join(os.path.dirname(REPO), "x16_CDebugger")
DEF_BOX16 = os.path.join(CDEBUGGER, "box16-src", "build", "vs2022", "out",
                         "x64", "Release", "box16.exe")
DEF_ROM = os.path.join(CDEBUGGER, "emulator", "rom.bin")

THREAD_ID = 1
STEP_CAP = 5000
LOG = os.environ.get("PROG8_DAP_LOG")


def log(msg):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


class ThreadedMonitor:
    """binmon framing with a dedicated reader thread: responses are routed
    to waiting callers by request id, events go to the shared work queue
    as ('mon', rtype, err, body)."""

    def __init__(self, host, port, work_queue):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.2)
        self.work = work_queue
        self.next_id = 1
        self.pending = {}          # req_id -> queue.Queue(maxsize=1)
        self.closed = False
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_loop(self):
        buf = b""
        while not self.closed:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                break
            while len(buf) >= 12:
                stx, api, body_len = struct.unpack_from("<BBI", buf, 0)
                if stx != binmon.STX or api != binmon.API:
                    log(f"monitor: bad frame header {buf[:2].hex()}")
                    return
                total = 12 + body_len
                if len(buf) < total:
                    break
                rtype, err, rid = struct.unpack_from("<BBI", buf, 6)
                body = buf[12:total]
                buf = buf[total:]
                if rid == EVENT_ID:
                    self.work.put(("mon", rtype, err, body))
                elif rid in self.pending:
                    self.pending.pop(rid).put((rtype, err, body))

    def command(self, cmd, body=b"", timeout=5.0):
        rq = queue.Queue(maxsize=1)
        req_id = self.next_id
        self.next_id += 1
        self.pending[req_id] = rq
        frame = struct.pack("<BBIIB", binmon.STX, binmon.API,
                            len(body), req_id, cmd) + body
        self.sock.sendall(frame)
        try:
            rtype, err, rbody = rq.get(timeout=timeout)
        except queue.Empty:
            self.pending.pop(req_id, None)
            raise TimeoutError(f"monitor command {cmd:#04x} timed out")
        if err != 0:
            raise binmon.MonitorError(f"command {cmd:#04x} -> error {err:#04x}")
        return rbody

    # -- operations (same wire formats as binmon.Monitor) ---------------

    def ping(self):
        self.command(CMD_PING)

    def checkpoint_set(self, start, end=None, op=4, temporary=False):
        end = start if end is None else end
        body = struct.pack("<HHBBBBB", start, end, 1, 1, op, int(temporary), 0)
        rbody = self.command(CMD_CHECKPOINT_SET, body)
        (cp_num,) = struct.unpack_from("<I", rbody, 0)
        return cp_num

    def checkpoint_delete(self, cp_num):
        self.command(CMD_CHECKPOINT_DELETE, struct.pack("<I", cp_num))

    def resume(self):
        self.command(CMD_EXIT)

    def advance(self, step_over=False, count=1):
        self.command(CMD_ADVANCE, struct.pack("<BH", int(step_over), count))

    def until_return(self):
        self.command(CMD_UNTIL_RETURN)

    def reset_paused(self):
        self.command(CMD_RESET, b"\x00")

    def autostart(self, prg_path):
        name = str(prg_path).encode()
        self.command(CMD_AUTOSTART, bytes([1, 0, 0, len(name)]) + name)


class Adapter:
    def __init__(self):
        self.work = queue.Queue()
        self.seq = 0
        self.out_lock = threading.Lock()
        self.mon = None
        self.box16 = None
        self.smap = None
        self.cfg = {}
        self.cwd = None
        self.breakpoints = {}       # source path -> {line: cp_num}
        self.entry_cp = None        # temporary stop-on-entry checkpoint
        self.stopped_pc = None
        self.stop_reason = "pause"
        self.launch_req = None      # deferred launch response
        self.running = True
        self.expected_stops = 0     # STOPPED events our own ops will consume

    # -- DAP wire --------------------------------------------------------

    def _send(self, msg):
        data = json.dumps(msg).encode("utf-8")
        with self.out_lock:
            sys.stdout.buffer.write(
                b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
            sys.stdout.buffer.flush()
        log(f"-> {msg.get('type')} {msg.get('command') or msg.get('event')}")

    def send_response(self, request, body=None, success=True, message=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "response",
               "request_seq": request["seq"], "command": request["command"],
               "success": success}
        if body is not None:
            msg["body"] = body
        if message:
            msg["message"] = message
        self._send(msg)

    def send_event(self, event, body=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "event", "event": event}
        if body is not None:
            msg["body"] = body
        self._send(msg)

    def output(self, text, category="console"):
        self.send_event("output", {"category": category, "output": text + "\n"})

    # -- stdin reader ------------------------------------------------------

    def stdin_loop(self):
        stream = sys.stdin.buffer
        while True:
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                ch = stream.read(1)
                if not ch:
                    self.work.put(("dap-eof",))
                    return
                headers += ch
            length = 0
            for line in headers.decode().split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":")[1])
            data = stream.read(length)
            try:
                self.work.put(("dap", json.loads(data)))
            except ValueError:
                log(f"bad DAP payload: {data[:200]!r}")

    # -- monitor event helpers --------------------------------------------

    def wait_mon(self, rtype, timeout=30.0):
        """Wait for a monitor event, re-queueing DAP requests that arrive
        in the meantime (they are handled after the current operation)."""
        deferred = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"monitor event {rtype:#04x} timed out")
                try:
                    item = self.work.get(timeout=remaining)
                except queue.Empty:
                    continue
                if item[0] == "mon":
                    _, etype, err, body = item
                    if etype == rtype:
                        return err, body
                    # other monitor events during an op are dropped (register
                    # info, resumed/checkpoint chatter)
                else:
                    deferred.append(item)
        finally:
            for item in deferred:
                self.work.put(item)

    def wait_stopped(self, timeout=30.0):
        err, body = self.wait_mon(RESP_STOPPED, timeout)
        (pc,) = struct.unpack_from("<H", body, 0)
        return pc

    # -- source helpers ----------------------------------------------------

    def entry_source(self, entry):
        f = entry["file"]
        if f.startswith("library:"):
            return None
        path = f if os.path.isabs(f) else os.path.normpath(os.path.join(self.cwd, f))
        return {"name": os.path.basename(path), "path": path}

    def frame_name(self, entry):
        """Enclosing sub: nearest earlier entry in the same file whose text
        declares a sub."""
        best, best_line = "(program)", -1
        for e in self.smap.entries:
            if e["file"] == entry["file"] and best_line < e["line"] <= entry["line"]:
                t = e["text"].lstrip()
                if t.startswith(("sub ", "asmsub ")):
                    best = t.split("(")[0].replace("sub ", "").strip() + "()"
                    best_line = e["line"]
        return best

    def report_stop(self, pc, reason):
        self.stopped_pc = pc
        self.stop_reason = reason
        self.send_event("stopped", {"reason": reason, "threadId": THREAD_ID,
                                    "allThreadsStopped": True})

    # -- request handlers ----------------------------------------------------

    def handle(self, req):
        cmd = req["command"]
        handler = getattr(self, "req_" + cmd, None)
        log(f"<- {cmd}")
        if handler is None:
            self.send_response(req, success=True)
            return
        try:
            handler(req)
        except Exception as e:  # report, don't kill the session
            log(f"ERROR in {cmd}: {e!r}")
            self.send_response(req, success=False, message=str(e))

    def req_initialize(self, req):
        self.send_response(req, {
            "supportsConfigurationDoneRequest": True,
            "supportsTerminateRequest": True,
            "supportTerminateDebuggee": True,
        })

    def req_launch(self, req):
        a = req.get("arguments", {})
        self.cfg = a
        program = a["program"]
        stem = os.path.splitext(os.path.basename(program))[0]
        prog_dir = os.path.dirname(os.path.abspath(program))
        default_cwd = (os.path.dirname(prog_dir)
                       if os.path.basename(prog_dir).lower() == "examples"
                       else prog_dir)
        self.cwd = a.get("cwd") or default_cwd
        build = a.get("buildDir") or os.path.join(self.cwd, "build")
        asm = a.get("asm") or os.path.join(build, stem + ".asm")
        prg = a.get("prg") or os.path.join(build, stem + ".prg")
        box16 = a.get("box16") or DEF_BOX16
        rom = a.get("rom") or DEF_ROM

        for path, what in ((asm, "generated assembly (build first!)"),
                           (prg, "program"), (box16, "Box16 fork"), (rom, "rom")):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{what} not found: {path}")

        self.smap, _, summary = p8map.generate(asm)
        self.output(f"source map: {summary}")

        port = int(a.get("port") or 6502)
        host = a.get("host") or "127.0.0.1"
        # the fork ignores CMD_AUTOSTART; CMD_RESET re-arms the boot loader
        # from the -prg given here, so the PRG must be on the command line
        cmdline = [box16, "-ignore_ini", "-binarymonitor", "-rom", rom,
                   "-prg", os.path.abspath(prg), "-run",
                   "-scale", str(a.get("scale") or 1)]
        self.output("launching Box16 ...")
        self.box16 = subprocess.Popen(cmdline, cwd=os.path.dirname(box16),
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while True:
            try:
                self.mon = ThreadedMonitor(host, port, self.work)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("Box16 monitor port never opened")
                time.sleep(0.5)
        self.mon.ping()

        # VS64-proven attach: RESET holds the machine paused at the reset
        # vector and re-arms the -prg boot injection, so breakpoints get
        # installed before any program code runs.
        self.mon.reset_paused()
        self.prg = prg

        # defer the launch response until configurationDone
        self.launch_req = req
        self.send_event("initialized")

    def req_setBreakpoints(self, req):
        a = req["arguments"]
        path = a["source"]["path"]
        wanted = [bp.get("line") for bp in a.get("breakpoints", [])]
        old = self.breakpoints.pop(path, {})
        for cp in old.values():
            try:
                self.mon.checkpoint_delete(cp)
            except Exception as e:
                log(f"checkpoint_delete: {e!r}")
        result, current = [], {}
        for line in wanted:
            entry = (self.smap.line_to_entry(os.path.basename(path), line)
                     or self.smap.next_mapped_line(os.path.basename(path), line))
            if entry is None:
                result.append({"verified": False, "line": line,
                               "message": "no code for this line"})
                continue
            cp = self.mon.checkpoint_set(entry["addr"])
            current[entry["line"]] = cp
            result.append({"verified": True, "line": entry["line"]})
        self.breakpoints[path] = current
        self.send_response(req, {"breakpoints": result})

    def req_configurationDone(self, req):
        if self.cfg.get("stopOnEntry"):
            first = min((e for e in self.smap.entries
                         if not e["file"].startswith("library:")),
                        key=lambda e: e["addr"])
            self.entry_cp = self.mon.checkpoint_set(first["addr"],
                                                    temporary=True)
        self.send_response(req)
        if self.launch_req is not None:
            self.send_response(self.launch_req)
            self.launch_req = None
        self.mon.resume()
        self.output(f"running {os.path.basename(self.prg)}")

    def req_threads(self, req):
        self.send_response(req, {"threads": [{"id": THREAD_ID, "name": "65C02"}]})

    def req_stackTrace(self, req):
        frames = []
        if self.stopped_pc is not None:
            entry = self.smap.addr_to_entry(self.stopped_pc)
            if entry is not None:
                frame = {"id": 1, "name": self.frame_name(entry),
                         "line": entry["line"], "column": 1}
                src = self.entry_source(entry)
                if src:
                    frame["source"] = src
                frames.append(frame)
            else:
                frames.append({"id": 1, "name": f"${self.stopped_pc:04x}",
                               "line": 0, "column": 0})
        self.send_response(req, {"stackFrames": frames,
                                 "totalFrames": len(frames)})

    def req_scopes(self, req):
        self.send_response(req, {"scopes": []})   # variables arrive with M3

    def req_variables(self, req):
        self.send_response(req, {"variables": []})

    def req_continue(self, req):
        self.stopped_pc = None
        self.send_response(req, {"allThreadsContinued": True})
        self.mon.resume()

    def _current_line(self, pc):
        e = self.smap.addr_to_entry(pc)
        if e is None or e["file"].startswith("library:"):
            return None
        return (e["file"], e["line"])

    def _step(self, req, step_over):
        start = self._current_line(self.stopped_pc)
        self.send_response(req)
        pc = self.stopped_pc
        for i in range(STEP_CAP):
            self.mon.advance(step_over=step_over)
            self.wait_mon(RESP_RESUMED, 10)
            pc = self.wait_stopped(60)
            here = self._current_line(pc)
            if here is None:
                # strayed into ROM/library: run to the caller and re-check
                if not step_over:
                    self.mon.until_return()
                    self.wait_mon(RESP_RESUMED, 10)
                    pc = self.wait_stopped(60)
                    here = self._current_line(pc)
                if here is None:
                    continue
            if here != start:
                break
        self.report_stop(pc, "step")

    def req_next(self, req):
        self._step(req, step_over=True)

    def req_stepIn(self, req):
        self._step(req, step_over=False)

    def req_stepOut(self, req):
        self.send_response(req)
        pc = self.stopped_pc
        for _ in range(20):
            self.mon.until_return()
            self.wait_mon(RESP_RESUMED, 10)
            pc = self.wait_stopped(60)
            if self._current_line(pc) is not None:
                break
        self.report_stop(pc, "step")

    def req_pause(self, req):
        # no pause command in the protocol: a temporary exec checkpoint
        # over the whole address space stops on the very next instruction
        self.send_response(req)
        self.mon.checkpoint_set(0x0000, 0xFFFF, temporary=True)

    def req_disconnect(self, req):
        self.shutdown()
        self.send_response(req)
        self.running = False

    def req_terminate(self, req):
        self.shutdown()
        self.send_response(req)
        self.send_event("terminated")

    def shutdown(self):
        if self.mon is not None:
            try:
                for per_file in self.breakpoints.values():
                    for cp in per_file.values():
                        self.mon.checkpoint_delete(cp)
                self.mon.resume()
            except Exception as e:
                log(f"shutdown: {e!r}")
            self.mon.close()
            self.mon = None
        if self.box16 is not None:
            self.box16.terminate()
            try:
                self.box16.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.box16.kill()
            self.box16 = None

    # -- unsolicited stops (breakpoint / entry / pause hits) ---------------

    def on_mon_event(self, rtype, err, body):
        if rtype == RESP_STOPPED:
            (pc,) = struct.unpack_from("<H", body, 0)
            reason = "breakpoint"
            if self.entry_cp is not None:
                entry = min((e for e in self.smap.entries
                             if not e["file"].startswith("library:")),
                            key=lambda e: e["addr"])
                if pc == entry["addr"]:
                    reason = "entry"
                self.entry_cp = None
            self.report_stop(pc, reason)

    # -- main loop -----------------------------------------------------------

    def run(self):
        threading.Thread(target=self.stdin_loop, daemon=True).start()
        while self.running:
            item = self.work.get()
            if item[0] == "dap":
                self.handle(item[1])
            elif item[0] == "mon":
                self.on_mon_event(item[1], item[2], item[3])
            elif item[0] == "dap-eof":
                self.shutdown()
                break


if __name__ == "__main__":
    Adapter().run()
