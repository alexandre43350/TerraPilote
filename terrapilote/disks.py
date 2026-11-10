"""Inventaire des disques et règles de sécurité."""

import json
import os
import re
from dataclasses import dataclass, field

from terrapilote.errors import TerraPiloteError

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
        raise TerraPiloteError(f"Numéro de partition introuvable dans {path}")
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
    include_loop = os.environ.get("TERRAPILOTE_ALLOW_LOOP") == "1"
    out = runner.run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS], readonly=True)
    return parse_lsblk(out, include_loop=include_loop)


def get_disk(runner, path):
    path = os.path.realpath(path)
    for disk in list_disks(runner):
        if os.path.realpath(disk.path) == path:
            return disk
    raise TerraPiloteError(f"{path} n'est pas un disque connu (voir `terrapilote disks`)")


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
    """Disques sur lesquels TerraPilote ne doit jamais écrire."""
    protected = {}
    disk = disk_of_mount(runner, LIVE_MEDIUM)
    if disk:
        protected[disk] = "la clé USB TerraPilote"
    if image_dir and os.path.isdir(image_dir):
        disk = disk_of_mount(runner, image_dir)
        if disk:
            protected.setdefault(disk, "le dépôt d'images")
    return protected


def check_writable_target(runner, disk, image_dir=None):
    if disk.readonly:
        raise TerraPiloteError(f"{disk.path} est en lecture seule")
    reason = protected_disks(runner, image_dir).get(disk.path)
    if reason:
        raise TerraPiloteError(f"Refus : {disk.path} héberge {reason}")
    if disk.mountpoints:
        raise TerraPiloteError(
            f"{disk.path} est utilisé ({', '.join(disk.mountpoints)}). Démontez-le d'abord."
        )


def check_readable_source(disk):
    if disk.mountpoints:
        raise TerraPiloteError(
            f"{disk.path} a des partitions montées ({', '.join(disk.mountpoints)}). "
            "Démontez-les pour obtenir une copie cohérente."
        )
