"""Mode atelier : déploiement en série, sans clic.

Réglé une fois (image, nom des PC, quoi faire après), il est enregistré sur le
support des images. Ensuite, chaque PC qui démarre sur la clé :

1. cherche son disque interne ; il faut qu'il y en ait exactement un, assez
   grand, qui ne soit ni la clé, ni le support des images, ni un disque USB ;
2. affiche un compte à rebours plein écran (avec « Annuler ») ;
3. déploie l'image, renomme le PC, puis s'éteint ou redémarre.

Au moindre doute (aucun disque, plusieurs disques, image absente…), rien n'est
effacé : l'écran explique pourquoi et Clonix reste en mode normal.
"""

import json
import os

from clonix import disks as dk
from clonix import image as im
from clonix import naming
from clonix.errors import ClonixError

CONFIG_FILE = ".clonix-atelier.json"
AFTER = ("poweroff", "reboot", "none")
DEFAULTS = {"enabled": False, "image": "", "name_pattern": "", "adapt": True, "expand": True,
            "uefi": True, "after": "poweroff", "countdown": 20, "done": 0}


def load(image_dir):
    try:
        with open(os.path.join(image_dir, CONFIG_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(image_dir, cfg):
    tmp = os.path.join(image_dir, CONFIG_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(image_dir, CONFIG_FILE))


def validate(image_dir, body):
    """Réglages envoyés par l'interface → réglages vérifiés (ou erreur claire)."""
    images = {n: m for n, m, _ in im.list_images(image_dir)}
    image = body.get("image") or ""
    if image not in images:
        raise ClonixError("Choisissez une image du dépôt")
    pattern = (body.get("name_pattern") or "").strip()
    if pattern:
        naming.check_pattern(pattern)
    after = body.get("after", "poweroff")
    if after not in AFTER:
        raise ClonixError("Action après le déploiement inconnue")
    try:
        countdown = int(body.get("countdown", 20))
    except (TypeError, ValueError):
        countdown = 20
    countdown = min(max(countdown, 10), 120)  # jamais moins de 10 s pour pouvoir annuler
    old = load(image_dir) or {}
    return {**DEFAULTS, "enabled": True, "image": image, "name_pattern": pattern,
            "adapt": bool(body.get("adapt", True)), "expand": bool(body.get("expand", True)),
            "uefi": bool(body.get("uefi", True)), "after": after, "countdown": countdown,
            "done": old.get("done", 0) if old.get("image") == image else 0}


def image_need(manifest):
    """Taille de disque nécessaire pour l'image (plus petite si l'image est adaptable)."""
    table = manifest.get("table", {})
    return table.get("min_bytes") or table.get("required_bytes", 0)


def pick_target(runner, image_dir, need):
    """Le disque interne à effacer, ou (None, raison) s'il n'y a pas exactement un choix sûr."""
    protected = dk.protected_disks(runner, image_dir)
    internal, too_small = [], []
    for d in dk.list_disks(runner):
        if d.readonly or d.removable or d.transport == "usb" or d.path in protected or dk.is_boot_key(d):
            continue
        if d.mountpoints:
            continue
        if d.size < need:
            too_small.append(d)
            continue
        internal.append(d)
    if len(internal) == 1:
        return internal[0], None
    if not internal and too_small:
        d = too_small[0]
        return None, (f"le disque interne {d.path} ({dk.human_size(d.size)}) est trop petit pour l'image "
                      f"({dk.human_size(need)} nécessaires)")
    if not internal:
        return None, "aucun disque interne trouvé"
    names = ", ".join(f"{d.path} ({dk.human_size(d.size)})" for d in internal)
    return None, f"plusieurs disques internes ({names}) : choisissez-le à la main"
