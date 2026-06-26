$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = Join-Path $env:TEMP 'poppler.zip'
$url = 'https://github.com/oschwartz10612/poppler-windows/releases/download/v26.02.0-0/Release-26.02.0-0.zip'
Write-Output "Downloading Poppler from $url"
Invoke-WebRequest -Uri $url -OutFile $zip
$dest = 'C:\tools\poppler'
if (-not (Test-Path $dest)) {
    try {
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
    } catch {
        $dest = Join-Path $env:LOCALAPPDATA 'Programs\poppler'
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
    }
}
if (Test-Path $dest) {
    Remove-Item -LiteralPath (Join-Path $dest '*') -Recurse -Force -ErrorAction SilentlyContinue
}
Expand-Archive -Path $zip -DestinationPath $dest -Force
$bin = Get-ChildItem -Path $dest -Recurse -Filter pdfinfo.exe | Where-Object { -not $_.PSIsContainer } | Select-Object -First 1
if (-not $bin) { throw 'pdfinfo.exe not found after extraction' }
Write-Output "Found binary: $($bin.FullName)"
$folder = $bin.DirectoryName
$current = [Environment]::GetEnvironmentVariable('Path','User')
if (-not $current) { $current = '' }
if ($current -notlike "*$folder*") {
    [Environment]::SetEnvironmentVariable('Path', "$current;$folder", 'User')
    Write-Output "User PATH updated with $folder"
} else {
    Write-Output "User PATH already contains $folder"
}
$env:Path = "$env:Path;$folder"
Write-Output 'pdfinfo version:'
& $bin.FullName -v
