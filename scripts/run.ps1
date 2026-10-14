# Reuse an installed runtime; uv supplies bootstrap Python on a fresh checkout.
$ErrorActionPreference = 'Stop'

function Update-ProcessPath {
    $entries = @($env:Path -split ';')
    foreach ($scope in @('User', 'Machine')) {
        $entries += [Environment]::GetEnvironmentVariable('Path', $scope) -split ';'
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $env:Path = (@($entries | Where-Object { $_ -and $seen.Add($_) }) -join ';')
}

function Find-MissingTools {
    @('uv', 'ffmpeg') | Where-Object { -not (Get-Command $_ -CommandType Application -ErrorAction SilentlyContinue) }
}

function Get-ManualInstallMessage([string[]]$tools) {
    $commands = @{
        uv = "winget install --id astral-sh.uv --exact --source winget --scope user`n  Download instructions: https://docs.astral.sh/uv/getting-started/installation/"
        ffmpeg = "winget install --id Gyan.FFmpeg --exact --source winget --scope user`n  Download instructions: https://ffmpeg.org/download.html"
    }
    $instructions = ($tools | ForEach-Object { "  $_`: $($commands[$_])" }) -join [Environment]::NewLine
    "Windows could not find $($tools -join ' and '). Run the command below or follow the download instructions. For a manual installation, add the executable folder to PATH (Windows' list of program folders).`n$instructions`nThen close this window, open AutoDJ again, and retry setup."
}

function Warn-MissingFfmpeg {
    [Console]::Error.WriteLine("AutoDJ: FFmpeg is optional for setup but needed for MP3/M4A audio and streaming. $(Get-ManualInstallMessage @('ffmpeg'))")
}

try {
    Update-ProcessPath
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $statePath = Join-Path $projectRoot '.uv\setup.json'
    $runtimePath = Join-Path $projectRoot '.venv'
    $setupRequested = $args.Count -gt 0 -and $args[0] -eq 'setup'
    $savedRuntime = Test-Path -LiteralPath $statePath -PathType Leaf
    if ($savedRuntime) {
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

    if ($setupRequested -or -not $savedRuntime) {
        $missing = @(Find-MissingTools)
        if ($missing.Count -gt 0) {
            $noInstall = ($args -contains '--dry-run') -or ($args -contains '--help') -or ($args -contains '-h')
            $acceptInstall = $setupRequested -and ($args -contains '--yes')
            $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
            if (-not $noInstall -and ($interactive -or $acceptInstall)) {
                $winget = Get-Command 'winget' -CommandType Application -ErrorAction SilentlyContinue
                if ($winget) {
                    Write-Host 'uv installs Python and AutoDJ packages. FFmpeg reads MP3/M4A audio and provides streaming.'
                    $answer = if ($acceptInstall) { 'yes' } else {
                        Read-Host ("May AutoDJ download and install missing tools ({0}) for your Windows account using WinGet? [y/N]" -f ($missing -join ', '))
                    }
                    if ($answer -and $answer.Trim() -match '^(y|yes)$') {
                        $ids = @{ uv = 'astral-sh.uv'; ffmpeg = 'Gyan.FFmpeg' }
                        foreach ($tool in $missing) {
                            & $winget.Source install --id $ids[$tool] --exact --source winget --scope user --accept-source-agreements --accept-package-agreements
                            if ($LASTEXITCODE -ne 0) {
                                if ($tool -eq 'uv') {
                                    throw "winget could not install uv (exit code $LASTEXITCODE). Install it manually:`n$(Get-ManualInstallMessage @('uv'))"
                                }
                            }
                        }
                        Update-ProcessPath
                        $missing = @(Find-MissingTools)
                        if ($missing -contains 'uv') {
                            throw "The installation completed, but uv is still not available on PATH.`n$(Get-ManualInstallMessage @('uv'))"
                        }
                        if ($missing -contains 'ffmpeg') {
                            Warn-MissingFfmpeg
                        }
                    } else {
                        if ($missing -contains 'uv') {
                            throw "Tool installation was declined.`n$(Get-ManualInstallMessage @('uv'))"
                        }
                        Warn-MissingFfmpeg
                    }
                } else {
                    if ($missing -contains 'uv') {
                        throw "winget is unavailable.`n$(Get-ManualInstallMessage @('uv'))"
                    }
                    Warn-MissingFfmpeg
                }
            } elseif (-not $noInstall) {
                if ($missing -contains 'uv') {
                    throw "uv is missing. Open AutoDJ in a terminal to approve installation, or install uv yourself:`n$(Get-ManualInstallMessage @('uv'))"
                }
                Warn-MissingFfmpeg
            } elseif (-not (Get-Command 'uv' -CommandType Application -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
                throw "uv is required to start AutoDJ on a fresh checkout.`n$(Get-ManualInstallMessage @('uv'))"
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
