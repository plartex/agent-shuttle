$ErrorActionPreference = 'Stop'
$bridgeRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $PSScriptRoot '.runtime'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Bridge environment missing: $python"
}
if ($env:BRIDGE_AGY_MODE -eq 'sdk') {
    if (-not (Test-Path -LiteralPath (Join-Path $bridgeRoot '.venv-agy\Scripts\python.exe'))) {
        Write-Warning 'Optional Antigravity SDK environment is missing.'
    }
} else {
    $agyCommand = $env:BRIDGE_AGY_COMMAND
    if (-not $agyCommand) {
        $candidate = Join-Path $bridgeRoot 'bin\agy.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $agyCommand = $candidate }
    }
    if (-not $agyCommand) {
        $found = Get-Command agy -ErrorAction SilentlyContinue
        if ($found) { $agyCommand = $found.Source }
    }
    if (-not $agyCommand) {
        $candidate = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $agyCommand = $candidate }
    }
    if (-not $agyCommand) {
        $agyCommand = 'agy'
        Write-Warning 'agy CLI is not installed; Antigravity tasks need it in the default harness mode.'
    }
    $env:BRIDGE_AGY_COMMAND = $agyCommand
}
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
foreach ($agent in @(@{ Name = 'codex'; Port = 8765 }, @{ Name = 'antigravity'; Port = 8766 })) {
    $pidFile = Join-Path $runtime "$($agent.Name).pid"
    if (Test-Path -LiteralPath $pidFile) {
        $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
        if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
            Write-Output "$($agent.Name) already running (PID $existingPid)"
            continue
        }
    }
    $launchArgs = @('-m', 'agent_bridge.cli', 'serve', $agent.Name, '--port', [string]$agent.Port)
    if ($agent.Name -eq 'antigravity') {
        if ($env:BRIDGE_AGY_MODE -eq 'sdk') {
            $launchArgs += @('--agy-mode', 'sdk')
        }
    }
    $process = Start-Process -FilePath $python `
        -ArgumentList $launchArgs `
        -WorkingDirectory $bridgeRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "$($agent.Name).out.log") `
        -RedirectStandardError (Join-Path $runtime "$($agent.Name).err.log")
    Set-Content -LiteralPath $pidFile -Value $process.Id
    Write-Output "$($agent.Name): http://127.0.0.1:$($agent.Port) (PID $($process.Id))"
}
