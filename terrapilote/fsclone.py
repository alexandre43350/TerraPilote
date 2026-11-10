"""Choix de la méthode de copie de chaque partition et commandes associées.

- ``partclone`` : ne copie que les blocs utilisés (rapide, image compacte) ;
- ``dd``        : copie brute, pour tout ce que partclone ne sait pas lire
                  (BitLocker, LUKS, LVM, partitions sans système de fichiers) ;
- ``swap``      : rien à copier, la swap est recréée avec le même UUID ;
- ``skip``      : partition étendue MBR (simple conteneur, recréée par sfdisk).
"""

import shutil

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


def _zstd(level):
    # « adapt » : zstd ajuste lui-même le niveau au débit de sortie (réseau lent
    # → compression plus forte, donc moins d'octets à transférer).
    lvl = ["--adapt=min=1,max=9"] if level == "adapt" else [f"-{level}"]
    return ["zstd", "-q", "-T0", *lvl, "-c"]


def _gzip(level):
    tool = "pigz" if shutil.which("pigz") else "gzip"  # pigz : gzip multicœur
    return [tool, f"-{min(int(level) if level != 'adapt' else 6, 9)}", "-c"]


COMPRESSORS = {
    "zstd": (".zst", _zstd, ["zstd", "-q", "-d", "-c"]),
    "gzip": (".gz", _gzip, ["gzip", "-d", "-c"]),
    "none": ("", None, None),
}


def choose_level(level, image_fstype):
    """Niveau « auto » : zstd -1 (le plus rapide) sur un disque local, adaptatif
    sur un partage réseau où c'est le débit du réseau qui limite."""
    if level != "auto":
        return level
    return "adapt" if image_fstype in NETWORK_FS else 1


def partclone_flags(supports, tool, capture=False):
    """Options de vitesse, ajoutées seulement si la version de partclone les connaît."""
    flags = []
    if supports(tool, "--buffer_size"):
        flags += ["-z", str(PARTCLONE_BUFFER)]
    if capture and supports(tool, "--checksum-mode"):
        flags += ["-a0"]  # pas de CRC par bloc : TerraPilote calcule déjà un SHA-256
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
