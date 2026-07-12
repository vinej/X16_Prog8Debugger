# Build a prog8 program with the in-repo SDK (prog8-sdk\).
# Called by the "prog8: build" VSCode task; usable standalone:
#   powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 [program.p8]
param([string]$Program = "examples\bounce.p8")

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# prog8c needs Java 11+; prefer the newest Adoptium JDK, fall back to PATH.
$java = Get-ChildItem 'C:\Program Files\Eclipse Adoptium\jdk-*\bin\java.exe' -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $java) { $java = 'java' }

# prog8c invokes 64tass from the PATH
$env:PATH = "$repo\prog8-sdk;" + $env:PATH

& $java -jar "$repo\prog8-sdk\prog8c.jar" -target cx16 -asmlist -out build $Program
exit $LASTEXITCODE
