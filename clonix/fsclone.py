"""Choix de la méthode de copie de chaque partition et commandes associées.

- ``partclone`` : ne copie que les blocs utilisés (rapide, image compacte) ;
- ``dd``        : copie brute, pour tout ce que partclone ne sait pas lire
                  (BitLocker, LUKS, LVM, partitions sans système de fichiers) ;
- ``swap``      : rien à copier, la swap est recréée avec le même UUID ;
- ``skip``      : partition étendue MBR (simple conteneur, recréée par sfdisk).
"""

import shutil
import subprocess

PARTCLONE_TOOLS = {
    "ext2": "partclone.extfs",
    "ext3": "partclone.extfs",
    "ext4": "partclone.extfs",
    "ntfs": "partclone.ntfs",
    "vfat": "partclone.fat",
    "exfat": "partclone.exfat",
    "btrfs": "partclone.btrfs",
    "xfs": "partclone.xfs",
    "f2fs": "partclone.f2fs",
    "hfsplus": "partclone.hfsp",
}
EXTENDED_TYPES = {"5", "f", "85"}
DD_BS = "16M"
PARTCLONE_BUFFER = 16 * 1024 * 1024  # défaut partclone : 1 Mio
NETWORK_FS = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"}


FAST_LEVEL = 3  # zstd --fast=3 : bien plus rapide que -1, image un peu plus grosse
_adapt_min_cache = []


def adapt_min_level():
    """Niveau le plus bas autorisé en mode adaptatif.

    Les niveaux « rapides » (négatifs) laissent zstd alléger la compression quand
    c'est le processeur qui limite (PC peu puissant). On vérifie une fois que le
    zstd installé les accepte ; sinon on reste au niveau 1.
    """
    if not _adapt_min_cache:
        level = 1
        if shutil.which("zstd"):
            try:
                ok = subprocess.run(["zstd", "-q", f"--adapt=min=-{FAST_LEVEL},max=9", "-c"],
                                    input=b"clonix", stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=10).returncode == 0
                level = -FAST_LEVEL if ok else 1
            except (OSError, subprocess.SubprocessError):
                level = 1
        _adapt_min_cache.append(level)
    return _adapt_min_cache[0]


def _zstd(level):
    # « adapt » : zstd ajuste lui-même le niveau. Sortie lente (réseau, USB) →
    # compression plus forte, donc moins d'octets à écrire ; processeur lent →
    # niveaux « rapides », pour ne pas freiner la copie.
    # « fast » : niveau rapide fixe (option « Ultra-rapide »).
    if level == "adapt":
        lvl = [f"--adapt=min={adapt_min_level()},max=9"]
    elif level == "fast":
        lvl = [f"--fast={FAST_LEVEL}"]
    else:
        lvl = [f"-{level}"]
    return ["zstd", "-q", "-T0", *lvl, "-c"]


def _gzip(level):
    tool = "pigz" if shutil.which("pigz") else "gzip"  # pigz : gzip multicœur
    value = {"adapt": 6, "fast": 1}.get(level, level)
    return [tool, f"-{min(int(value), 9)}", "-c"]


COMPRESSORS = {
    "zstd": (".zst", _zstd, ["zstd", "-q", "-d", "-c"]),
    "gzip": (".gz", _gzip, ["gzip", "-d", "-c"]),
    "none": ("", None, None),
}


def choose_level(level, image_fstype, on_usb=False):
    """Niveau « auto ».

    - disque interne : zstd -1, le plus rapide ;
    - partage réseau ou support USB : niveau adaptatif. C'est alors l'écriture
      qui limite (réseau, ou SSD USB dont l'écriture chute une fois son cache
      plein) : zstd compresse davantage quand la sortie ralentit, donc écrit
      moins d'octets, et revient à un niveau rapide quand elle suit.
    """
    if level != "auto":
        return level
    return "adapt" if image_fstype in NETWORK_FS or on_usb else 1


def partclone_flags(supports, tool, capture=False):
    """Options de vitesse, ajoutées seulement si la version de partclone les connaît."""
    flags = []
    if supports(tool, "--buffer_size"):
        flags += ["-z", str(PARTCLONE_BUFFER)]
    if capture and supports(tool, "--checksum-mode"):
        flags += ["-a0"]  # pas de CRC par bloc : Clonix calcule déjà un SHA-256
    return flags


def plan_mode(part, table_type, which):
    """Méthode à utiliser pour une partition (``which`` = shutil.which)."""
    if table_type.lower() in EXTENDED_TYPES:
        return "skip", None
    if part.fstype == "swap":
        return "swap", None
    tool = PARTCLONE_TOOLS.get(part.fstype)
    if tool and which(tool):
        return "partclone", tool
    return "dd", None


def read_cmd(mode, tool, device, flags=()):
    """Commande qui écrit le contenu de ``device`` sur sa sortie standard."""
    if mode == "partclone":
        return [tool, "-c", *flags, "-s", device, "-o", "-"]
    return ["dd", f"if={device}", f"bs={DD_BS}", "status=progress"]


def write_cmd(mode, tool, device, flags=()):
    """Commande qui lit son entrée standard et l'écrit sur ``device``."""
    if mode == "partclone":
        return [tool, "-r", *flags, "-s", "-", "-o", device]
    return ["dd", f"of={device}", f"bs={DD_BS}", "iflag=fullblock", "conv=fsync", "status=progress"]


def copy_cmds(mode, tool, src, dst, flags=()):
    """Copie directe partition → partition.

    En brut, deux ``dd`` reliés par un tube : la lecture du disque source et
    l'écriture sur la cible se font en parallèle au lieu de s'alterner.
    """
    if mode == "partclone":
        return [[tool, "-b", *flags, "-s", src, "-o", dst]]
    return [
        ["dd", f"if={src}", f"bs={DD_BS}", "status=progress"],
        ["dd", f"of={dst}", f"bs={DD_BS}", "iflag=fullblock", "conv=fsync"],
    ]


def mkswap_cmd(device, uuid="", label=""):
    cmd = ["mkswap"]
    if uuid:
        cmd += ["-U", uuid]
    if label:
        cmd += ["-L", label]
    return cmd + [device]


def compress_cmd(compression, level):
    return COMPRESSORS[compression][1](level) if COMPRESSORS[compression][1] else None


def decompress_cmd(compression):
    return COMPRESSORS[compression][2]


def extension(compression):
    return COMPRESSORS[compression][0]


# ------------------------------------------------- copie fichier par fichier (WIM)
#
# Une copie « blocs utilisés » ne peut revenir que dans une partition au moins
# aussi grande que l'originale : Windows éparpille ses données sur toute la
# partition. Pour déployer sur un disque plus petit, la partition Windows est
# enregistrée fichier par fichier au format WIM (celui des images d'installation
# de Microsoft) avec wimlib, qui lit et écrit le NTFS directement, sans montage,
# en gardant droits, flux secondaires, liens et noms courts.

WIMLIB = "wimlib-imagex"
WIM_EXCLUDE = """[ExclusionList]
\\$ntfs.log
\\hiberfil.sys
\\pagefile.sys
\\swapfile.sys
\\System Volume Information
\\Windows\\CSC
"""
WIM_FSTYPES = {"ntfs"}


def wim_capture_cmd(device, config):
    """Capture du volume NTFS ``device`` vers la sortie standard (flux WIM « pipable »).

    Pas de compression dans le WIM : c'est zstd, multicœur, qui compresse ensuite.
    """
    return [WIMLIB, "capture", device, "-", "Clonix", "--pipable", "--compress=none",
            f"--config={config}"]


def wim_apply_cmd(device):
    """Écrit le flux WIM lu sur l'entrée standard dans le volume NTFS ``device``."""
    return [WIMLIB, "apply", "-", "1", device]


def mkntfs_cmd(device, label=""):
    cmd = ["mkntfs", "--quick", "--force"]
    if label:
        cmd += ["--label", label]
    return cmd + [device]


def grow_cmds(fstype, device):
    """Commandes qui agrandissent le système de fichiers à la taille de sa partition."""
    if fstype == "ntfs":
        return [(["ntfsresize", "--force", "--no-progress-bar", device], "y\n", True)]
    if fstype in ("ext2", "ext3", "ext4"):
        # e2fsck renvoie 1 quand il a corrigé quelque chose : ce n'est pas une erreur.
        return [(["e2fsck", "-f", "-y", device], None, False), (["resize2fs", device], None, True)]
    return []


GROWABLE = {"ntfs", "ext2", "ext3", "ext4"}
