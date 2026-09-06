$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$existing = Get-NetTCPConnection -State Listen -LocalPort 4173 -ErrorAction SilentlyContinue
if (-not $existing) {
    Start-Process -FilePath $python `
        -ArgumentList @('stats_server.py', '--host', '127.0.0.1', '--port', '4173') `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden
}

Start-Sleep -Milliseconds 500
Start-Process 'http://127.0.0.1:4173/delta-stats-page.html'
