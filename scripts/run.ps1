# Reuse an installed runtime; uv supplies bootstrap Python on a fresh checkout.
$ErrorActionPreference = 'Stop'
try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $statePath = Join-Path $projectRoot '.uv\setup.json'
    $runtimePath = Join-Path $projectRoot '.venv'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        # Python validates this state. A broken selection must still allow `setup`.
        try {
            $selection = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ($selection.environment -is [string] -and -not [string]::IsNullOrWhiteSpace($selection.environment)) {
                $runtimePath = $selection.environment
                if (-not [System.IO.Path]::IsPathRooted($runtimePath)) {
                    $runtimePath = Join-Path $projectRoot $runtimePath
                }
            }
        } catch {
            $runtimePath = Join-Path $projectRoot '.venv'
        }
    }
    $runtimePython = Join-Path $runtimePath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        & uv run --no-project --python 3.14 (Join-Path $PSScriptRoot 'run.py') @args
    } else {
        & $runtimePython (Join-Path $PSScriptRoot 'run.py') @args
    }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("AutoDJ: $($_.Exception.Message)")
    exit 1
}
