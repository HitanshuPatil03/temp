# MRIP — one command to a running, populated system (Windows).
#
#     .\run.ps1
#
# This is a wrapper, not a second implementation. The logic lives in run.sh,
# which runs under the bash that ships with Git for Windows — keeping it in one
# place means the Windows path cannot drift from the Linux and macOS one, which
# is exactly what happens to paired scripts over a few months.
#
#     .\run.ps1            bring the stack up and seed it
#     .\run.ps1 stop       stop it, keep the data
#     .\run.ps1 reset      stop it and destroy the data (asks first)
#     .\run.ps1 logs       follow the logs
#     .\run.ps1 status     what is running, on which ports

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root 'run.sh'

if (-not (Test-Path $script)) {
    Write-Host ""
    Write-Host "Cannot continue. run.sh is not next to this script." -ForegroundColor Red
    Write-Host "    Run .\run.ps1 from inside the repository, not from a copy of the file."
    Write-Host ""
    exit 1
}

# Where Git for Windows puts bash. `bash.exe` on PATH is checked first, but the
# usual install does not add it, so the standard locations are tried too.
#
# Deliberately NOT falling back to WSL's bash: it would run the script inside a
# different filesystem and talk to a different Docker context, which fails in a
# way that looks like the script being broken rather than the shell being wrong.
$candidates = @(
    (Get-Command bash.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe",
    "$env:ProgramW6432\Git\bin\bash.exe"
)

$bash = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $bash) {
    Write-Host ""
    Write-Host "Cannot continue. No bash found to run run.sh with." -ForegroundColor Red
    Write-Host ""
    Write-Host "    Git for Windows includes one. Install it and run this again:"
    Write-Host ""
    Write-Host "        https://git-scm.com/download/win"
    Write-Host ""
    Write-Host "    You almost certainly have it already if you cloned this repository."
    Write-Host "    If so, run the script from the Git Bash prompt instead:"
    Write-Host ""
    Write-Host "        ./run.sh"
    Write-Host ""
    exit 1
}

# Pass the arguments through untouched so `.\run.ps1 logs api` behaves the same
# as `./run.sh logs api`.
& $bash $script @args
exit $LASTEXITCODE
