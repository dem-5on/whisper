param(
    [switch] $RemoveUserData
)

$ErrorActionPreference = 'Stop'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\Whisper'
$ConfigRoot = Join-Path $env:APPDATA 'Whisper'
$DataRoot = Join-Path $env:LOCALAPPDATA 'Whisper'
$ShortcutPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Whisper.lnk'
$WhisperCommand = Join-Path $InstallRoot 'venv\Scripts\whisper.exe'

if (Test-Path $WhisperCommand) {
    & $WhisperCommand shutdown *> $null
    & $WhisperCommand autostart disable *> $null
    $SocketPath = Join-Path $env:TEMP 'whisper-daemon.sock'
    for ($Attempt = 0; $Attempt -lt 20 -and (Test-Path $SocketPath); $Attempt++) {
        Start-Sleep -Milliseconds 250
    }
}
else {
    & schtasks.exe /Delete /F /TN WhisperTranscriber *> $null
}

if (Test-Path $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath -Force
}
if (Test-Path $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}

if ($RemoveUserData) {
    foreach ($Path in @($ConfigRoot, $DataRoot)) {
        if (Test-Path $Path) {
            Remove-Item -LiteralPath $Path -Recurse -Force
        }
    }
    Write-Host 'Whisper was removed, including its settings, API keys, recordings, and model files.' -ForegroundColor Yellow
}
else {
    Write-Host 'Whisper was removed. Your settings, API keys, recordings, and model files were kept.' -ForegroundColor Green
    Write-Host 'To remove those too, run this script again with -RemoveUserData.'
}
