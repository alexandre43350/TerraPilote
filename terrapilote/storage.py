"""Montage du dépôt d'images : disque local, partage Windows/Samba, NFS ou SSH."""

import os
import tempfile

from terrapilote import image as im
from terrapilote.errors import TerraPiloteError


def _prepare(runner, mountpoint):
    if os.path.ismount(mountpoint):
        raise TerraPiloteError(f"{mountpoint} est déjà monté (terrapilote umount pour le libérer)")
    if not runner.dry_run:
        os.makedirs(mountpoint, exist_ok=True)


def mount_local(runner, device, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    runner.run(["mount", device, mountpoint])


def mount_smb(runner, share, user=None, password=None, domain=None, mountpoint=im.DEFAULT_DIR):
    _prepare(runner, mountpoint)
    opts = ["iocharset=utf8"]
    cred = None
    if user:
        # Fichier d'identifiants temporaire : le mot de passe n'apparaît pas
        # dans la liste des processus.
        fd, cred = tempfile.mkstemp(prefix="tp-cred-", dir="/run" if os.path.isdir("/run") else None)
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
        raise TerraPiloteError(f"{mountpoint} n'est pas monté")
    runner.run(["umount", mountpoint])
