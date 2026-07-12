# X16 Prog8 Debugger

**Goal: source-level debugging of [Prog8](https://github.com/irmen/prog8)
programs on the Commander X16 in VSCode** — breakpoints on `.p8` lines,
step, inspect — using the emulator infrastructure already proven by
[x16_CDebugger](https://github.com/vinej/x16_CDebugger) (six toolchains)
and being extended by
[X16_BasicDebugger](https://github.com/vinej/X16_BasicDebugger):

```
VSCode ──DAP──► custom debug adapter ──VICE binary monitor (TCP 6502)──► Box16 fork
```

Status: **all milestones (M0–M4) done** — source map generator, live
source-level stepping, and the VSCode extension with its Python DAP
adapter: breakpoints (incl. `%import`ed modules and `%breakpoint`
directives), step over/into/out, stack, run control, and variables
(Locals/Globals panes, hover, edit-value). The repo root IS the VSCode
extension. What already works without this
repo (tier 1) lives in x16_CDebugger's `prog8\` folder: build tasks +
*symbolic* debugging inside Box16's own debugger via prog8's
`.vice-mon-list`.

## The VSCode extension (M2)

The repo doubles as a VSCode extension (no build step, no npm — the
debug adapter is Python). It contributes:

* **Debugging** (`type: "prog8"`): F5 builds via the `prog8: build`
  task, launches the Box16 fork with the PRG, regenerates the source
  map, and attaches over the binary monitor. Breakpoints on `.p8` lines
  (auto-adjusted to the next statement), step over/into/out at source
  level (library-inlined code is stepped through), stop-on-entry,
  call-stack line highlight, pause via a temporary full-range exec
  checkpoint. Uses the fork's VS64 attach semantics: `CMD_RESET` holds
  the machine paused and re-arms the `-prg` boot injection, so
  breakpoints arm before the program starts (`CMD_AUTOSTART` is a no-op
  in the fork — the PRG must be on the Box16 command line).
* **Language support for `.p8`**: syntax highlighting (TextMate
  grammar incl. `%asm {{ }}` blocks with 65C02 opcodes), comment/
  bracket configuration, and completions for keywords, types,
  directives, builtins, and the common library modules
  (`txt.`, `sys.`, `cx16.`, `sprites.`, `psg.`, …).

Install (already done on this machine) — junction the repo into the
extensions folder and restart VSCode:

```powershell
New-Item -ItemType Junction `
  -Path "$env:USERPROFILE\.vscode\extensions\vinej.x16-prog8-debug" `
  -Target c:\quartus\projects\X16_Prog8Debugger
```

Then open this folder in VSCode, open `examples\bounce.p8`, click
breakpoints in the gutter, and press **F5** (config in
`.vscode\launch.json`).

**Performance — in-core line stepping:** `p8map.py` also writes a
`.dbj` line table next to the PRG. The Box16 fork auto-loads
`<prg>.dbj` and then executes each ADVANCE as a *whole source line
inside the emulator core* (the line-granular stepping added for
Oscar64/VS64, reused for prog8): one monitor round-trip per step (~14
ms), instead of one per instruction, and library/ROM code is ground
through in-core without touching the wire. The adapter's client-side
step loop remains as a fallback, so debugging still works if the
`.dbj` is missing. Variable-scope reads are batched into contiguous
MEMORY_GET spans for the same reason (both lessons imported from
X16_BasicDebugger's fork work). With the fork's sub-ms paused command
servicing (commit `bbeae4e`), measured on this adapter: paused
MEMORY_GET median **0.02 ms** (was ~17 ms — a full frame), line step
median **0.1 ms**.

The adapter is verified headlessly by `test\dap_smoke.py`, which plays
VSCode over stdio against a real Box16: entry stop → breakpoint hits →
next/stepIn/stepOut → continue → disconnect. Run it after adapter
changes (plus `test\dap_multi.py` for multi-file + `%breakpoint`). Set
`PROG8_DAP_LOG=<file>` to trace the adapter.

## Tools (working)

- `tools\p8map.py` — **M0**: builds the `p8 line ↔ address` map from a
  prog8 build. The listing prog8c writes has no asm line numbers, so the
  tool re-assembles the `.asm` with 64tass `--line-numbers` (reusing the
  flags recorded in the listing header) for an exact join, and
  byte-compares the regenerated PRG against the original as proof the
  addresses match the program that actually runs. Output:
  `<name>.p8map.json` next to the asm.

  ```
  python tools\p8map.py build\bounce.asm [--dump]
  ```

- `tools\binmon.py` — the VICE binary-monitor client library (framing
  from `box16-src\test\binmon_test.py`; supports the fork's bank-aware
  checkpoints). This is the transport the DAP adapter will reuse.

- `tools\step_probe.py` — **M1**: sets an exec checkpoint on a `.p8`
  line's address, waits for the hit, round-trips PC → line, then
  steps (step-over) until the mapped line changes. `--launch` starts
  Box16 itself; non-statement lines are adjusted to the next mapped
  line, as a DAP adapter would.

  ```
  python tools\step_probe.py --launch --line 84
  ```

  (defaults: this repo's `build\bounce.p8map.json` / `build\bounce.prg`,
  the shared Box16 fork + rom from the sibling `x16_CDebugger` checkout)

  Verified live (2026-07-12): breakpoint on `bounce.p8:84` hits at
  `$0895`, one step-over lands on line 85; lines 81 (adjusted → 82)
  and 87 pass too.

## Toolchain (`prog8-sdk\`, gitignored)

Binaries are copied into the repo but not committed. Get them here:

| File | Version | Where to get it |
| --- | --- | --- |
| `prog8-sdk\prog8c.jar` | 12.2.1 | <https://github.com/irmen/prog8/releases> |
| `prog8-sdk\64tass.exe` | 1.60.3243 | <https://sourceforge.net/projects/tass64/files/> |
| `emulator\box16.exe` (+ `SDL2.dll`, `zlibwapi.dll`, `icons.png`) | fork ≥ `bbeae4e` | build [vinej/box16, branch `binary-monitor`](https://github.com/vinej/box16/tree/binary-monitor) (VS2022 solution in `build\vs2022`); `bbeae4e` adds sub-ms paused command servicing — older fork builds work but every stop/step costs ~17 ms per monitor round-trip |
| `emulator\rom.bin` | R48 | <https://github.com/X16Community/x16-emulator/releases> (or copy from an official emulator install) |

`prog8c` needs **Java 11+** (e.g. an [Adoptium](https://adoptium.net/)
JDK). Build the test program into `build\` with `build.ps1` (also what
the `prog8: build` task runs — it finds the newest Adoptium JDK and puts
`prog8-sdk\` on the PATH for 64tass):

```
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 examples\bounce.p8
```

## Why VS64 cannot host this one

Prog8 compiles `.p8` → 64tass assembly → **64tass**. VS64 2.6.2 has no
prog8 or 64tass toolkit (hardcoded list: acme, kick, cc65, oscar64, llvm,
basic), so there is nothing to configure — the VSCode client must be new
code. Two candidate shapes:

1. **A custom DAP debug adapter** (recommended): small VSCode extension
   speaking the VICE binary monitor directly. The protocol side is fully
   proven — the Box16 fork (`vinej/box16`, branch `binary-monitor`)
   already serves six VS64 toolchains, and the X16_BasicDebugger project
   drives it from Python probes.
2. An upstream VS64 PR adding a prog8/64tass toolkit — bigger, and the
   debug-info story below would still have to be built.

## The source map exists — that's the key feasibility fact

A source-level debugger needs `.p8 line ↔ machine address`. Prog8 gives
both halves (verified with prog8c 12.2.1):

* `prog8c` embeds the original source lines in its generated assembly as
  `; source: examples\bounce.p8:NN` comments (**default behavior**;
  `-nosourcelines` turns it off) → asm line ↔ p8 line.
* `-asmlist` writes the 64tass listing → asm line ↔ address.
* Joining them yields the map — the moral equivalent of Oscar64's `.dbj`
  or DWARF's line table.

Bonus artifacts, also verified: `.vice-mon-list` (labels + `%breakpoint`
entries, loaded by Box16 `-sym`), and the listing carries the full
symbol/section layout.

## Shared foundation with X16_BasicDebugger

The BASIC project has already proven, live, the two runtime facts this
project needs (see its repo, `docs/`):

* The fork's binary monitor drives Box16 exactly as VS64 does; its
  protocol reference client is `box16-src\test\binmon_test.py`.
* **Bank-aware checkpoints** were added to the fork for BASIC M1
  (optional trailing u16 bank on `CHECKPOINT_SET`) — Prog8 programs live
  in low RAM so plain 16-bit checkpoints suffice, but banked-RAM Prog8
  code (`memory()` in banked RAM, ROM calls) can use the same extension.

The M3 client milestone of the BASIC project and this project's adapter
are natural siblings: **one DAP adapter with pluggable source-map
providers** (BASIC line map / prog8 listing map / raw 64tass listing for
assembly) would serve both. Coordinate before building two.

## Milestones

- [x] **M0 — source map generator:** `tools\p8map.py` parses
  `build\bounce.asm` (source comments) + a 64tass `--line-numbers`
  listing into a `p8 line ↔ address` table. Validated: reassembled PRG
  is byte-identical to prog8c's, entries hand-checked against the
  listing, and every `p8b_main` sub label in `.vice-mon-list` resolves
  to a line inside that sub. One address can carry several lines (sub
  header + first statement, loop head + inlined library code) — the
  lookup prefers the program's own statements for PC display.
- [x] **M1 — proof of stepping:** `tools\step_probe.py` (transport
  reused from the BASIC project's probes / `binmon_test.py`): checkpoint
  on a mapped `.p8` line in the running bounce demo, hit, PC mapped back
  to the line, step-over until the line changes. Passes live against the
  Box16 fork on lines 84, 87, and 81 (non-statement, auto-adjusted).
- [x] **M2 — the DAP adapter:** VSCode debug extension: launch (build
  via prog8c task, start Box16 fork, attach), line breakpoints, step
  over/into/out, PC→line highlight, plus `.p8` syntax highlighting and
  completions. Lives in this repo (root = extension,
  `tools\dap_adapter.py` = adapter); the monitor/DAP layers are
  source-map-agnostic so X16_BasicDebugger can plug a BASIC line-map
  provider into the same adapter. Verified by `test\dap_smoke.py`
  against a real Box16.
- [x] **M3 — variables:** the map carries every `p8v_*` variable with
  scope/address/type (zeropage equates parsed from the asm's `; zp`
  comments, memory variables resolved through the VICE label file). The
  adapter shows **Locals** (current sub) and **Globals** (block) scopes,
  formats by type (signed/unsigned, bool, arrays, MFLPT5 floats),
  supports hover **evaluate** and **setVariable**. Reads are batched
  into contiguous MEMORY_GET spans (variables cluster in zp), so a scope
  refresh is 1–2 monitor round-trips, not one per variable — the
  slow-variables lesson from X16_BasicDebugger applied from the start.
- [x] **M4 — polish:** multi-file programs work end to end —
  breakpoints/stepping/variables in `%import`ed modules
  (`examples\multi.p8` + `examples\textutils.p8`, verified by
  `test\dap_multi.py`). `%breakpoint` directives (compiled to
  `_prog8_breakpoint_N` labels) are armed automatically at launch
  (`syncBreakpoints: false` disables). Banked checkpoints: the client
  plumbing for the fork's bank extension (trailing u16 bank on
  CHECKPOINT_SET) is in `binmon.py` and the adapter's monitor; it will
  be used automatically once prog8 programs place code in banked RAM —
  no bank inference exists yet because the map has no bank column.

## Test program

`examples\bounce.p8` — the Prog8 port of the shared bounce demo (copied
from `x16_CDebugger\prog8\examples\`), the same demo every other
toolchain in the ecosystem uses, so debugger behavior is directly
comparable. Build it into `build\` with the command in the Toolchain
section; the tools' defaults point at that build.

`examples\multi.p8` (+ `examples\textutils.p8`) — the multi-file /
`%breakpoint` test program used by `test\dap_multi.py`.

## References

- [x16_CDebugger `prog8\`](https://github.com/vinej/x16_CDebugger) —
  tier-1 integration (tasks, bounce.p8, prog8-sdk layout).
- [X16_BasicDebugger](https://github.com/vinej/X16_BasicDebugger) — the
  sibling project; shares the fork, the probes, and (eventually) the
  adapter.
- [Prog8 compiling docs](https://prog8.readthedocs.io/en/stable/compiling.html)
  — `-asmlist`, `-nosourcelines`, `.vice-mon-list`, `-breakinstr`.
- [vinej/box16 `binary-monitor`](https://github.com/vinej/box16/tree/binary-monitor)
  — the emulator fork; protocol notes in x16_CDebugger's `debugger.md`.
