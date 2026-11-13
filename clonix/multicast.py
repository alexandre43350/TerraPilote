"""Déploiement multicast avec udpcast.

L'émetteur envoie l'image sous forme d'un unique flux tar (``udp-sender``).
Chaque récepteur (``udp-receiver``) lit ce flux et restaure les partitions au
fil de l'eau : rien n'est stocké localement, et une seule transmission sert
tous les PC, quel que soit leur nombre.
"""

import os
import subprocess
import time

from clonix import disks as dk
from clonix import image as im
from clonix import ops
from clonix.errors import CommandError, ClonixError

DEFAULT_PORTBASE = 9000


def sender_cmd(receivers, interface=None, portbase=DEFAULT_PORTBASE, max_wait=600, max_bitrate=None,
               full_duplex=True):
    cmd = ["udp-sender", "--nokbd", "--portbase", str(portbase),
           "--min-receivers", str(receivers), "--max-wait", str(max_wait)]
    if full_duplex:
        cmd.append("--full-duplex")  # réseaux commutés (switch) : pas d'attente inutile
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
    runner.log("Vérification de l'image avant l'envoi…")
    dk.tune_readahead(runner, os.path.dirname(path))
    runner.plan([("Vérification de l'image", 1), ("Diffusion", 1)])
    runner.start_step(0)
    manifest, bad = im.verify(path, progress=runner.byte_progress())
    if bad:
        raise ClonixError("Image corrompue, envoi annulé : " + ", ".join(n for n, _ in bad))
    cmd = sender_cmd(receivers, **opts)
    runner.log(
        f"Envoi multicast de « {manifest['name']} » : attente de {receivers} récepteur(s) "
        f"(démarrage forcé après {opts.get('max_wait', 600)} s s'il y en a au moins un)"
    )
    if runner.dry_run:
        runner.log(f"[dry-run] tar {path} | {' '.join(cmd)}")
        return manifest
    total = im.image_bytes(manifest)
    runner.plan([("Vérification de l'image", total), (f"Diffusion de « {manifest['name']} »", total)])
    runner.start_step(1)
    proc = runner.popen(cmd, stdin=subprocess.PIPE)
    try:
        im.write_tar_stream(path, manifest, _Counting(proc.stdin, runner, total))
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait()
        runner.release(proc)
    runner.check_cancel()
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode)
    runner.log("Envoi multicast terminé")
    return manifest


def receive(runner, disk_path, new_uuids=False, adapt_windows=False, expand=True, **opts):
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
        manifest = ops.deploy(runner, source, disk_path, new_uuids=new_uuids, adapt_windows=adapt_windows,
                              expand=expand)
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
        runner.release(proc)
    return manifest


class _Counting:
    """Compte les octets envoyés pour la barre de progression de l'émetteur."""

    def __init__(self, raw, runner, total):
        self.raw, self.runner, self.total = raw, runner, max(total, 1)
        self.done = 0
        self.last = 0.0

    def write(self, buf):
        n = self.raw.write(buf)
        self.done += len(buf)
        now = time.monotonic()
        if now - self.last > 0.5:
            self.last = now
            self.runner.emit(min(self.done / self.total, 1.0), done_bytes=self.done)
        return n

    def flush(self):
        self.raw.flush()


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
