"""Entrée de démarrage UEFI après un déploiement ou un clonage.

Un PC UEFI démarre grâce à une entrée enregistrée dans sa mémoire interne
(« Windows Boot Manager »…), qui désigne une partition précise par son
identifiant. Après un déploiement, cette entrée désigne souvent une partition
qui n'existe plus : le PC affiche « Périphérique d'amorçage introuvable » (HP :
erreur 3F0) alors que Windows est bien là.

Comme Clonezilla, Clonix recrée l'entrée vers la partition EFI du disque déployé
(efibootmgr), supprime les entrées orphelines du même nom, et ajoute si besoin
le chargeur de secours \\EFI\\Boot\\bootx64.efi que tout PC UEFI sait trouver seul.
"""

import os
import re
import shutil
import tempfile

from clonix import disks as dk
from clonix import partitions as pt
from clonix.errors import ClonixError

ESP_TYPES = {"c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "ef"}
FALLBACK = ("EFI", "Boot", "bootx64.efi")
# (nom de l'entrée, chemin du chargeur sur la partition EFI), par priorité
LOADERS = [
    ("Windows Boot Manager", ("EFI", "Microsoft", "Boot", "bootmgfw.efi")),
    ("debian", ("EFI", "debian", "shimx64.efi")),
    ("debian", ("EFI", "debian", "grubx64.efi")),
    ("ubuntu", ("EFI", "ubuntu", "shimx64.efi")),
]
_ENTRY = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*?)\s*(?:\t|\s{2,}|HD\()(.*)$")
_PARTUUID = re.compile(r"HD\(\d+,GPT,([0-9a-fA-F-]{36})", re.I)


def is_uefi(sysfs="/sys/firmware/efi"):
    """Clonix a-t-il démarré en mode UEFI ? (sinon efibootmgr ne peut rien faire)"""
    return os.path.isdir(sysfs)


def find_ci(root, parts):
    """Chemin d'un fichier sur une partition FAT, sans tenir compte des majuscules."""
    path = root
    for name in parts:
        try:
            entries = os.listdir(path)
        except OSError:
            return None
        match = next((e for e in entries if e.lower() == name.lower()), None)
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def parse_entries(text):
    """Entrées de ``efibootmgr -v`` : [(numéro, nom, partuuid ou None)]."""
    out = []
    for line in (text or "").splitlines():
        m = _ENTRY.match(line.strip())
        if not m:
            continue
        uuid = _PARTUUID.search(line)
        out.append((m.group(1).upper(), m.group(2).strip(), uuid.group(1).lower() if uuid else None))
    return out


def esp_partitions(runner, disk_path):
    """Numéros des partitions EFI du disque, d'après sa table."""
    table = pt.parse_dump(pt.dump_table(runner, disk_path))
    return [e.number for e in table.entries if str(e.type).lower() in ESP_TYPES]


def partuuid(runner, part_path):
    out = runner.run(["blkid", "-s", "PARTUUID", "-o", "value", part_path], readonly=True, check=False)
    return (out or "").strip().lower() or None


def present_partuuids(byid="/dev/disk/by-partuuid"):
    try:
        return {n.lower() for n in os.listdir(byid)}
    except OSError:
        return set()


def repair(runner, disk_path, efi_sysfs="/sys/firmware/efi", byid="/dev/disk/by-partuuid"):
    """Recrée l'entrée de démarrage UEFI du système présent sur ``disk_path``.

    Retourne le nom de l'entrée créée, ou None s'il n'y avait rien à faire.
    Ne lève une erreur que si quelque chose a été tenté et a échoué.
    """
    if runner.dry_run:
        runner.log(f"[dry-run] entrée de démarrage UEFI pour {disk_path}")
        return None
    if not is_uefi(efi_sysfs):
        runner.log("PC démarré en mode BIOS (Legacy) : pas d'entrée de démarrage UEFI à créer.")
        return None
    if not runner.which("efibootmgr"):
        raise ClonixError("Outil introuvable : efibootmgr")
    esps = esp_partitions(runner, disk_path)
    if not esps:
        runner.log(f"{disk_path} : pas de partition EFI, démarrage UEFI non concerné.")
        return None

    for number in esps:
        part = dk.part_path(disk_path, number)
        mp = tempfile.mkdtemp(prefix="clonix-esp-")
        try:
            try:
                runner.run(["mount", "-t", "vfat", part, mp])
            except ClonixError as exc:
                runner.log(f"  {part} : montage impossible ({exc})")
                continue
            try:
                found = next(((label, parts) for label, parts in LOADERS if find_ci(mp, parts)), None)
                if found is None:
                    continue
                label, parts = found
                loader = find_ci(mp, parts)
                if not find_ci(mp, FALLBACK):
                    # Chargeur de secours : trouvé par le PC même sans entrée enregistrée.
                    boot_dir = find_ci(mp, FALLBACK[:2]) or os.path.join(find_ci(mp, FALLBACK[:1]), "Boot")
                    os.makedirs(boot_dir, exist_ok=True)
                    shutil.copy2(loader, os.path.join(boot_dir, FALLBACK[-1]))
                    runner.log(f"  chargeur de secours \\EFI\\Boot\\bootx64.efi ajouté sur {part}")
            finally:
                runner.run(["sync"], check=False)
                runner.run(["umount", mp], check=False)
        finally:
            try:
                os.rmdir(mp)
            except OSError:
                pass

        target = partuuid(runner, part)
        present = present_partuuids(byid)
        listing = runner.run(["efibootmgr", "-v"], readonly=True, check=False)
        for num, name, uuid in parse_entries(listing):
            if name != label:
                continue
            if uuid is None or uuid == target or uuid not in present:
                runner.run(["efibootmgr", "--quiet", "--bootnum", num, "--delete-bootnum"], check=False)
        win_path = "\\" + "\\".join(parts)
        runner.run(["efibootmgr", "--quiet", "--create", "--disk", disk_path, "--part", str(number),
                    "--label", label, "--loader", win_path])
        runner.log(f"✓ Entrée de démarrage « {label} » créée vers {part} (en tête de l'ordre de démarrage).")
        return label
    runner.log(f"{disk_path} : aucun chargeur Windows ou Linux trouvé sur la partition EFI.")
    return None
