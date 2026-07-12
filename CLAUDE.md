# X16_Prog8Debugger — project context for Claude

## What this project is

Source-level debugging of **Prog8** programs (Commander X16 target) in
VSCode, via a custom DAP adapter speaking the VICE binary monitor to the
Box16 fork (`vinej/box16`, branch `binary-monitor`). Read `README.md`
first — it carries the charter, the verified feasibility facts, and
milestones M0–M4.

## Relationship to the sibling projects (important)

- `c:\quartus\projects\x16_CDebugger` — the working six-toolchain
  debugging repo. Its `prog8\` folder is the **tier-1** Prog8 integration
  (build tasks + symbolic Box16 debugging) and hosts the shared test
  program `prog8\examples\bounce.p8`, plus `prog8-sdk\` (prog8c.jar
  v12.2.1 + 64tass.exe, gitignored). prog8c needs **Java 11+**; use
  `C:\Program Files\Eclipse Adoptium\jdk-21.0.3.9-hotspot\bin\java.exe`
  (system default java is 1.8 — too old).
- `c:\quartus\projects\X16_BasicDebugger` — the BASIC V2 debugger project
  (own VSCode window/session). It has ALREADY proven: monitor-driven
  probes (its `experiments\`), and **bank-aware checkpoints in the fork**
  (optional trailing u16 bank on CHECKPOINT_SET; regression
  `binmon_test.py` extended — run it with the ca65 build's .lbl, the acme
  one uses an unparseable format). Its M3 "client" milestone and this
  project's M2 adapter should be ONE shared DAP adapter with pluggable
  source-map providers — coordinate, don't duplicate.
- The Box16 fork clone is `x16_CDebugger\box16-src` — shared by all
  projects; changes must keep `box16-src\test\binmon_test.py` green so
  the six existing VS64 debug flows keep working.

## Verified facts (do not re-derive)

- prog8c generated asm contains `; source: <file>.p8:NN` comments by
  default; `-asmlist` writes the 64tass listing with addresses. Together
  they give the p8-line↔address source map (M0 = write the parser).
- `.vice-mon-list` loads into Box16 via `-sym` (labels + %breakpoint).
- bounce.p8 compiles first-try with prog8c 12.2.1 → 2208-byte PRG; the
  Box16 fork runs it with the monitor listening.
- VS64 has NO prog8/64tass toolkit — a custom client is the only path
  (details of VS64's internals: X16_BasicDebugger's
  `docs/vs64-basic-internals.md` and x16_CDebugger's README sections).

## User workflow preferences

- GitHub user `vinej`; projects go public there. Commit/push only after
  the user confirms things work (or after Claude-side CLI verification
  for non-interactive pieces).
- Third-party binaries are copied INTO repos but gitignored, with README
  tables telling users where to get them.
- Claude verifies CLI-first; interactive VSCode tests are handed to the
  user with precise steps.
- This repo: git initialized, no commits yet, no GitHub remote yet.
