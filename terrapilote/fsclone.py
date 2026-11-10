"""Choix de la méthode de copie de chaque partition et commandes associées.

- ``partclone`` : ne copie que les blocs utilisés (rapide, image compacte) ;
- ``dd``        : copie brute, pour tout ce que partclone ne sait pas lire
                  (BitLocker, LUKS, LVM, partitions sans système de fichiers) ;
- ``swap``      : rien à copier, la swap est recréée avec le même UUID ;
- ``skip``      : partition étendue MBR (simple conteneur, recréée par sfdisk).
"""

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
DD_BS = "4M"

COMPRESSORS = {
    "zstd": (".zst", lambda level: ["zstd", "-q", "-T0", f"-{level}", "-c"], ["zstd", "-q", "-d", "-c"]),
    "gzip": (".gz", lambda level: ["gzip", f"-{min(level, 9)}", "-c"], ["gzip", "-d", "-c"]),
    "none": ("", None, None),
}


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


def read_cmd(mode, tool, device):
    """Commande qui écrit le contenu de ``device`` sur sa sortie standard."""
    if mode == "partclone":
        return [tool, "-c", "-s", device, "-o", "-"]
    return ["dd", f"if={device}", f"bs={DD_BS}", "status=progress"]


def write_cmd(mode, tool, device):
    """Commande qui lit son entrée standard et l'écrit sur ``device``."""
    if mode == "partclone":
        return [tool, "-r", "-s", "-", "-o", device]
    return ["dd", f"of={device}", f"bs={DD_BS}", "iflag=fullblock", "conv=fsync", "status=progress"]


def copy_cmd(mode, tool, src, dst):
    """Copie directe partition → partition."""
    if mode == "partclone":
        return [tool, "-b", "-s", src, "-o", dst]
    return ["dd", f"if={src}", f"of={dst}", f"bs={DD_BS}", "conv=fsync", "status=progress"]


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
