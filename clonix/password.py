"""Réinitialisation du mot de passe administrateur local d'un poste déployé.

Après avoir déployé une image sur un PC, on a souvent besoin de redonner la main
sur le compte administrateur local (mot de passe oublié, image d'un autre parc,
compte à réinitialiser avant remise en service). Clonix monte la partition
système du disque choisi et agit sur le compte local :

- **Windows** : via ``chntpw`` sur la ruche SAM. L'effacement (mot de passe vide)
  est fiable sur toutes les versions ; définir un mot de passe précis ne l'est pas
  sur Windows 10/11 (chntpw lui-même le déconseille), donc Clonix ne propose que
  l'effacement pour Windows.
- **Linux** : en modifiant ``/etc/shadow`` (effacer ou définir un nouveau mot de
  passe, haché avec ``openssl passwd -6``).

Le disque visé est traité comme une cible en écriture : la clé Clonix, le dépôt
d'images et tout disque monté sont refusés, comme pour un déploiement. L'opérateur
a déjà le contrôle physique du poste (il vient d'en écrire le disque entier).

Ceci ne contourne pas un chiffrement : une partition BitLocker ou LUKS verrouillée
ne se monte pas et l'opération échoue proprement.
"""

import os
import tempfile

from clonix import disks as dk
from clonix.errors import ClonixError

WINDOWS = "windows"
LINUX = "linux"
SUPPORTED_FS = {"ntfs", "ext2", "ext3", "ext4", "btrfs", "xfs"}
NOLOGIN_SHELLS = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", ""}


# --------------------------------------------------------------- système de fichiers


def _ci_children(parent, name):
    """Entrées de ``parent`` dont le nom vaut ``name`` (casse ignorée)."""
    try:
        return [os.path.join(parent, e) for e in os.listdir(parent) if e.lower() == name.lower()]
    except OSError:
        return []


def find_sam(root):
    """Chemin de Windows/System32/config/SAM sous ``root`` (ou None)."""
    for w in _ci_children(root, "Windows"):
        for s in _ci_children(w, "System32"):
            for c in _ci_children(s, "config"):
                for sam in _ci_children(c, "SAM"):
                    if os.path.isfile(sam):
                        return sam
    return None


def detect_os(root):
    if find_sam(root):
        return WINDOWS
    if os.path.isfile(os.path.join(root, "etc", "shadow")):
        return LINUX
    return None


# ------------------------------------------------------------------------ Windows


def chntpw_list_cmd(sam):
    return ["chntpw", "-l", sam]


def chntpw_clear_cmd(sam, user):
    return ["chntpw", "-u", user, sam]


# Menu chntpw : « 1 » efface le mot de passe, « q » quitte, « y » confirme l'écriture.
CHNTPW_CLEAR_INPUT = "1\nq\ny\n"


def parse_chntpw_users(output):
    """Noms d'utilisateurs listés par ``chntpw -l`` (table RID | Username | ...)."""
    users = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("|") or "RID" in line:
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) >= 2 and cols[1]:
            users.append(cols[1])
    return users


def windows_accounts(runner, sam):
    return parse_chntpw_users(runner.run(chntpw_list_cmd(sam), readonly=True, check=False))


def windows_clear(runner, sam, user):
    if not runner.which("chntpw"):
        raise ClonixError("Outil chntpw introuvable (nécessaire pour les comptes Windows)")
    runner.run(chntpw_clear_cmd(sam, user), input=CHNTPW_CLEAR_INPUT)


# -------------------------------------------------------------------------- Linux


def parse_passwd(text):
    """Comptes humains de /etc/passwd : (utilisateur, uid, admin?)."""
    accounts = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 7:
            continue
        name, _, uid, _, _, _, shell = parts[:7]
        try:
            uid = int(uid)
        except ValueError:
            continue
        if uid != 0 and uid < 1000:
            continue  # comptes système
        if shell in NOLOGIN_SHELLS and uid != 0:
            continue
        accounts.append((name, uid, uid == 0))
    return accounts


def edit_shadow(text, user, new_field):
    """Remplace le champ mot de passe de ``user`` dans /etc/shadow.

    ``new_field`` : "" pour un compte sans mot de passe, "!" pour verrouillé,
    ou le haché pour un nouveau mot de passe. Lève une erreur si l'utilisateur
    est absent.
    """
    out = []
    found = False
    for line in text.splitlines():
        parts = line.split(":")
        if parts and parts[0] == user:
            found = True
            if len(parts) < 2:
                parts.append(new_field)
            else:
                parts[1] = new_field
            line = ":".join(parts)
        out.append(line)
    if not found:
        raise ClonixError(f"Compte « {user} » introuvable dans /etc/shadow")
    return "\n".join(out) + "\n"


def hash_password(runner, password):
    """Haché SHA-512 pour /etc/shadow, calculé par openssl (sans exposer le mot
    de passe dans la liste des processus)."""
    if not runner.which("openssl"):
        raise ClonixError("Outil openssl introuvable (nécessaire pour définir un mot de passe)")
    out = runner.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n")
    digest = out.strip()
    if not digest.startswith("$6$"):
        raise ClonixError("Échec du calcul du haché du mot de passe")
    return digest


def linux_accounts(runner, root):
    with open(os.path.join(root, "etc", "passwd"), encoding="utf-8", errors="replace") as f:
        return parse_passwd(f.read())


def _rewrite_shadow(runner, root, user, new_field):
    path = os.path.join(root, "etc", "shadow")
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    new = edit_shadow(text, user, new_field)
    if runner.dry_run:
        runner.log(f"[dry-run] mise à jour de /etc/shadow pour {user}")
        return
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(new)


def linux_clear(runner, root, user):
    _rewrite_shadow(runner, root, user, "")


def linux_set(runner, root, user, password):
    _rewrite_shadow(runner, root, user, hash_password(runner, password))


# ----------------------------------------------------------------- orchestration


def _mount(runner, device, mountpoint, readonly):
    opts = ["ro"] if readonly else ["rw"]
    runner.run(["mount", "-o", ",".join(opts), device, mountpoint])


def _umount(runner, mountpoint):
    runner.run(["umount", mountpoint], check=False)


def _system_partition(runner, disk, mountpoint, readonly):
    """Monte la 1re partition qui porte un système reconnu ; retourne (part, os)."""
    candidates = [p for p in disk.partitions if p.fstype in SUPPORTED_FS]
    if not candidates:
        raise ClonixError(f"{disk.path} ne contient aucune partition Windows ou Linux")
    last_error = None
    for part in candidates:
        try:
            _mount(runner, part.path, mountpoint, readonly)
        except ClonixError as exc:
            last_error = exc
            continue
        try:
            os_type = detect_os(mountpoint) if not runner.dry_run else (
                WINDOWS if part.fstype == "ntfs" else LINUX)
        except Exception:
            os_type = None
        if os_type:
            return part, os_type
        _umount(runner, mountpoint)
    if last_error:
        raise ClonixError(
            f"Impossible de monter la partition système de {disk.path} "
            f"(chiffrée, hibernée ou endommagée ?) : {last_error}")
    raise ClonixError(f"Aucun système Windows ou Linux trouvé sur {disk.path}")


def accounts(runner, disk_path):
    """Liste des comptes du poste sur ``disk_path`` : {'os', 'partition', 'accounts'}."""
    disk = dk.get_disk(runner, disk_path)
    dk.check_readable_source(disk)
    mp = tempfile.mkdtemp(prefix="clonix-pw-")
    try:
        part, os_type = _system_partition(runner, disk, mp, readonly=True)
        try:
            if os_type == WINDOWS:
                names = windows_accounts(runner, find_sam(mp)) if not runner.dry_run else ["Administrator"]
                accts = [{"name": n, "admin": n.lower() in ("administrator", "administrateur")} for n in names]
            else:
                accts = [{"name": n, "admin": adm} for n, _, adm in
                         (linux_accounts(runner, mp) if not runner.dry_run else [("root", 0, True)])]
        finally:
            _umount(runner, mp)
        return {"os": os_type, "partition": part.path, "accounts": accts}
    finally:
        _cleanup(mp)


def reset(runner, disk_path, user, action, password=None, confirm=None):
    """Réinitialise le mot de passe de ``user`` sur le poste de ``disk_path``.

    ``action`` : "clear" (supprimer le mot de passe) ou "set" (en définir un).
    ``confirm(disk, os_type, user, action)`` est appelé avant toute écriture.
    """
    if action not in ("clear", "set"):
        raise ClonixError("Action inconnue (clear ou set)")
    if action == "set" and not password:
        raise ClonixError("Aucun mot de passe fourni")
    disk = dk.get_disk(runner, disk_path)
    dk.check_writable_target(runner, disk)

    mp = tempfile.mkdtemp(prefix="clonix-pw-")
    try:
        part, os_type = _system_partition(runner, disk, mp, readonly=False)
        try:
            if os_type == WINDOWS and action == "set":
                raise ClonixError(
                    "Définir un mot de passe précis n'est pas fiable sous Windows 10/11. "
                    "Effacez-le ici, puis choisissez-en un nouveau au premier démarrage de Windows.")
            if confirm:
                confirm(disk, os_type, user, action)
            runner.log(f"{'Suppression' if action == 'clear' else 'Changement'} du mot de passe "
                       f"de « {user} » sur {part.path} ({os_type})")
            if os_type == WINDOWS:
                windows_clear(runner, find_sam(mp), user)
            elif action == "clear":
                linux_clear(runner, mp, user)
            else:
                linux_set(runner, mp, user, password)
            runner.run(["sync"], check=False)
        finally:
            _umount(runner, mp)
        runner.log("Terminé. Le compte pourra se connecter sans mot de passe tant qu'il n'en aura pas défini un."
                   if action == "clear" else "Terminé. Nouveau mot de passe actif au prochain démarrage.")
        return {"os": os_type, "partition": part.path, "user": user, "action": action}
    finally:
        _cleanup(mp)


def _cleanup(mountpoint):
    try:
        os.rmdir(mountpoint)  # le démontage a déjà eu lieu ; le dossier est vide
    except OSError:
        pass
