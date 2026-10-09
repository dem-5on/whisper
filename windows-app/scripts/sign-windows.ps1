param(
    [Parameter(Mandatory = $true)]
    [string] $FilePath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw 'Tauri asked Whisper to sign a file that does not exist.'
}
if (-not $env:WHISPER_SIGNING_THUMBPRINT) {
    throw 'Windows signing was requested, but no signing certificate was imported.'
}

$sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
$signTool = Get-ChildItem -Path $sdkRoot -Filter signtool.exe -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
    Sort-Object -Property FullName -Descending |
    Select-Object -First 1
if (-not $signTool) {
    throw 'Could not find the Windows SDK signtool.exe.'
}

& $signTool.FullName sign /fd SHA256 /sha1 $env:WHISPER_SIGNING_THUMBPRINT /s My `
    /tr 'https://timestamp.digicert.com' /td SHA256 $FilePath
if ($LASTEXITCODE -ne 0) {
    throw "Windows code signing failed with exit code $LASTEXITCODE."
}
