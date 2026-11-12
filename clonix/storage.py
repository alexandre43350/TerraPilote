"""Montage du dépôt d'images : espace sur la clé Clonix elle-même, disque
local, partage Windows/Samba, NFS ou SSH."""

import json
import os
import tempfile
from types import SimpleNamespace

from clonix import disks as dk
from clonix import image as im
from clonix.errors import ClonixError, CommandError

# Les images peuvent être stockées sur la clé de démarrage elle-même :
# - dans une 2e partition nommée CLONIX-DATA (clé écrite avec Rufus) ;
# - dans la partition Ventoy, quand Clonix démarre via Ventoy (comme AOMEI).
CLONIX_DATA_LABEL = "CLONIX-DATA"
VENTOY_LABEL = "Ventoy"
WRITABLE_FS = {"exfat", "ntfs", "vfat", "ext2", "ext3", "ext4", "btrfs", "xfs"}
VENTOY_HELP = (
    "La partition Ventoy est verrouillée par Ventoy pendant que Clonix tourne. "
    "Activez l'option VTOY_LINUX_REMOUNT : copiez le fichier ventoy.json fourni "
    "dans le dossier « ventoy » de la clé (voir le README), puis redémarrez."
)


def _is_busy(exc):
    msg = (exc.stderr or "").lower()
    return "busy" in msg or "already mounted" in msg


def _prepare(runner, mountpoint):
    if os.path.ismount(mountpoint):
        raise ClonixError(f"{mountpoint} est déjà monté (clonix umount pour le libérer)")
    if not runner.dry_run:
        os.makedirs(mountpoint, exist_ok=True)


def mount_local(runner, device, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    try:
        runner.run(["mount", device, mountpoint])
        dk.tune_readahead(runner, mountpoint)
    except CommandError as exc:
        if _is_busy(exc):
            raise ClonixError(
                f"{device} est déjà utilisé par la clé de démarrage. " + VENTOY_HELP
            ) from exc
        raise


def mount_smb(runner, share, user=None, password=None, domain=None, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    opts = ["iocharset=utf8"]
    cred = None
    if user:
        # Fichier d'identifiants temporaire : le mot de passe n'apparaît pas
        # dans la liste des processus.
        fd, cred = tempfile.mkstemp(prefix="clonix-cred-", dir="/run" if os.path.isdir("/run") else None)
        with os.fdopen(fd, "w") as f:
            f.write(f"username={user}\npassword={password or ''}\n")
            if domain:
                f.write(f"domain={domain}\n")
        opts.append(f"credentials={cred}")
    else:
        opts.append("guest")
    try:
        runner.run(["mount", "-t", "cifs", share, mountpoint, "-o", ",".join(opts)])
    finally:
        if cred:
            os.unlink(cred)


def mount_nfs(runner, export, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    runner.run(["mount", "-t", "nfs", export, mountpoint])


def mount_sshfs(runner, remote, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    # Interactif : sshfs demande le mot de passe dans le terminal.
    runner.run(["sshfs", remote, mountpoint, "-o", "reconnect,ServerAliveInterval=15"], capture=False)


def umount(runner, mountpoint=im.DEFAULT_DIR):
    if not os.path.ismount(mountpoint):
        raise ClonixError(f"{mountpoint} n'est pas monté")
    runner.run(["umount", mountpoint])


def _walk(nodes):
    for node in nodes or []:
        yield node
        yield from _walk(node.get("children"))


def _storage_candidates(runner):
    """Périphériques où ranger les images sur la clé de démarrage, par priorité.

    1. CLONIX-DATA ; 2. la partition Ventoy (exFAT/NTFS).
    Pour un même nom, les périphériques /dev/mapper passent avant la partition :
    avec l'option Ventoy VTOY_LINUX_REMOUNT, la partition elle-même reste
    occupée mais une copie montable apparaît sous /dev/mapper.
    """
    out = runner.run(["lsblk", "-J", "-o", "PATH,TYPE,FSTYPE,LABEL"], readonly=True, check=False)
    try:
        nodes = list(_walk(json.loads(out or "{}").get("blockdevices")))
    except ValueError:
        return []
    found = []
    for label in (CLONIX_DATA_LABEL, VENTOY_LABEL):
        matches = [n for n in nodes if (n.get("label") or "") == label
                   and (n.get("fstype") or "") in WRITABLE_FS]
        matches.sort(key=lambda n: 0 if n.get("type") == "dm" else 1)
        for n in matches:
            path = n.get("path") or "/dev/" + (n.get("name") or "")
            if all(c.path != path for c in found):
                found.append(SimpleNamespace(path=path, label=label, fstype=n.get("fstype") or ""))
    return found


def find_data_partition(runner):
    """Espace de stockage sur la clé de démarrage (CLONIX-DATA ou Ventoy), ou None."""
    candidates = _storage_candidates(runner)
    return candidates[0] if candidates else None


def mount_data_partition(runner, mountpoint=im.DEFAULT_DIR):
    """Monte l'espace de stockage de la clé de démarrage comme dépôt d'images."""
    candidates = _storage_candidates(runner)
    if not candidates:
        raise ClonixError(
            "Aucun espace de stockage sur la clé : créez une 2e partition nommée "
            "CLONIX-DATA, ou démarrez Clonix avec Ventoy (voir le README)."
        )
    _prepare(runner, mountpoint)
    ventoy_locked = False
    for cand in candidates:
        try:
            runner.run(["mount", cand.path, mountpoint])
            dk.tune_readahead(runner, mountpoint)
            return cand.path
        except CommandError as exc:
            if not _is_busy(exc):
                raise
            ventoy_locked = ventoy_locked or cand.label == VENTOY_LABEL
    if ventoy_locked:
        raise ClonixError(VENTOY_HELP)
    raise ClonixError("L'espace de stockage de la clé est déjà utilisé et ne peut pas être monté.")


def auto_mount_data(runner, mountpoint=im.DEFAULT_DIR):
    """Monte l'espace de la clé s'il existe et qu'aucun dépôt n'est monté.

    Best-effort : appelée au démarrage pour que le dépôt soit prêt sans action
    de l'utilisateur. N'échoue jamais.
    """
    try:
        if os.path.ismount(mountpoint) or not find_data_partition(runner):
            return False
        mount_data_partition(runner, mountpoint)
        return True
    except (ClonixError, OSError):
        return False
