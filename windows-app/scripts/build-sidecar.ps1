$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AppRoot = Join-Path $RepoRoot 'windows-app'
$BinaryRoot = Join-Path $AppRoot 'src-tauri\binaries'
$BuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "whisper-sidecar-$PID"
$VenvRoot = Join-Path $BuildRoot 'venv'
$BuildPython = Join-Path $VenvRoot 'Scripts\python.exe'
$DistRoot = Join-Path $BuildRoot 'dist'
$WorkRoot = Join-Path $BuildRoot 'work'
$SpecRoot = Join-Path $BuildRoot 'spec'
$ProjectSpec = "${RepoRoot}[windows,local]"
$LocationPushed = $false

function Invoke-Checked([string] $Program, [string[]] $Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Program"
    }
}

try {
    Push-Location $RepoRoot
    $LocationPushed = $true
    New-Item -ItemType Directory -Force $BuildRoot, $BinaryRoot, $DistRoot, $WorkRoot, $SpecRoot | Out-Null
    Invoke-Checked 'python' @('-m', 'venv', $VenvRoot)
    Invoke-Checked $BuildPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', $ProjectSpec, 'pyinstaller')
    Invoke-Checked $BuildPython @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'whisper-daemon',
        '--distpath', $DistRoot,
        '--workpath', $WorkRoot,
        '--specpath', $SpecRoot,
        '--collect-all', 'faster_whisper',
        '--collect-all', 'ctranslate2',
        '--collect-all', 'av',
        '--collect-all', 'websockets',
        '--collect-all', 'numpy',
        '--collect-all', 'sounddevice',
        '--collect-all', 'yaml',
        '--paths', '.',
        '--hidden-import', 'transcriber.platforms.windows.websocket',
        '--hidden-import', 'transcriber.platforms.windows.ipc',
        '--hidden-import', 'transcriber.platforms.windows.audio',
        'windows-app/sidecar_entry.py'
    )

    $BuiltSidecar = Join-Path $DistRoot 'whisper-daemon.exe'
    if (-not (Test-Path -LiteralPath $BuiltSidecar -PathType Leaf)) {
        throw 'PyInstaller did not produce whisper-daemon.exe.'
    }

    $TargetSidecar = Join-Path $BinaryRoot 'whisper-daemon-x86_64-pc-windows-msvc.exe'
    Move-Item -LiteralPath $BuiltSidecar -Destination $TargetSidecar -Force
    $Sidecar = Get-Item -LiteralPath $TargetSidecar
    if ($Sidecar.Length -lt 10MB) {
        throw "The packaged Whisper daemon sidecar is unexpectedly small ($($Sidecar.Length) bytes)."
    }
    Write-Host "Built $($Sidecar.Name) ($($Sidecar.Length) bytes)."
}
finally {
    if ($LocationPushed) {
        Pop-Location
    }
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
}
