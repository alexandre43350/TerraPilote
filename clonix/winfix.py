"""Adapter un Windows déployé au PC qui le reçoit (équivalent « Universal Restore »).

Un Windows copié tel quel sur un autre PC plante souvent au démarrage avec
INACCESSIBLE_BOOT_DEVICE (0x7B) : le pilote du contrôleur de disque du nouveau
PC n'est pas chargé au démarrage. On règle donc, dans le registre hors ligne de
Windows (fichier Windows/System32/config/SYSTEM), les deux pilotes de disque
standard de Windows pour qu'ils démarrent toujours :

- stornvme : disques NVMe ;
- storahci : disques SATA en mode AHCI.

Pour chacun : Start = 0 (démarrage « boot ») et suppression de StartOverride,
qui sinon désactive le pilote. C'est la méthode documentée par Microsoft pour
passer un PC en mode AHCI sans réinstaller.

Un PC dont le BIOS utilise Intel VMD/RST a besoin d'un pilote Intel qui n'est
pas fourni par Windows : il faut alors désactiver VMD dans son BIOS.

Seule la copie déployée est modifiée, jamais l'image ni le PC source.
"""

import os
import struct
import tempfile

from clonix import disks as dk
from clonix.errors import ClonixError

STORAGE_DRIVERS = ("stornvme", "storahci")
REG_DWORD = 4


def _ci_children(parent, name):
    try:
        return [os.path.join(parent, e) for e in os.listdir(parent) if e.lower() == name.lower()]
    except OSError:
        return []


def find_system_hive(root):
    """Chemin de Windows/System32/config/SYSTEM sous ``root`` (casse ignorée)."""
    for w in _ci_children(root, "Windows"):
        for s in _ci_children(w, "System32"):
            for c in _ci_children(s, "config"):
                for f in _ci_children(c, "SYSTEM"):
                    if os.path.isfile(f):
                        return f
    return None


def _child(h, node, name):
    for c in h.node_children(node):
        if h.node_name(c).lower() == name.lower():
            return c
    return None


def _load_hivex():
    try:
        import hivex
    except ImportError:
        raise ClonixError("Le module python3-hivex est absent de la clé : reconstruisez la clé.") from None
    return hivex


def enable_storage_drivers(hive_path, hivex_mod=None):
    """Active les pilotes de disque standard dans la ruche SYSTEM.

    Retourne la liste des modifications (texte) ; lève une erreur si aucun
    pilote n'a été trouvé (ce n'est alors pas une ruche SYSTEM de Windows).
    """
    hivex_mod = hivex_mod or _load_hivex()
    h = hivex_mod.Hivex(hive_path, write=True)
    changes = []
    for control_set in h.node_children(h.root()):
        cs_name = h.node_name(control_set)
        if not cs_name.lower().startswith("controlset"):
            continue
        services = _child(h, control_set, "Services")
        if services is None:
            continue
        for driver in STORAGE_DRIVERS:
            node = _child(h, services, driver)
            if node is None:
                continue
            h.node_set_value(node, {"key": "Start", "t": REG_DWORD, "value": struct.pack("<I", 0)})
            changes.append(f"{cs_name}\\Services\\{driver} : démarrage automatique")
            override = _child(h, node, "StartOverride")
            if override is not None:
                h.node_delete_child(override)
                changes.append(f"{cs_name}\\Services\\{driver} : StartOverride supprimé")
    if not changes:
        raise ClonixError("Aucun pilote de disque Windows trouvé dans le registre")
    h.commit(None)
    return changes


def adapt(runner, disk_path, hivex_mod=None):
    """Adapte le Windows présent sur ``disk_path`` au PC actuel.

    Monte chaque partition NTFS (en supprimant un éventuel fichier de veille
    prolongée : une session suspendue venant d'un autre PC ne doit pas être
    reprise), cherche le registre SYSTEM et active les pilotes de disque.
    """
    disk = dk.get_disk(runner, disk_path)
    dk.check_writable_target(runner, disk)
    if runner.dry_run:
        runner.log(f"[dry-run] adaptation de Windows sur {disk.path} (pilotes {', '.join(STORAGE_DRIVERS)})")
        return []
    for part in disk.partitions:
        if part.fstype != "ntfs":
            continue
        mp = tempfile.mkdtemp(prefix="clonix-win-")
        try:
            try:
                runner.run(["mount", "-t", "ntfs-3g", "-o", "remove_hiberfile", part.path, mp])
            except ClonixError as exc:
                runner.log(f"  {part.path} : montage impossible ({exc})")
                continue
            try:
                hive = find_system_hive(mp)
                if not hive:
                    continue
                runner.log(f"Adaptation de Windows ({part.path}) à ce PC…")
                changes = enable_storage_drivers(hive, hivex_mod)
                for line in changes:
                    runner.log(f"  {line}")
                return changes
            finally:
                runner.run(["sync"], check=False)
                runner.run(["umount", mp], check=False)
        finally:
            try:
                os.rmdir(mp)
            except OSError:
                pass
    raise ClonixError(f"Aucune installation Windows trouvée sur {disk.path}")
