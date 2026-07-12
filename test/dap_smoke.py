#!/usr/bin/env python3
"""End-to-end smoke test for tools/dap_adapter.py.

Plays VSCode: spawns the adapter on stdio, launches bounce.p8 in the
real Box16 fork, and asserts the full debug session:

  stopOnEntry (line 66) -> breakpoint 84 -> next -> 85 -> stepIn ->
  inside move_axis_y() -> stepOut -> back in start() -> continue ->
  84 again -> disconnect (Box16 gone).

Run:  python test/dap_smoke.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ADAPTER = os.path.join(REPO, "tools", "dap_adapter.py")
PROGRAM = os.path.join(REPO, "examples", "bounce.p8")


class DapClient:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, ADAPTER],
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.seq = 0
        self.incoming = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        out = self.proc.stdout
        while True:
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                ch = out.read(1)
                if not ch:
                    return
                headers += ch
            length = int(headers.decode().split(":")[1].strip().split("\r\n")[0])
            self.incoming.put(json.loads(out.read(length)))

    def request(self, command, arguments=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        data = json.dumps(msg).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()
        return self.seq

    def wait(self, pred, what, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("waiting for " + what)
            try:
                msg = self.incoming.get(timeout=remaining)
            except queue.Empty:
                continue
            if pred(msg):
                return msg

    def wait_response(self, req_seq, timeout=60):
        m = self.wait(lambda m: m.get("type") == "response"
                      and m.get("request_seq") == req_seq,
                      f"response {req_seq}", timeout)
        assert m["success"], f"request failed: {m.get('message')} / {m}"
        return m

    def wait_event(self, event, timeout=60):
        return self.wait(lambda m: m.get("type") == "event"
                         and m.get("event") == event, f"event {event}", timeout)


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


def stack_line(c):
    rs = c.request("stackTrace", {"threadId": 1})
    frames = c.wait_response(rs)["body"]["stackFrames"]
    assert frames, "empty stack"
    f = frames[0]
    src = f.get("source", {}).get("path", "(none)")
    print(f"     top frame: {f['name']} at {os.path.basename(src)}:{f['line']}")
    return f


def main():
    c = DapClient()
    r = c.request("initialize", {"adapterID": "prog8"})
    body = c.wait_response(r).get("body", {})
    check(body.get("supportsConfigurationDoneRequest"), "initialize capabilities")

    launch_seq = c.request("launch", {
        "program": PROGRAM, "cwd": REPO, "stopOnEntry": True})
    c.wait_event("initialized", timeout=90)
    check(True, "launch -> initialized event (Box16 up, reset+autostart done)")

    r = c.request("setBreakpoints", {
        "source": {"path": PROGRAM},
        "breakpoints": [{"line": 84}, {"line": 12}]})
    bps = c.wait_response(r)["body"]["breakpoints"]
    check(bps[0]["verified"] and bps[0]["line"] == 84,
          "breakpoint on line 84 verified")
    check(bps[1]["verified"] and bps[1]["line"] > 12,
          f"breakpoint on comment line 12 adjusted to {bps[1]['line']}")

    r = c.request("configurationDone")
    c.wait_response(r)
    c.wait_response(launch_seq)
    check(True, "configurationDone + launch responses")

    ev = c.wait_event("stopped", timeout=90)
    check(ev["body"]["reason"] == "entry", "stopOnEntry stop")
    f = stack_line(c)
    check(f["line"] == 66 and f["name"] == "start()",
          "entry stop is bounce.p8:66 in start()")

    # the adjusted line-51 breakpoint is pos_x's init statement, which
    # runs during program startup -- it must hit before line 84 does
    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=60)
    f = stack_line(c)
    check(ev["body"]["reason"] == "breakpoint" and f["line"] == 51,
          "init-statement breakpoint (line 51) hits first")

    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=60)
    check(ev["body"]["reason"] == "breakpoint", "breakpoint stop reason")
    f = stack_line(c)
    check(f["line"] == 84, "stopped on line 84")

    r = c.request("next", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=60)
    f = stack_line(c)
    check(ev["body"]["reason"] == "step" and f["line"] == 85,
          "next: 84 -> 85")

    r = c.request("stepIn", {"threadId": 1})
    c.wait_response(r)
    c.wait_event("stopped", timeout=60)
    f = stack_line(c)
    check(f["name"] == "move_axis_y()" and 119 <= f["line"] <= 135,
          "stepIn: inside move_axis_y()")

    r = c.request("stepOut", {"threadId": 1})
    c.wait_response(r)
    c.wait_event("stopped", timeout=60)
    f = stack_line(c)
    check(f["name"] == "start()" and 82 <= f["line"] <= 91,
          "stepOut: back in start()")

    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=60)
    f = stack_line(c)
    check(ev["body"]["reason"] == "breakpoint" and f["line"] == 84,
          "continue: hits line 84 again")

    r = c.request("disconnect", {"terminateDebuggee": True})
    c.wait_response(r)
    c.proc.wait(timeout=15)
    check(c.proc.returncode is not None, "adapter exited on disconnect")

    time.sleep(1)
    tasks = subprocess.run(["tasklist", "/FI", "IMAGENAME eq box16.exe"],
                           capture_output=True, text=True).stdout
    check("box16.exe" not in tasks, "Box16 terminated")
    print("DONE - DAP smoke test passed")


if __name__ == "__main__":
    main()
