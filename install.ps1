param()

$ErrorActionPreference = 'Stop'
$RepositoryArchive = 'https://github.com/dem-5on/whisper/archive/refs/heads/main.zip'
if (-not $env:LOCALAPPDATA -or -not $env:APPDATA) {
    throw 'Could not locate your Windows user folders.'
}
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\Whisper'
$ConfigRoot = Join-Path $env:APPDATA 'Whisper'
$DataRoot = Join-Path $env:LOCALAPPDATA 'Whisper'
$ShortcutPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Whisper.lnk'
$TemporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())

function Write-Status([string] $Message) {
    Write-Host "`nWhisper: $Message" -ForegroundColor Cyan
}

function Invoke-Quiet([string] $Program, [string[]] $Arguments) {
    $LogPath = Join-Path $TemporaryRoot 'install.log'
    & $Program @Arguments *> $LogPath
    if ($LASTEXITCODE -ne 0) {
        Get-Content $LogPath -Tail 40 | Write-Host -ForegroundColor Red
        throw "Command failed ($LASTEXITCODE): $Program"
    }
}

try {
    if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
        throw 'Python 3.11 or newer is required. Install Python from https://www.python.org/downloads/windows/ (include the Python Launcher), then run this installer again.'
    }

    Write-Status 'Checking Python...'
    & py.exe -3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/windows/ and run this installer again.'
    }

    New-Item -ItemType Directory -Path $TemporaryRoot -Force | Out-Null
    Write-Status 'Downloading Whisper...'
    $ArchivePath = Join-Path $TemporaryRoot 'whisper.zip'
    Invoke-WebRequest -Uri $RepositoryArchive -OutFile $ArchivePath
    Expand-Archive -Path $ArchivePath -DestinationPath $TemporaryRoot -Force
    $SourceRoot = Get-ChildItem -Path $TemporaryRoot -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } | Select-Object -First 1
    if (-not $SourceRoot) {
        throw 'The Whisper source archive did not contain the expected project files.'
    }

    Write-Status 'Preparing your private app environment...'
    New-Item -ItemType Directory -Path $InstallRoot, $ConfigRoot, $DataRoot -Force | Out-Null
    $Python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
    $PythonWindowed = Join-Path $InstallRoot 'venv\Scripts\pythonw.exe'
    if (-not (Test-Path $Python)) {
        Invoke-Quiet 'py.exe' @('-3', '-m', 'venv', (Join-Path $InstallRoot 'venv'))
    }
    if (-not (Test-Path $PythonWindowed)) {
        throw 'The Python installation did not provide pythonw.exe, which Whisper needs for its tray app.'
    }

    Write-Status 'Installing Whisper and its Windows audio/transcription components...'
    Invoke-Quiet $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', 'pip')
    $PackagePath = $SourceRoot.FullName + '[windows,local]'
    Invoke-Quiet $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', $PackagePath)

    if (-not (Test-Path (Join-Path $ConfigRoot 'config.yaml'))) {
        Copy-Item (Join-Path $SourceRoot.FullName 'config.example.yaml') (Join-Path $ConfigRoot 'config.yaml')
    }
    Copy-Item (Join-Path $SourceRoot.FullName 'uninstall.ps1') (Join-Path $InstallRoot 'uninstall.ps1') -Force

    Write-Status 'Adding Whisper to your Start menu...'
    New-Item -ItemType Directory -Path (Split-Path $ShortcutPath) -Force | Out-Null
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PythonWindowed
    $Shortcut.Arguments = '-m transcriber.platforms.windows.tray'
    $Shortcut.WorkingDirectory = $InstallRoot
    $Shortcut.Description = 'Whisper voice transcription'
    $Shortcut.Save()

    Write-Status 'Setting Whisper to start when you sign in...'
    Invoke-Quiet (Join-Path $InstallRoot 'venv\Scripts\whisper.exe') @('autostart', 'enable')
    Write-Status 'Starting Whisper...'
    Start-Process -FilePath $PythonWindowed -ArgumentList @('-m', 'transcriber.platforms.windows.tray') -WorkingDirectory $InstallRoot -WindowStyle Hidden

    Write-Host "`nWhisper is installed. Open it from the Start menu or press Ctrl+Alt+R to record." -ForegroundColor Green
    Write-Host 'The first local transcription downloads the speech model. Use the tray menu to cancel or quit.'
}
catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
