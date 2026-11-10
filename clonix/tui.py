"""Menu interactif plein écran (whiptail), lancé au démarrage de la clé USB."""

import os
import subprocess

from clonix import disks as dk
from clonix import image as im
from clonix import multicast as mc
from clonix import ops, storage
from clonix.errors import ClonixError

TITLE = "Clonix"


class Back(Exception):
    """L'utilisateur a choisi « Annuler »."""


def _whiptail(*args):
    proc = subprocess.run(["whiptail", "--title", TITLE, *args], stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise Back()
    return proc.stderr.strip()


def menu(text, items):
    flat = []
    for key, label in items:
        flat += [key, label]
    return _whiptail("--menu", text, "22", "78", "12", *flat)


def inputbox(text, default=""):
    return _whiptail("--inputbox", text, "10", "70", default)


def password(text):
    return _whiptail("--passwordbox", text, "10", "70")


def yesno(text):
    try:
        _whiptail("--defaultno", "--yesno", text, "16", "74")
        return True
    except Back:
        return False


def message(text):
    try:
        _whiptail("--msgbox", text, "16", "74")
    except Back:
        pass


def pause():
    input("\nAppuyez sur Entrée pour revenir au menu…")


def pick_disk(runner, text, image_dir, writable):
    protected = dk.protected_disks(runner, image_dir)
    items = []
    for d in dk.list_disks(runner):
        if writable and (d.path in protected or d.mountpoints or d.readonly):
            continue
        items.append((d.path, f"{dk.human_size(d.size):>10}  {d.model or d.transport or ''}"[:60]))
    if not items:
        raise ClonixError("Aucun disque disponible")
    return dk.get_disk(runner, menu(text, items))


def pick_image(image_dir):
    images = im.list_images(image_dir)
    if not images:
        raise ClonixError(f"Aucune image dans {image_dir}. Montez d'abord un dépôt.")
    items = [(n, f"{dk.human_size(s):>10}  {m['created'][:10]}  {m.get('description', '')}"[:60])
             for n, m, s in images]
    return menu("Choisissez une image :", items)


def confirm_erase(disk, what):
    parts = "\n".join(f"  {p.path} {dk.human_size(p.size)} {p.fstype} {p.label}" for p in disk.partitions)
    text = (f"{what}\n\nLe disque {disk.describe()} va être ENTIÈREMENT EFFACÉ :\n"
            f"{parts or '  (aucune partition)'}\n\nContinuer ?")
    if not yesno(text) or not yesno(f"Dernière confirmation : effacer {disk.path} ?"):
        raise Back()


def do_storage(runner, image_dir):
    options = []
    if storage.find_data_partition(runner):
        options.append(("clonix", "Sur la clé Clonix elle-même (CLONIX-DATA)"))
    options += [
        ("local", "Disque ou clé USB branché sur ce PC"),
        ("smb", "Partage réseau Windows / Samba / NAS"),
        ("nfs", "Partage réseau NFS"),
        ("sshfs", "Serveur SSH"),
        ("umount", "Démonter le dépôt actuel"),
    ]
    kind = menu("Où sont stockées les images ?", options)
    if kind == "clonix":
        storage.mount_data_partition(runner, image_dir)
        return message(f"Espace de la clé monté sur {image_dir}")
    if kind == "umount":
        storage.umount(runner, image_dir)
        return message("Dépôt démonté.")
    if kind == "local":
        parts = []
        for d in dk.list_disks(runner):
            for p in d.partitions:
                if p.fstype and p.fstype != "swap":
                    parts.append((p.path, f"{dk.human_size(p.size):>10} {p.fstype} {p.label} ({d.model})"[:60]))
        if not parts:
            raise ClonixError("Aucune partition utilisable")
        storage.mount_local(runner, menu("Partition contenant les images :", parts), image_dir)
    elif kind == "smb":
        share = inputbox("Partage (ex. //192.168.1.10/images) :", "//")
        user = inputbox("Utilisateur (vide = invité) :")
        pwd = password("Mot de passe :") if user else None
        domain = inputbox("Domaine (facultatif) :") if user else None
        storage.mount_smb(runner, share, user or None, pwd, domain or None, image_dir)
    elif kind == "nfs":
        storage.mount_nfs(runner, inputbox("Export NFS (ex. 192.168.1.10:/srv/images) :"), image_dir)
    else:
        os.system("clear")
        storage.mount_sshfs(runner, inputbox("Destination (ex. user@192.168.1.10:/srv/images) :"), image_dir)
    message(f"Dépôt d'images monté sur {image_dir}")


def do_capture(runner, image_dir):
    disk = pick_disk(runner, "Disque à capturer :", image_dir, writable=False)
    name = inputbox("Nom de l'image (sans espace) :", f"{disk.name}-{im.now_iso()[:10]}")
    description = inputbox("Description (facultatif) :")
    os.system("clear")
    ops.capture(runner, disk.path, name, image_dir, description=description)
    pause()


def do_deploy(runner, image_dir):
    name = pick_image(image_dir)
    disk = pick_disk(runner, "Disque cible (sera effacé) :", image_dir, writable=True)
    confirm_erase(disk, f"Déploiement de l'image « {name} ».")
    os.system("clear")
    ops.deploy(runner, ops.open_image(name, image_dir), disk.path, image_dir)
    pause()


def do_clone(runner, image_dir):
    src = pick_disk(runner, "Disque SOURCE :", image_dir, writable=False)
    dst = pick_disk(runner, "Disque CIBLE (sera effacé) :", image_dir, writable=True)
    confirm_erase(dst, f"Clonage de {src.describe()}.")
    os.system("clear")
    ops.clone(runner, src.path, dst.path)
    pause()


def do_send(runner, image_dir):
    name = pick_image(image_dir)
    n = int(inputbox("Nombre de PC récepteurs :", "1"))
    wait = int(inputbox("Démarrer au plus tard après (secondes) :", "600"))
    message("Démarrez maintenant les PC récepteurs sur la clé Clonix et choisissez\n"
            "« Recevoir une image multicast ». La diffusion commencera dès que les\n"
            f"{n} récepteur(s) seront connectés.")
    os.system("clear")
    mc.send(runner, name, n, image_dir, interface=mc.default_interface(), max_wait=wait)
    pause()


def do_receive(runner, image_dir):
    disk = pick_disk(runner, "Disque cible (sera effacé) :", image_dir, writable=True)
    confirm_erase(disk, "Réception d'une image multicast.")
    os.system("clear")
    mc.receive(runner, disk.path, interface=mc.default_interface())
    pause()


def do_verify(runner, image_dir):
    name = pick_image(image_dir)
    os.system("clear")
    print("Vérification en cours…")
    manifest, bad = im.verify(im.resolve(name, image_dir))
    if bad:
        message("Image CORROMPUE :\n" + "\n".join(f"  {n} : {r}" for n, r in bad))
    else:
        message(f"Image « {name} » intacte.")


ACTIONS = [
    ("1", "Choisir le dépôt d'images", do_storage),
    ("2", "Capturer un disque → image", do_capture),
    ("3", "Déployer une image → disque", do_deploy),
    ("4", "Cloner un disque → disque", do_clone),
    ("5", "Diffuser une image en multicast", do_send),
    ("6", "Recevoir une image multicast", do_receive),
    ("7", "Vérifier une image", do_verify),
]


def main(runner, image_dir):
    # Monte automatiquement l'espace « CLONIX-DATA » de la clé s'il existe.
    storage.auto_mount_data(runner, image_dir)
    while True:
        mounted = "monté" if os.path.ismount(image_dir) else "NON MONTÉ"
        items = [(k, label) for k, label, _ in ACTIONS]
        items += [("s", "Terminal (shell)"), ("r", "Redémarrer"), ("p", "Éteindre")]
        try:
            choice = menu(f"Dépôt d'images : {image_dir} ({mounted})", items)
        except Back:
            if yesno("Quitter Clonix et ouvrir un terminal ?"):
                return 0
            continue
        if choice == "s":
            os.system("clear; echo 'Tapez exit pour revenir au menu.'; bash -l")
            continue
        if choice in ("r", "p"):
            if yesno("Confirmer ?"):
                subprocess.run(["systemctl", "reboot" if choice == "r" else "poweroff"])
            continue
        action = next(f for k, _, f in ACTIONS if k == choice)
        try:
            action(runner, image_dir)
        except Back:
            continue
        except (ClonixError, ValueError) as exc:
            message(f"Erreur : {exc}")
