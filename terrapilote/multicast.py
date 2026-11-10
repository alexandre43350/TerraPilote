"""Déploiement multicast avec udpcast.

L'émetteur envoie l'image sous forme d'un unique flux tar (``udp-sender``).
Chaque récepteur (``udp-receiver``) lit ce flux et restaure les partitions au
fil de l'eau : rien n'est stocké localement, et une seule transmission sert
tous les PC, quel que soit leur nombre.
"""

import os
import subprocess

from terrapilote import disks as dk
from terrapilote import image as im
from terrapilote import ops
from terrapilote.errors import CommandError, TerraPiloteError

DEFAULT_PORTBASE = 9000


def sender_cmd(receivers, interface=None, portbase=DEFAULT_PORTBASE, max_wait=600, max_bitrate=None):
    cmd = ["udp-sender", "--nokbd", "--portbase", str(portbase),
           "--min-receivers", str(receivers), "--max-wait", str(max_wait)]
    if interface:
        cmd += ["--interface", interface]
    if max_bitrate:
        cmd += ["--max-bitrate", max_bitrate]
    return cmd


def receiver_cmd(interface=None, portbase=DEFAULT_PORTBASE):
    cmd = ["udp-receiver", "--nokbd", "--portbase", str(portbase)]
    if interface:
        cmd += ["--interface", interface]
    return cmd


def send(runner, name, receivers, image_dir=None, **opts):
    path = im.resolve(name, image_dir)
    manifest, bad = im.verify(path)
    if bad:
        raise TerraPiloteError("Image corrompue, envoi annulé : " + ", ".join(n for n, _ in bad))
    cmd = sender_cmd(receivers, **opts)
    runner.log(
        f"Envoi multicast de « {manifest['name']} » : attente de {receivers} récepteur(s) "
        f"(démarrage forcé après {opts.get('max_wait', 600)} s s'il y en a au moins un)"
    )
    if runner.dry_run:
        runner.log(f"[dry-run] tar {path} | {' '.join(cmd)}")
        return manifest
    proc = runner.popen(cmd, stdin=subprocess.PIPE)
    try:
        im.write_tar_stream(path, manifest, proc.stdin)
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait()
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode)
    runner.log("Envoi multicast terminé")
    return manifest


def receive(runner, disk_path, new_uuids=False, **opts):
    """Reçoit et restaure une image.

    La confirmation doit être demandée AVANT l'appel : une fois la
    transmission lancée, l'émetteur avance au rythme du récepteur le plus lent,
    une question en suspens bloquerait tous les PC.
    """
    cmd = receiver_cmd(**opts)
    if runner.dry_run:
        runner.log(f"[dry-run] {' '.join(cmd)} | restauration sur {disk_path}")
        return None
    # Vérifie le disque cible avant d'attendre l'émetteur.
    dk.check_writable_target(runner, dk.get_disk(runner, disk_path))

    runner.log(f"En attente de l'émetteur multicast (port {opts.get('portbase', DEFAULT_PORTBASE)})…")
    proc = runner.popen(cmd, stdout=subprocess.PIPE)
    source = None
    try:
        source = im.TarStreamSource(proc.stdout)
        manifest = ops.deploy(runner, source, disk_path, new_uuids=new_uuids)
        while proc.stdout.read(1024 * 1024):  # fin du flux (bourrage tar)
            pass
    except BaseException:
        proc.kill()
        raise
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                pass
        proc.stdout.close()
        proc.wait()
    return manifest


def default_interface():
    """Première interface réseau active autre que lo."""
    base = "/sys/class/net"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for name in names:
        if name == "lo":
            continue
        try:
            with open(os.path.join(base, name, "operstate")) as f:
                if f.read().strip() == "up":
                    return name
        except OSError:
            continue
    return None
