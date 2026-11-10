"""Table de partitions (MBR/GPT) au format ``sfdisk --dump``."""

import re
from dataclasses import dataclass

from terrapilote.disks import part_number, part_path
from terrapilote.errors import TerraPiloteError

GPT_BACKUP_SECTORS = 33  # en-tête + entrées GPT de secours en fin de disque
MAX_BOOT_AREA = 16 * 1024 * 1024


@dataclass
class TableEntry:
    number: int
    start: int  # en secteurs
    size: int  # en secteurs
    type: str = ""


@dataclass
class Table:
    label: str  # "gpt" ou "dos"
    sector_size: int
    entries: list
    first_lba: int = 34

    @property
    def end_sector(self):
        return max((e.start + e.size for e in self.entries), default=0)

    @property
    def first_start(self):
        return min((e.start for e in self.entries), default=2048)

    def required_bytes(self):
        extra = GPT_BACKUP_SECTORS if self.label == "gpt" else 0
        return (self.end_sector + extra) * self.sector_size

    def boot_area_bytes(self):
        """Zone avant la 1re partition : MBR, GPT, chargeur GRUB (BIOS)…"""
        return min(self.first_start * self.sector_size, MAX_BOOT_AREA)

    def boot_regions(self, length):
        """Plages (début, fin) de la zone de boot à recopier après sfdisk.

        On ne réécrit pas la table elle-même (sfdisk vient de l'écrire pour le
        disque cible) : seulement le code d'amorçage du MBR (440 premiers
        octets) et l'espace libre entre la table et la 1re partition, où
        GRUB installe son « core.img » en mode BIOS.
        """
        gap_start = (self.first_lba if self.label == "gpt" else 1) * self.sector_size
        regions = [(0, min(440, length))]
        if length > gap_start:
            regions.append((gap_start, length))
        return regions


_LINE = re.compile(r"^(?P<dev>\S+)\s*:\s*(?P<fields>.*)$")


def _fields(text):
    out = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            out[key.strip()] = value.strip()
        else:
            out[item] = True  # "bootable"
    return out


def parse_dump(text):
    header = {}
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if m and m.group("dev").startswith("/"):
            f = _fields(m.group("fields"))
            entries.append(
                TableEntry(
                    number=part_number(m.group("dev")),
                    start=int(f.get("start", 0)),
                    size=int(f.get("size", 0)),
                    type=str(f.get("type", "")),
                )
            )
        elif ":" in line:
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()
    label = header.get("label")
    if label not in ("gpt", "dos"):
        raise TerraPiloteError(f"Table de partitions non prise en charge : {label or 'aucune'}")
    entries.sort(key=lambda e: e.number)
    return Table(
        label=label,
        sector_size=int(header.get("sector-size", 512)),
        entries=entries,
        first_lba=int(header.get("first-lba", 34)),
    )


def rewrite_dump(text, target_disk, new_uuids=False):
    """Adapte un dump sfdisk pour l'écrire sur ``target_disk``.

    - les noms de partitions pointent vers le disque cible ;
    - ``device`` et ``last-lba`` sont retirés : sfdisk recalcule la fin du GPT
      pour un disque cible plus grand ;
    - ``new_uuids`` retire identifiants de disque et de partitions pour que
      sfdisk en génère de nouveaux (utile pour éviter les doublons sur un parc).
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _LINE.match(line)
        if m and m.group("dev").startswith("/"):
            fields = m.group("fields")
            if new_uuids:
                fields = re.sub(r",?\s*uuid=[^,]*", "", fields)
            dev = part_path(target_disk, part_number(m.group("dev")))
            out.append(f"{dev} : {fields.strip().strip(',').strip()}")
            continue
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in ("device", "last-lba"):
            continue
        if new_uuids and key == "label-id":
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def dump_table(runner, disk_path):
    text = runner.run(["sfdisk", "--dump", disk_path], readonly=True)
    parse_dump(text)  # valide le format
    return text


def write_table(runner, disk, dump_text, new_uuids=False):
    table = parse_dump(dump_text)
    if table.sector_size != disk.sector_size:
        raise TerraPiloteError(
            f"Taille de secteur incompatible : image {table.sector_size} o, "
            f"{disk.path} {disk.sector_size} o"
        )
    if table.required_bytes() > disk.size:
        raise TerraPiloteError(
            f"{disk.path} est trop petit : {table.required_bytes()} octets requis, {disk.size} disponibles"
        )
    script = rewrite_dump(dump_text, disk.path, new_uuids=new_uuids)
    runner.run(["wipefs", "--all", "--force", disk.path])
    runner.run(["sfdisk", "--force", "--no-reread", disk.path], input=script)
    reload_partitions(runner, disk.path)
    return table


def save_boot_area(runner, disk_path, table):
    return runner.read_at(disk_path, 0, table.boot_area_bytes())


def restore_boot_area(runner, disk_path, table, data):
    for start, end in table.boot_regions(len(data)):
        runner.write_at(disk_path, start, data[start:end])


def reload_partitions(runner, disk_path):
    runner.run(["partprobe", disk_path], check=False)
    runner.run(["udevadm", "settle"], check=False)
