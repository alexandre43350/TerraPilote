"""Inventaire des disques et règles de sécurité."""

import json
import os
import re
from dataclasses import dataclass, field

from clonix.errors import ClonixError

LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM,RO,LOG-SEC,PKNAME,PTTYPE"
LIVE_MEDIUM = "/run/live/medium"


@dataclass
class Partition:
    path: str
    number: int
    size: int
    fstype: str = ""
    label: str = ""
    uuid: str = ""


@dataclass
class Disk:
    path: str
    size: int
    model: str = ""
    serial: str = ""
    transport: str = ""
    removable: bool = False
    readonly: bool = False
    sector_size: int = 512
    partitions: list = field(default_factory=list)
    mountpoints: list = field(default_factory=list)
    pttype: str = ""  # « gpt », « dos » (MBR) ou vide

    @property
    def name(self):
        return os.path.basename(self.path)

    def describe(self):
        bits = [self.path, human_size(self.size)]
        if self.model:
            bits.append(self.model)
        if self.transport:
            bits.append(self.transport.upper())
        if self.removable:
            bits.append("amovible")
        return " · ".join(bits)


def human_size(n):
    n = float(n)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if n < 1024 or unit == "Tio":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024


def part_path(disk_path, number):
    """/dev/sda + 1 -> /dev/sda1 ; /dev/nvme0n1 + 1 -> /dev/nvme0n1p1."""
    sep = "p" if disk_path[-1].isdigit() else ""
    return f"{disk_path}{sep}{number}"


def part_number(path):
    m = re.search(r"(\d+)$", path)
    if not m:
        raise ClonixError(f"Numéro de partition introuvable dans {path}")
    return int(m.group(1))


def _bool(v):
    return v in (True, 1, "1", "true")


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _mountpoints(node):
    found = []
    if node.get("mountpoint"):
        found.append(node["mountpoint"])
    for child in node.get("children") or []:
        found.extend(_mountpoints(child))
    return found


def parse_lsblk(text, include_loop=False):
    data = json.loads(text)
    disks = []
    for node in data.get("blockdevices", []):
        kind = node.get("type")
        name = node.get("name", "")
        if kind == "loop" and not include_loop:
            continue
        if kind not in ("disk", "loop") or name.startswith(("zram", "ram")):
            continue
        path = node.get("path") or f"/dev/{name}"
        disk = Disk(
            path=path,
            size=_int(node.get("size")),
            model=(node.get("model") or "").strip(),
            serial=(node.get("serial") or "").strip(),
            transport=node.get("tran") or "",
            removable=_bool(node.get("rm")),
            readonly=_bool(node.get("ro")),
            sector_size=_int(node.get("log-sec"), 512),
            mountpoints=_mountpoints(node),
            pttype=node.get("pttype") or "",
        )
        for child in node.get("children") or []:
            if child.get("type") != "part":
                continue
            cpath = child.get("path") or f"/dev/{child.get('name')}"
            disk.partitions.append(
                Partition(
                    path=cpath,
                    number=part_number(cpath),
                    size=_int(child.get("size")),
                    fstype=child.get("fstype") or "",
                    label=child.get("label") or "",
                    uuid=child.get("uuid") or "",
                )
            )
        disk.partitions.sort(key=lambda p: p.number)
        disks.append(disk)
    return disks


def list_disks(runner):
    include_loop = os.environ.get("CLONIX_ALLOW_LOOP") == "1"
    out = runner.run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS], readonly=True)
    return parse_lsblk(out, include_loop=include_loop)


def get_disk(runner, path):
    path = os.path.realpath(path)
    for disk in list_disks(runner):
        if os.path.realpath(disk.path) == path:
            return disk
    raise ClonixError(f"{path} n'est pas un disque connu (voir `clonix disks`)")


def disk_of_mount(runner, target):
    """Disque physique qui porte le point de montage ``target`` (ou None)."""
    out = runner.run(["findmnt", "-n", "-o", "SOURCE", "--target", target], readonly=True, check=False)
    source = out.strip().splitlines()[0] if out.strip() else ""
    if not source.startswith("/dev/"):
        return None  # partage réseau, tmpfs, overlay…
    source = re.sub(r"\[.*\]$", "", source)  # btrfs : /dev/sda2[/@]
    parent = runner.run(["lsblk", "-n", "-r", "-s", "-o", "PATH,TYPE", source], readonly=True, check=False)
    for line in parent.splitlines():
        bits = line.split()
        if len(bits) == 2 and bits[1] == "disk":
            return bits[0]
    return None


def fstype_of_mount(runner, target):
    out = runner.run(["findmnt", "-n", "-o", "FSTYPE", "--target", target], readonly=True, check=False)
    return out.strip().splitlines()[0] if out.strip() else ""


BOOT_KEY_LABELS = {"VTOYEFI"}  # petite partition EFI de toute clé Ventoy


def is_boot_key(disk):
    """Clé ou SSD de démarrage (Ventoy, ou image Clonix copiée telle quelle).

    Clonix est chargé en mémoire au démarrage et n'utilise plus sa clé : on la
    reconnaît donc à son contenu plutôt qu'à son point de montage.
    """
    for p in disk.partitions:
        if p.label in BOOT_KEY_LABELS:
            return True
        if p.fstype == "iso9660" and (p.label or "").upper().startswith("CLONIX"):
            return True
    return False


def protected_disks(runner, image_dir=None):
    """Disques sur lesquels Clonix ne doit jamais écrire."""
    protected = {}
    disk = disk_of_mount(runner, LIVE_MEDIUM)
    if disk:
        protected[disk] = "la clé USB Clonix"
    if image_dir and os.path.isdir(image_dir):
        disk = disk_of_mount(runner, image_dir)
        if disk:
            protected.setdefault(disk, "le dépôt d'images")
    return protected


def check_writable_target(runner, disk, image_dir=None):
    if disk.readonly:
        raise ClonixError(f"{disk.path} est en lecture seule")
    reason = protected_disks(runner, image_dir).get(disk.path)
    if not reason and is_boot_key(disk):
        reason = "la clé de démarrage (Clonix ou Ventoy)"
    if reason:
        raise ClonixError(f"Refus : {disk.path} héberge {reason}")
    if disk.mountpoints:
        raise ClonixError(
            f"{disk.path} est utilisé ({', '.join(disk.mountpoints)}). Démontez-le d'abord."
        )


def usb_speed(disk_path, sysfs="/sys/block"):
    """Vitesse du lien USB du disque, en Mbit/s (480 = USB 2), ou None si pas USB."""
    try:
        path = os.path.realpath(os.path.join(sysfs, os.path.basename(disk_path), "device"))
    except OSError:
        return None
    while path and path != os.path.dirname(path):
        speed = os.path.join(path, "speed")
        if os.path.isfile(speed) and os.path.isfile(os.path.join(path, "idVendor")):
            try:
                with open(speed) as f:
                    return int(float(f.read().strip()))
            except (OSError, ValueError):
                return None
        path = os.path.dirname(path)
    return None


def usb_label(speed):
    """(nom court, détail) d'une liaison USB d'après sa vitesse en Mbit/s."""
    if speed <= 12:
        return "USB 1", f"{speed} Mbit/s, très lent"
    if speed <= 480:
        return "USB 2", "480 Mbit/s, lent"
    if speed <= 5000:
        return "USB 3", "5 Gbit/s"
    if speed <= 10000:
        return "USB 3.2", "10 Gbit/s"
    return "USB 3.2", f"{speed // 1000} Gbit/s"


def store_link(runner, path, fstype=""):
    """Type de liaison du support des images, pour l'affichage."""
    from clonix.fsclone import NETWORK_FS
    if fstype in NETWORK_FS:
        return {"kind": "network", "label": "Réseau", "detail": fstype.upper(), "slow": False}
    disk = disk_of_mount(runner, path)
    speed = usb_speed(disk) if disk else None
    if speed:
        label, detail = usb_label(speed)
        return {"kind": "usb", "label": label, "detail": detail, "speed": speed, "slow": speed <= 480}
    return {"kind": "internal", "label": "Interne", "detail": "disque du PC", "slow": False}


def check_usb_link(runner, image_dir, sysfs="/sys/block"):
    """Prévient si le dépôt d'images est sur un support branché en USB 2."""
    if not image_dir or not os.path.isdir(image_dir):
        return None
    disk = disk_of_mount(runner, image_dir)
    if not disk:
        return None
    speed = usb_speed(disk, sysfs)
    if speed is not None and speed <= 480:
        runner.warn(
            f"Le dépôt d'images ({disk}) est branché en USB 2 ({speed} Mbit/s) : la copie est "
            "limitée à environ 40 Mo/s. Branchez-le sur un port USB 3 (bleu, ou marqué « SS ») : "
            "c'est 3 à 10 fois plus rapide."
        )
    return speed


READAHEAD_KB = 16 * 1024


def tune_readahead(runner, directory, kb=READAHEAD_KB):
    """Agrandit la lecture anticipée du support qui porte ``directory``.

    Par défaut Linux lit 128 Ko d'avance : un SSD ou une clé USB lus ainsi
    plafonnent souvent à 30-50 Mo/s, alors qu'ils en font plusieurs centaines
    avec de grosses lectures. On règle le périphérique monté et ses parents
    (partition, /dev/mapper de Ventoy, disque).
    """
    if runner.dry_run or not directory or not os.path.isdir(directory):
        return []
    out = runner.run(["findmnt", "-n", "-o", "SOURCE", "--target", directory], readonly=True, check=False)
    source = re.sub(r"\[.*\]$", "", out.strip().splitlines()[0]) if out.strip() else ""
    if not source.startswith("/dev/"):
        return []  # partage réseau : sans objet
    chain = runner.run(["lsblk", "-n", "-r", "-s", "-o", "PATH", source], readonly=True, check=False)
    devices = [line.strip() for line in chain.splitlines() if line.strip().startswith("/dev/")] or [source]
    for dev in devices:
        runner.run(["blockdev", "--setra", str(kb * 2), dev], check=False)
    return devices


_KERNEL_STORAGE = re.compile(
    r"usb|uas|reset|disconnect|I/O error|exfat|blk_update_request|Buffer I/O|critical medium|"
    r"sense key|offline|Medium Error|nvme", re.I)


def kernel_storage_messages(runner, limit=8):
    """Derniers messages du noyau sur les disques et l'USB (déconnexion, réinitialisation…)."""
    try:
        out = runner.run(["dmesg"], readonly=True, check=False) or ""
    except Exception:  # dmesg absent ou interdit : diagnostic seulement
        return []
    lines = [line.strip() for line in out.splitlines()[-500:] if _KERNEL_STORAGE.search(line)]
    return lines[-limit:]


_USB_TROUBLE = re.compile(r"usb \S+: reset|USB disconnect|uas_eh_abort|uas_eh_device_reset|"
                          r"I/O error, dev|Buffer I/O error", re.I)

USB_TROUBLE_HELP = (
    "La liaison USB avec le support des images a été coupée ou réinitialisée {n} fois depuis le début : "
    "à chaque fois la copie s'arrête plusieurs secondes, c'est ce qui la ralentit. Cause habituelle : "
    "le port USB du PC fournit trop peu de courant pour un SSD NVMe en boîtier, ou le câble est fatigué. "
    "Essayez un autre port (de préférence directement sur le PC, à l'arrière sur un PC fixe), un câble "
    "court en bon état, ou un hub USB 3 alimenté par une prise.")


class UsbWatch:
    """Surveille, pendant une copie, les coupures et réinitialisations USB vues par le noyau."""

    def __init__(self, runner):
        self.runner = runner
        self.base = self._count()
        self.warned = 0

    def _count(self):
        try:
            out = self.runner.run(["dmesg"], readonly=True, check=False) or ""
        except Exception:
            return None
        return sum(1 for line in out.splitlines() if _USB_TROUBLE.search(line))

    def check(self):
        if self.base is None:
            return 0
        n = self._count()
        if n is None:
            return 0
        if n < self.base:  # journal du noyau tourné : on repart de là
            self.base = n
            return 0
        new = n - self.base
        if new and not self.warned:
            self.runner.warn(USB_TROUBLE_HELP.format(n=new))
        elif new > self.warned:
            self.runner.log(f"  {new} coupures/réinitialisations USB depuis le début")
        self.warned = max(self.warned, new)
        return new


def store_io_error(runner, exc, writing=True):
    """Transforme une erreur système sur le support des images en message clair.

    Ajoute au journal ce que le noyau a vu (déconnexion USB, réinitialisation,
    erreur exFAT), qui dit presque toujours la cause.
    """
    import errno as _errno
    code = getattr(exc, "errno", None)
    action = "l'écriture sur" if writing else "la lecture depuis"
    if code == _errno.ENOSPC:
        return ClonixError("Le support des images est plein. Supprimez une image (bouton corbeille) "
                           "ou utilisez un support plus grand, puis recommencez.")
    if code == _errno.EROFS:
        return ClonixError("Le support des images est passé en lecture seule, souvent à cause d'une "
                           "erreur sur la clé ou le SSD. Vérifiez-le depuis Windows, puis recommencez.")
    lines = kernel_storage_messages(runner)
    if lines:
        runner.log("Messages du noyau sur les disques et l'USB :")
        for line in lines:
            runner.log(f"  {line}")
    text = " ".join(lines).lower()
    if "disconnect" in text:
        cause = "Le support des images (SSD ou clé USB) s'est déconnecté pendant la copie."
    elif "reset" in text:
        cause = "La liaison USB avec le support des images a été réinitialisée pendant la copie."
    elif "exfat" in text:
        cause = "Le système de fichiers du support des images (exFAT) signale une erreur."
    else:
        cause = f"Erreur matérielle pendant {action} le support des images ({exc.strerror or exc})."
    advice = ("Branchez le SSD directement sur un port USB 3 du PC (sans rallonge ni hub), si possible "
              "un autre port, puis recommencez. Si l'erreur revient, vérifiez le support depuis Windows "
              "(clic droit sur le lecteur › Propriétés › Outils › Vérifier).")
    if code == _errno.EIO and "exfat" in text and "disconnect" not in text and "reset" not in text:
        advice = ("Vérifiez le support depuis Windows (clic droit sur le lecteur › Propriétés › Outils › "
                  "Vérifier), puis recommencez.")
    return ClonixError(f"{cause} {advice}")


def tune_disk_readahead(runner, disk, kb=READAHEAD_KB):
    """Lecture anticipée agrandie sur le disque du PC (source d'une capture).

    partclone ne lit que les zones utilisées, une à la fois : avec les 128 Ko
    d'avance de Linux, un SSD NVMe lu ainsi reste loin de sa vitesse.
    """
    if runner.dry_run:
        return []
    devices = [disk.path] + [p.path for p in disk.partitions]
    for dev in devices:
        runner.run(["blockdev", "--setra", str(kb * 2), dev], check=False)
    return devices


def check_readable_source(disk):
    if disk.mountpoints:
        raise ClonixError(
            f"{disk.path} a des partitions montées ({', '.join(disk.mountpoints)}). "
            "Démontez-les pour obtenir une copie cohérente."
        )
