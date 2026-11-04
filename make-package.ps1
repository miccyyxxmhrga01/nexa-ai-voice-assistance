# Builds what you share with other Windows PCs, in dist\:
#   NexaSetup.exe  the installer (recommended): double-click it, press Install, done
#   Nexa.zip       the same app as a plain folder: unzip it and double-click start.bat
# Neither holds anything personal: no API keys, chats, logs, downloaded models or .venv.
#
#   powershell -ExecutionPolicy Bypass -File make-package.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$files = "server.py", "owner_voice.py", "nexa_config.example.py", "requirements.txt", "setup.ps1", "start.bat", "README.md", "static"
$stage = Join-Path $PSScriptRoot "dist\Nexa"
$zip = Join-Path $PSScriptRoot "dist\Nexa.zip"
$payload = Join-Path $PSScriptRoot "dist\payload.zip"
$setupExe = Join-Path $PSScriptRoot "dist\NexaSetup.exe"

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
foreach ($f in $files) { Copy-Item $f -Destination $stage -Recurse }
Get-ChildItem $stage -Recurse -Force | Where-Object { $_.Name -eq "__pycache__" -or $_.Extension -eq ".pyc" } |
    Remove-Item -Recurse -Force

# Keys live in %LOCALAPPDATA%\Nexa, never in the project, but check anyway before sharing.
$leak = Get-ChildItem $stage -Recurse -File |
    Select-String -Pattern "AIza[0-9A-Za-z_\-]{30,}", "sk-ant-[0-9A-Za-z_\-]{20,}", "gsk_[0-9A-Za-z]{20,}" -List
if ($leak) {
    Write-Host "Stopped: something that looks like an API key is in $($leak[0].Path). Remove it and try again." -ForegroundColor Red
    exit 1
}

# The zip: an "Nexa" folder to unzip anywhere.
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $stage -DestinationPath $zip

# The installer: the same files, plus the uninstaller, packed into one .exe built with the
# C# compiler that comes with Windows.
Copy-Item installer\uninstall.ps1 $stage
if (Test-Path $payload) { Remove-Item -Force $payload }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $payload
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
& $csc /nologo /target:winexe /optimize+ "/out:$setupExe" /win32icon:static\nexa.ico /win32manifest:installer\app.manifest `
    "/resource:$payload,payload.zip" "/resource:static\nexa-mark.png,logo.png" `
    /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll `
    /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll installer\NexaSetup.cs
if ($LASTEXITCODE -ne 0) { Write-Host "Building the installer failed (see the messages above)." -ForegroundColor Red; exit 1 }
Remove-Item -Force $payload
Remove-Item -Recurse -Force $stage

$exeKb = [math]::Round((Get-Item $setupExe).Length / 1KB)
$zipKb = [math]::Round((Get-Item $zip).Length / 1KB)
Write-Host "Built $setupExe ($exeKb KB) and $zip ($zipKb KB)." -ForegroundColor Green
Write-Host "Share NexaSetup.exe: on the other PC, double-click it and press Install."
