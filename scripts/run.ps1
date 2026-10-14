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
    @('uv', 'ffmpeg') | Where-Object { -not (Get-Command $_ -CommandType Application -ErrorAction SilentlyContinue) }
}

function Get-ManualInstallMessage([string]$tool) {
    $help = @{
        uv = @('uv', 'astral-sh.uv', 'https://docs.astral.sh/uv/getting-started/installation/')
        ffmpeg = @('FFmpeg', 'Gyan.FFmpeg', 'https://ffmpeg.org/download.html')
    }[$tool]
    "To install $($help[0]), run this command:`n  winget install --id $($help[1]) --exact --source winget --scope user`nOr download it from $($help[2]) and add its folder to PATH (the list of folders Windows searches for programs).`nThen close this window and open AutoDJ again."
}

function Warn-MissingFfmpeg {
    [Console]::Error.WriteLine("AutoDJ: FFmpeg is optional for setup, but AutoDJ needs it to play MP3 and M4A files and to stream.`n$(Get-ManualInstallMessage 'ffmpeg')")
}

try {
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
                    Write-Host 'AutoDJ uses uv to install Python and its packages, and FFmpeg to play MP3 and M4A files and to stream.'
                    $answer = if ($acceptInstall) { 'yes' } else {
                        Read-Host ("Download and install {0} for your Windows account with WinGet? [y/N]" -f ($missing -join ', '))
                    }
                    if ($answer -and $answer.Trim() -match '^(y|yes)$') {
                        $ids = @{ uv = 'astral-sh.uv'; ffmpeg = 'Gyan.FFmpeg' }
                        foreach ($tool in $missing) {
                            & $winget.Source install --id $ids[$tool] --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
                            if ($LASTEXITCODE -ne 0) {
                                if ($tool -eq 'uv') {
                                    throw "WinGet could not install uv (exit code $LASTEXITCODE).`n$(Get-ManualInstallMessage 'uv')"
                                }
                                [Console]::Error.WriteLine("AutoDJ: WinGet could not install FFmpeg (exit code $LASTEXITCODE).")
                            }
                        }
                        Update-ProcessPath
                        $missing = @(Find-MissingTools)
                        if ($missing -contains 'uv') {
                            throw "WinGet finished, but Windows still cannot find uv.`n$(Get-ManualInstallMessage 'uv')"
                        }
                        if ($missing -contains 'ffmpeg') {
                            Warn-MissingFfmpeg
                        }
                    } else {
                        if ($missing -contains 'uv') {
                            throw "AutoDJ needs uv to set up Python, so it cannot continue without it.`n$(Get-ManualInstallMessage 'uv')"
                        }
                        Warn-MissingFfmpeg
                    }
                } else {
                    if ($missing -contains 'uv') {
                        throw "AutoDJ needs uv, and WinGet is not available to install it.`n$(Get-ManualInstallMessage 'uv')"
                    }
                    Warn-MissingFfmpeg
                }
            } elseif (-not $noInstall) {
                if ($missing -contains 'uv') {
                    throw "AutoDJ needs uv. It can only offer to install uv when you run it in a terminal window.`n$(Get-ManualInstallMessage 'uv')"
                }
                Warn-MissingFfmpeg
            } elseif (-not (Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
                throw "AutoDJ needs uv the first time it starts.`n$(Get-ManualInstallMessage 'uv')"
            }
        }
    }
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
