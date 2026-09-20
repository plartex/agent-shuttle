$runtime = Join-Path $PSScriptRoot '.runtime'
foreach ($agent in @('codex', 'antigravity')) {
    $pidFile = Join-Path $runtime "$agent.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $bridgePid -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $bridgePid
        Write-Output "Stopped $agent (PID $bridgePid)"
    }
    Remove-Item -LiteralPath $pidFile
}
