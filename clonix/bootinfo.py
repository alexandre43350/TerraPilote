"""Durée du démarrage de Clonix, mesurée sur le PC.

- ``systemd-analyze`` : temps du micrologiciel (si connu), du noyau Linux et du
  démarrage des services ;
- le chargement en mémoire (option toram) se lit dans le journal du noyau :
  entre le montage du fichier ISO (« ISO 9660 Extensions ») et celui du système
  compressé (« squashfs »), live-boot recopie la clé en mémoire ;
- l'écran Clonix est prêt quand le navigateur demande l'état pour la première
  fois : on note alors le temps écoulé depuis le démarrage de Linux.
"""

import os
import re

LIVE_MEDIUM = "/run/live/medium"

_PART = re.compile(r"((?:\d+min\s*)?[\d.]+(?:ms|s|min)) \((firmware|loader|kernel|initrd|userspace)\)")
_DMESG_TIME = re.compile(r"^\[\s*([\d.]+)\]\s*(.*)$")


def parse_duration(text):
    """« 1min 2.5s », « 345ms », « 3.2s » → secondes."""
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)\s*(min|ms|s)", text):
        total += float(value) * {"min": 60, "s": 1, "ms": 0.001}[unit]
    return total


def parse_systemd_analyze(text):
    """{"firmware": s, "loader": s, "kernel": s, "initrd": s, "userspace": s} (parties présentes)."""
    out = {}
    for value, part in _PART.findall(text or ""):
        out[part] = parse_duration(value)
    return out


def toram_seconds(dmesg):
    """Durée de la copie de la clé en mémoire d'après le journal du noyau, ou None."""
    start = None
    for line in (dmesg or "").splitlines():
        m = _DMESG_TIME.match(line.strip())
        if not m:
            continue
        t, msg = float(m.group(1)), m.group(2)
        if start is None and "ISO 9660 Extensions" in msg:
            start = t
        elif start is not None and ("squashfs" in msg.lower() or "loop0: detected capacity" in msg):
            return max(t - start, 0.0)
    return None


def uptime(proc="/proc/uptime"):
    try:
        with open(proc) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def ram_copy_bytes(path=LIVE_MEDIUM):
    """Taille de la copie en mémoire (tmpfs), ou None si Clonix tourne depuis sa clé."""
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def gather(runner, screen_ready=None):
    """Mesures du démarrage. ``screen_ready`` : secondes depuis Linux à l'affichage de l'écran."""
    from clonix import disks as dk
    info = {"screen": screen_ready, "in_ram": None, "ram_bytes": None, "toram": None}
    if os.path.isdir(LIVE_MEDIUM):
        info["in_ram"] = dk.fstype_of_mount(runner, LIVE_MEDIUM) == "tmpfs"
        if info["in_ram"]:
            info["ram_bytes"] = ram_copy_bytes()
            try:
                info["toram"] = toram_seconds(runner.run(["dmesg"], readonly=True, check=False))
            except Exception:
                pass
    try:
        parts = parse_systemd_analyze(runner.run(["systemd-analyze"], readonly=True, check=False))
    except Exception:
        parts = {}
    info.update({k: parts.get(k) for k in ("firmware", "loader", "kernel", "initrd", "userspace")})
    return info
