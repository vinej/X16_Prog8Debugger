# Build a prog8 program with the in-repo SDK (prog8-sdk\).
# Called by the "prog8: build" VSCode tasks; usable standalone:
#   powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 [program.p8] [-NoOpt]
# -NoOpt passes prog8c's -noopt: no optimizations, so every source line
# keeps its code and stays steppable/inspectable in the debugger.
param([string]$Program = "examples\bounce.p8", [switch]$NoOpt)

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# prog8c needs Java 11+; prefer the newest Adoptium JDK, fall back to PATH.
$java = Get-ChildItem 'C:\Program Files\Eclipse Adoptium\jdk-*\bin\java.exe' -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $java) { $java = 'java' }

# prog8c invokes 64tass from the PATH
$env:PATH = "$repo\prog8-sdk;" + $env:PATH

$opts = @("-target", "cx16", "-asmlist", "-out", "build")
if ($NoOpt) { $opts += "-noopt" }
& $java -jar "$repo\prog8-sdk\prog8c.jar" @opts $Program
exit $LASTEXITCODE
