# Construit la cle USB Clonix depuis Windows (PowerShell + Docker Desktop).
#
#   powershell -ExecutionPolicy Bypass -File live\build.ps1
#
# Resultat : live\out\clonix-AAAAMMJJ.iso
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker ne repond pas : lancez Docker Desktop et attendez qu'il soit pret." -ForegroundColor Red
    exit 1
}

docker run --rm --privileged --platform linux/amd64 -e CLONIX_IN_CONTAINER=1 `
    -v "${Root}:/src" debian:trixie sh /src/live/build.sh
exit $LASTEXITCODE
