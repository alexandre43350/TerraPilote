"""Inventaire des disques et règles de sécurité."""

import json
import os
import re
from dataclasses import dataclass, field

from clonix.errors import ClonixError

LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM,RO,LOG-SEC,PKNAME"
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
