#!/usr/bin/env python3
"""M4 test: multi-file (%import) debugging and %breakpoint sync.

Launches examples/multi.p8 (which imports examples/textutils.p8 and
carries a %breakpoint directive) and asserts:

  - a gutter breakpoint in the IMPORTED file (textutils.p8) verifies
    and hits, with the stack frame showing that file and sub;
  - locals there include the parameter n, globals are the textutils
    block's (calls);
  - hitting it again sees n increment (the repeat-5 loop);
  - after removing it, the program runs into the %breakpoint directive
    and stops on multi.p8's line 17.

Run:  python test/dap_multi.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dap_smoke import DapClient, check, stack_line, REPO

PROGRAM = os.path.join(REPO, "examples", "multi.p8")
MODULE = os.path.join(REPO, "examples", "textutils.p8")


def main():
    c = DapClient()
    r = c.request("initialize", {"adapterID": "prog8"})
    c.wait_response(r)

    launch_seq = c.request("launch", {"program": PROGRAM, "cwd": REPO})
    c.wait_event("initialized", timeout=90)

    # breakpoint in the IMPORTED module: line 9 = calls++
    r = c.request("setBreakpoints", {"source": {"path": MODULE},
                                     "breakpoints": [{"line": 9}]})
    bps = c.wait_response(r)["body"]["breakpoints"]
    check(bps[0]["verified"] and bps[0]["line"] == 9,
          "breakpoint in imported textutils.p8:9 verified")

    r = c.request("configurationDone")
    c.wait_response(r)
    c.wait_response(launch_seq)

    ev = c.wait_event("stopped", timeout=90)
    check(ev["body"]["reason"] == "breakpoint", "hit in imported file")
    f = stack_line(c)
    src = os.path.basename(f["source"]["path"])
    check(src == "textutils.p8" and f["line"] == 9
          and f["name"] == "announce()",
          "stack frame shows textutils.p8:9 in announce()")

    r = c.request("scopes", {"frameId": 1})
    scopes = {s["name"].split()[0]: s["variablesReference"]
              for s in c.wait_response(r)["body"]["scopes"]}
    check("Locals" in scopes and "Globals" in scopes,
          f"scopes in module: {list(scopes)}")
    r = c.request("variables", {"variablesReference": scopes["Locals"]})
    lvars = {v["name"]: v["value"] for v in c.wait_response(r)["body"]["variables"]}
    n1 = int(lvars["n"].split()[0])
    check(n1 == 1, f"param n == 1 on first call (got {lvars['n']})")

    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    c.wait_event("stopped", timeout=30)
    r = c.request("scopes", {"frameId": 1})
    scopes = {s["name"].split()[0]: s["variablesReference"]
              for s in c.wait_response(r)["body"]["scopes"]}
    r = c.request("variables", {"variablesReference": scopes["Globals"]})
    gvars = {v["name"]: v["value"] for v in c.wait_response(r)["body"]["variables"]}
    r = c.request("variables", {"variablesReference": scopes["Locals"]})
    lvars = {v["name"]: v["value"] for v in c.wait_response(r)["body"]["variables"]}
    n2 = int(lvars["n"].split()[0])
    calls = int(gvars["calls"].split()[0])
    check(n2 == 2 and calls == 1,
          f"second hit: n == 2, textutils.calls == 1 (got n={n2}, calls={calls})")

    # drop the gutter breakpoint; the %breakpoint directive must stop next
    r = c.request("setBreakpoints", {"source": {"path": MODULE},
                                     "breakpoints": []})
    c.wait_response(r)
    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=30)
    f = stack_line(c)
    src = os.path.basename(f["source"]["path"])
    check(ev["body"]["reason"] == "breakpoint" and src == "multi.p8"
          and 17 <= f["line"] <= 18,
          f"%breakpoint directive stops at multi.p8:{f['line']}")

    r = c.request("disconnect", {"terminateDebuggee": True})
    c.wait_response(r)
    c.proc.wait(timeout=15)
    print("DONE - multi-file + %breakpoint test passed")


if __name__ == "__main__":
    main()
