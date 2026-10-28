# Reuse an installed runtime; uv supplies bootstrap Python on a fresh checkout.
$ErrorActionPreference = 'Stop'

function Update-ProcessPath {
    $entries = @($env:Path -split ';')
    foreach ($scope in @('Machine', 'User')) {
        $entries += [Environment]::GetEnvironmentVariable('Path', $scope) -split ';'
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $env:Path = (@($entries | Where-Object { $_ -and $seen.Add($_) }) -join ';')
}

function Find-MissingTools {
    @('uv') | Where-Object { -not (Get-Command $_ -CommandType Application -ErrorAction SilentlyContinue) }
}

function Get-ManualInstallMessage([string]$tool) {
    $help = @{
        uv = @('uv', 'astral-sh.uv', 'https://docs.astral.sh/uv/getting-started/installation/')
    }[$tool]
    "To install $($help[0]), run this command:`n  winget install --id $($help[1]) --exact --source winget --scope user`nOr download it from $($help[2]) and add its folder to PATH (the list of folders Windows searches for programs).`nThen close this window and open AutoDJ again."
}

try {
    # Only the launcher marks the starts an update watches.
    Remove-Item -LiteralPath 'Env:AUTODJ_UPDATE_WATCH' -ErrorAction SilentlyContinue
    Update-ProcessPath
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $statePath = Join-Path $projectRoot '.uv\setup.json'
    $runtimePath = Join-Path $projectRoot '.venv'
    $setupRequested = $args.Count -gt 0 -and $args[0] -eq 'setup'
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
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
    $firstSetup = $interactive -and -not (Test-Path -LiteralPath $statePath -PathType Leaf)

    if ($setupRequested -or $firstSetup -or -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        $missing = @(Find-MissingTools)
        if ($missing.Count -gt 0) {
            $noInstall = ($args -contains '--dry-run') -or ($args -contains '--help') -or ($args -contains '-h')
            $acceptInstall = $setupRequested -and ($args -contains '--yes')
            if (-not $noInstall -and ($interactive -or $acceptInstall)) {
                $winget = Get-Command 'winget' -CommandType Application -ErrorAction SilentlyContinue
                if ($winget) {
                    Write-Host 'AutoDJ uses uv to install Python and its packages.'
                    $answer = if ($acceptInstall) { 'yes' } else {
                        Read-Host ("Download and install {0} for your Windows account with WinGet? [y/N]" -f ($missing -join ', '))
                    }
                    if ($answer -and $answer.Trim() -match '^(y|yes)$') {
                        & $winget.Source install --id astral-sh.uv --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
                        if ($LASTEXITCODE -ne 0) {
                            throw "WinGet could not install uv (exit code $LASTEXITCODE).`n$(Get-ManualInstallMessage 'uv')"
                        }
                        Update-ProcessPath
                        $missing = @(Find-MissingTools)
                        if ($missing -contains 'uv') {
                            throw "WinGet finished, but Windows still cannot find uv.`n$(Get-ManualInstallMessage 'uv')"
                        }
                    } else {
                        throw "AutoDJ needs uv to set up Python, so it cannot continue without it.`n$(Get-ManualInstallMessage 'uv')"
                    }
                } else {
                    throw "AutoDJ needs uv, and WinGet is not available to install it.`n$(Get-ManualInstallMessage 'uv')"
                }
            } elseif (-not $noInstall) {
                throw "AutoDJ needs uv. It can only offer to install uv when you run it in a terminal window.`n$(Get-ManualInstallMessage 'uv')"
            } elseif (-not (Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
                throw "AutoDJ needs uv the first time it starts.`n$(Get-ManualInstallMessage 'uv')"
            }
        }
    }
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        $versionPath = Join-Path $projectRoot '.python-version'
        $pythonVersion = "$(Get-Content -LiteralPath $versionPath -TotalCount 1)".Trim()
        if (-not $pythonVersion) {
            throw "$versionPath is empty; it must name a Python version such as 3.14."
        }
        & uv run --no-project --python $pythonVersion (Join-Path $PSScriptRoot 'run.py') @args
    } else {
        & $runtimePython (Join-Path $PSScriptRoot 'run.py') @args
    }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("AutoDJ: $($_.Exception.Message)")
    exit 1
}
