$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '.runtime'
$python = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')).Path
foreach ($agent in @('codex', 'antigravity')) {
    $pidFile = Join-Path $runtime "$agent.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
    if ($process) {
        if ($process.Path -ne $python) {
            throw "PID $bridgePid in $pidFile does not belong to Agent Bridge Python: $($process.Path)"
        }
        Stop-Process -Id $bridgePid -Force -ErrorAction Stop
        Write-Output "Stopped $agent (PID $bridgePid)"
    }
    Remove-Item -LiteralPath $pidFile
}
