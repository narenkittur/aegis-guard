$ErrorActionPreference = 'Stop'
$zip = Join-Path $env:TEMP 'poppler.zip'
if (-not (Test-Path $zip)) { throw 'poppler.zip not found in TEMP' }
$dest = 'C:\Program Files\poppler'
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
Expand-Archive -Path $zip -DestinationPath $dest -Force
$extracted = Get-ChildItem -Path $dest -Directory | Select-Object -First 1
if (-not $extracted) { throw 'No extracted directory found in C:\Program Files\poppler' }
$desired = Join-Path $dest 'Release-24.08.0-0'
if (Test-Path $desired) { Remove-Item -LiteralPath $desired -Recurse -Force }
New-Item -ItemType Directory -Path $desired -Force | Out-Null
Copy-Item -Path (Join-Path $extracted.FullName '*') -Destination $desired -Recurse -Force
$bin = Get-ChildItem -Path $desired -Recurse -Filter pdfinfo.exe | Where-Object { -not $_.PSIsContainer } | Select-Object -First 1
if (-not $bin) { throw 'pdfinfo.exe not found in created release folder' }
$machinePath = [Environment]::GetEnvironmentVariable('Path','Machine')
$newPath = if (-not $machinePath) { 'C:\Program Files\poppler\Release-24.08.0-0\Library\bin' } else { $machinePath + ';C:\Program Files\poppler\Release-24.08.0-0\Library\bin' }
[Environment]::SetEnvironmentVariable('Path', $newPath, 'Machine')
Write-Output "ELEVATED_DONE"
Write-Output "BIN=$($bin.FullName)"
"PATH_UPDATED=C:\Program Files\poppler\Release-24.08.0-0\Library\bin"