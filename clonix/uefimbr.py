"""Rendre démarrable en UEFI un Windows installé en mode BIOS (disque MBR). Expérimental.

Les outils de Microsoft (mbr2gpt, bcdboot) et ceux qui s'en servent (AOMEI…)
fonctionnent sous Windows. Clonix, sous Linux, fait le minimum nécessaire sans
toucher aux partitions de Windows ni convertir la table :

1. le disque reste en MBR : un PC UEFI sait démarrer sur une partition EFI
   d'un disque MBR (type 0xEF), et Windows aussi (comme Windows To Go) ;
2. la petite partition « Réservé au système » (démarrage BIOS) devient cette
   partition EFI : son contenu est sauvegardé en mémoire, elle est reformatée
   en FAT32, son contenu est remis, et l'on y ajoute les fichiers de démarrage
   UEFI que Windows contient déjà (C:\\Windows\\Boot\\EFI) ;
3. la configuration de démarrage existante (BCD) est reprise : ses références
   aux partitions (signature du disque MBR + position) restent justes, seuls
   les chemins changent (winload.exe → winload.efi…).

Le disque n'est plus démarrable en mode BIOS ensuite (on peut le redéployer).
"""

import contextlib
import os
import shutil
import tempfile

from clonix import disks as dk
from clonix import efiboot
from clonix import partitions as pt
from clonix.errors import ClonixError

BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
APP_PATH = "12000002"  # BcdLibraryString_ApplicationPath
REG_SZ = 1
UEFI_BOOTMGR = "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
PATH_FIXES = {
    "\\windows\\system32\\winload.exe": "\\Windows\\system32\\winload.efi",
    "\\windows\\system32\\winresume.exe": "\\Windows\\system32\\winresume.efi",
    "\\boot\\memtest.exe": "\\EFI\\Microsoft\\Boot\\memtest.efi",
}
MAX_SYSTEM_RESERVED = 2 * 1024 ** 3
FAT_MAX_FILE = 4 * 1024 ** 3 - 1


def _child(h, node, name):
    for c in h.node_children(node):
        if h.node_name(c).lower() == name.lower():
            return c
    return None


def _get_string(h, node, key="Element"):
    for v in h.node_values(node):
        if h.value_key(v) == key:
            _t, data = h.value_value(v)
            return data.decode("utf-16-le", "replace").rstrip("\x00")
    return None


def _set_string(h, node, text, key="Element"):
    h.node_set_value(node, {"key": key, "t": REG_SZ, "value": (text + "\x00").encode("utf-16-le")})


def patch_bcd(path, hivex_mod=None):
    """Adapte une configuration de démarrage BIOS (BCD) au démarrage UEFI.

    Retourne la liste des changements. Lève une erreur si aucun chargeur
    Windows n'a été trouvé (ce n'est pas un BCD de Windows installé en BIOS).
    """
    if hivex_mod is None:
        from clonix.winfix import _load_hivex
        hivex_mod = _load_hivex()
    h = hivex_mod.Hivex(path, write=True)
    objects = _child(h, h.root(), "Objects")
    if objects is None:
        raise ClonixError("Configuration de démarrage (BCD) illisible : pas d'objets")
    changes, loaders = [], 0
    for obj in h.node_children(objects):
        name = h.node_name(obj)
        elements = _child(h, obj, "Elements")
        if elements is None:
            continue
        element = _child(h, elements, APP_PATH)
        if name.lower() == BOOTMGR:
            if element is None:
                element = h.node_add_child(elements, APP_PATH)
            _set_string(h, element, UEFI_BOOTMGR)
            changes.append(f"gestionnaire de démarrage → {UEFI_BOOTMGR}")
            continue
        if element is None:
            continue
        current = _get_string(h, element)
        new = PATH_FIXES.get((current or "").lower())
        if new:
            _set_string(h, element, new)
            changes.append(f"{current} → {new}")
            loaders += "winload" in new
    if not loaders:
        raise ClonixError("Aucun chargeur Windows (winload.exe) dans la configuration de démarrage")
    h.commit(None)
    return changes


@contextlib.contextmanager
def _mounted(runner, device, fstype, readonly=False):
    mp = tempfile.mkdtemp(prefix="clonix-mbr-")
    opts = ["-o", "ro"] if readonly else []
    try:
        runner.run(["mount", "-t", fstype, *opts, device, mp])
        try:
            yield mp
        finally:
            runner.run(["sync"], check=False)
            runner.run(["umount", mp], check=False)
    finally:
        try:
            os.rmdir(mp)
        except OSError:
            pass


def _copy_tree(src, dst):
    """Copie récursive vers une partition FAT (droits Windows non conservés)."""
    skipped = []
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        if rel.split(os.sep)[0].lower() == "system volume information":
            dirs[:] = []
            continue
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in files:
            s = os.path.join(root, name)
            if os.path.getsize(s) > FAT_MAX_FILE:
                skipped.append(os.path.join(rel, name))
                continue
            shutil.copyfile(s, os.path.join(dst, rel, name))
    return skipped


def find_partitions(runner, disk):
    """(partition « Réservé au système », partition Windows) d'un disque MBR."""
    sysres = win = None
    for part in disk.partitions:
        if part.fstype != "ntfs":
            continue
        try:
            with _mounted(runner, part.path, "ntfs-3g", readonly=True) as mp:
                has_win = efiboot.find_ci(mp, ("Windows", "Boot", "EFI", "bootmgfw.efi")) is not None
                has_bcd = efiboot.find_ci(mp, ("Boot", "BCD")) is not None
        except ClonixError:
            continue
        if has_win and win is None:
            win = part
        elif has_bcd and sysres is None:
            sysres = part
    return sysres, win


def make_uefi_bootable(runner, disk_path, hivex_mod=None):
    """Rend le Windows (installé en BIOS, disque MBR) de ``disk_path`` démarrable en UEFI."""
    if runner.dry_run:
        runner.log(f"[dry-run] rendre {disk_path} démarrable en UEFI")
        return []
    table = pt.parse_dump(pt.dump_table(runner, disk_path))
    if table.label != "dos":
        raise ClonixError(f"{disk_path} n'est pas au format MBR : rien à convertir.")
    disk = dk.get_disk(runner, disk_path)
    dk.check_writable_target(runner, disk)
    sysres, win = find_partitions(runner, disk)
    if win is None:
        raise ClonixError("Windows introuvable sur ce disque, ou trop ancien pour démarrer en UEFI "
                          "(Windows 8 ou plus récent nécessaire).")
    if sysres is None:
        raise ClonixError("Pas de partition « Réservé au système » séparée sur ce disque : conversion "
                          "automatique impossible. Activez le mode Legacy/CSM du PC, ou convertissez le "
                          "PC d'origine avec « mbr2gpt /convert /allowFullOS » puis recapturez.")
    if sysres.size > MAX_SYSTEM_RESERVED:
        raise ClonixError(f"La partition de démarrage {sysres.path} est inhabituellement grande : "
                          "conversion annulée par prudence.")

    runner.log(f"Démarrage UEFI : {sysres.path} (Réservé au système) devient la partition EFI de {disk.path}")
    backup = tempfile.mkdtemp(prefix="clonix-sysres-")
    try:
        # 1. sauvegarde complète avant toute écriture
        with _mounted(runner, sysres.path, "ntfs-3g", readonly=True) as mp:
            _copy_tree(mp, backup)
        bcd_src = efiboot.find_ci(backup, ("Boot", "BCD"))
        if bcd_src is None:
            raise ClonixError("Configuration de démarrage (BCD) non sauvegardée : conversion annulée.")
        # 2. la partition devient une partition EFI en FAT32
        fat = ["mkfs.fat", "-F", "32", "-n", "SYSTEM"]
        if sysres.size < 260 * 1024 ** 2:
            fat += ["-s", "1"]  # petite partition : clusters de 512 o pour rester en FAT32
        runner.run(fat + [sysres.path])
        runner.run(["sfdisk", "--part-type", disk.path, str(sysres.number), "ef"])
        pt.reload_partitions(runner, disk.path)
        # 3. contenu d'origine + fichiers de démarrage UEFI de Windows + BCD adapté
        with _mounted(runner, sysres.path, "vfat") as esp:
            skipped = _copy_tree(backup, esp)
            boot = os.path.join(esp, "EFI", "Microsoft", "Boot")
            with _mounted(runner, win.path, "ntfs-3g", readonly=True) as wmp:
                _copy_tree(efiboot.find_ci(wmp, ("Windows", "Boot", "EFI")), boot)
                fonts = efiboot.find_ci(wmp, ("Windows", "Boot", "Fonts"))
                if fonts:
                    _copy_tree(fonts, os.path.join(boot, "Fonts"))
            shutil.copyfile(bcd_src, os.path.join(boot, "BCD"))
            changes = patch_bcd(os.path.join(boot, "BCD"), hivex_mod)
            os.makedirs(os.path.join(esp, "EFI", "Boot"), exist_ok=True)
            shutil.copyfile(efiboot.find_ci(boot, ("bootmgfw.efi",)), os.path.join(esp, "EFI", "Boot", "bootx64.efi"))
        for line in changes:
            runner.log(f"  BCD : {line}")
        for name in skipped:
            runner.log(f"  fichier de plus de 4 Go non recopié : {name}")
    finally:
        shutil.rmtree(backup, ignore_errors=True)
    runner.log(f"✓ {disk.path} : Windows démarrable en UEFI (partition EFI {sysres.path}).")
    efiboot.repair(runner, disk.path)
    return changes
