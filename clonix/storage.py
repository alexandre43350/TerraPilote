"""Montage du dépôt d'images : espace sur la clé Clonix elle-même, disque
local, partage Windows/Samba, NFS ou SSH."""

import os
import tempfile

from clonix import disks as dk
from clonix import image as im
from clonix.errors import ClonixError, CommandError

# Les images peuvent être stockées sur la clé Clonix, dans une 2e partition
# (modifiable) nommée ainsi. La partie « système » de la clé reste en lecture
# seule ; seule cette partition de données est utilisée pour écrire.
CLONIX_DATA_LABEL = "CLONIX-DATA"


def _prepare(runner, mountpoint):
    if os.path.ismount(mountpoint):
        raise ClonixError(f"{mountpoint} est déjà monté (clonix umount pour le libérer)")
    if not runner.dry_run:
        os.makedirs(mountpoint, exist_ok=True)


def mount_local(runner, device, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    try:
        runner.run(["mount", device, mountpoint])
    except CommandError as exc:
        msg = (exc.stderr or "").lower()
        if "busy" in msg or "already mounted" in msg:
            raise ClonixError(
                f"{device} est déjà utilisé par la clé de démarrage (Ventoy ?) et ne peut "
                "pas servir de dépôt. Branchez une 2e clé USB, ou créez une partition "
                "dédiée nommée CLONIX-DATA (voir le README)."
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


def find_data_partition(runner):
    """Partition « CLONIX-DATA » (sur la clé Clonix ou une autre), ou None.

    On cherche sur tous les disques : avec Ventoy, la clé de démarrage n'est pas
    reconnue comme « medium live » (elle apparaît via un périphérique loop), donc
    on se fie uniquement au nom CLONIX-DATA, assez spécifique.
    """
    for d in dk.list_disks(runner):
        for p in d.partitions:
            if p.label == CLONIX_DATA_LABEL:
                return p
    return None


def mount_data_partition(runner, mountpoint=im.DEFAULT_DIR):
    """Monte l'espace « CLONIX-DATA » de la clé comme dépôt d'images."""
    part = find_data_partition(runner)
    if not part:
        raise ClonixError(
            "Aucun espace « CLONIX-DATA » sur la clé. Créez une 2e partition "
            "nommée CLONIX-DATA sur la clé (voir le README), puis réessayez."
        )
    _prepare(runner, mountpoint)
    runner.run(["mount", part.path, mountpoint])
    return part.path


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
