#!/bin/sh
# Construit l'image ISO hybride de la clé USB TerraPilote (BIOS + UEFI).
#
#   ./live/build.sh            (nécessite Docker, accès Internet, ~4 Go libres)
#
# Fonctionne sous Linux, macOS (Docker Desktop, y compris puces Apple M1/M2/M3/M4)
# et Windows (Docker Desktop : lancer plutôt live\build.ps1 depuis PowerShell).
# La clé produite est toujours pour PC x86-64 (amd64).
#
# Résultat : live/out/terrapilote-AAAAMMJJ.iso, à copier sur une clé avec
# Balena Etcher, Rufus (mode DD) ou :  dd if=terrapilote.iso of=/dev/sdX bs=4M
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")

if [ "${TP_IN_CONTAINER:-0}" != 1 ]; then
    # --platform : sur Mac Apple Silicon, Docker construirait sinon pour ARM.
    exec docker run --rm --privileged --platform linux/amd64 -e TP_IN_CONTAINER=1 \
        -v "$ROOT":/src debian:bookworm sh /src/live/build.sh
fi

apt-get update
apt-get install -y --no-install-recommends live-build ca-certificates

WORK=/tmp/terrapilote-live
rm -rf "$WORK" && mkdir -p "$WORK" && cd "$WORK"

lb config \
    --distribution bookworm \
    --architecture amd64 \
    --archive-areas "main contrib non-free non-free-firmware" \
    --binary-images iso-hybrid \
    --debian-installer none \
    --memtest none \
    --iso-application TerraPilote \
    --iso-volume TERRAPILOTE \
    --bootappend-live "boot=live components quiet splash locales=fr_FR.UTF-8 keyboard-layouts=fr timezone=Europe/Paris live-config.noautologin"

cp -a /src/live/config/. config/
# Les droits d'exécution peuvent manquer si le dépôt vient de Windows.
chmod 755 config/hooks/live/*.hook.chroot config/includes.chroot/usr/local/bin/*
mkdir -p config/includes.chroot/opt/terrapilote
cp -a /src/terrapilote config/includes.chroot/opt/terrapilote/
find config/includes.chroot/opt -name __pycache__ -prune -exec rm -rf {} +

lb build

mkdir -p /src/live/out
cp ./*.iso "/src/live/out/terrapilote-$(date +%Y%m%d).iso"
echo "ISO prête : live/out/terrapilote-$(date +%Y%m%d).iso"
