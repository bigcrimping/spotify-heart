# Build and run the host unit tests with TinyCC (https://bellard.org/tcc/).
#
#   .\test\run_tests.ps1            runs the unit tests
#   .\test\run_tests.ps1 -Fixtures  also replays test\fixtures\*.txt captures
#
# tcc.exe is looked up on PATH; set $env:TCC to point at it otherwise.
param([switch]$Fixtures)

$tcc = $env:TCC
if (-not $tcc) {
    $cmd = Get-Command tcc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $tcc = $cmd.Source }
}
if (-not $tcc -or -not (Test-Path $tcc)) {
    Write-Error "tcc.exe not found on PATH (or set `$env:TCC to its full path)"
    exit 2
}

$src = Join-Path $PSScriptRoot "test_frame.c"
$fixtureArgs = @()
if ($Fixtures) {
    $fixtureArgs = Get-ChildItem (Join-Path $PSScriptRoot "fixtures") -Filter *.txt -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName }
}

& $tcc -Wall -Werror -I (Join-Path $PSScriptRoot "..\main") -run $src @fixtureArgs
exit $LASTEXITCODE
