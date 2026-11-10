"""Format d'image Clonix.

Une image est un dossier :

    clonix.json   manifeste (disque source, partitions, sommes SHA-256)
    table.sfdisk       table de partitions (sfdisk --dump)
    boot.img           zone d'amorçage (avant la 1re partition)
    p1.partclone.zst   données de chaque partition, compressées
    ...

L'ordre des fichiers est fixe (voir ``stream_order``) : c'est celui de
l'envoi multicast, que les récepteurs traitent au fil de l'eau.
"""

import datetime
import hashlib
import json
import os
import tarfile

from clonix import __version__
from clonix.errors import ClonixError

MANIFEST = "clonix.json"
TABLE = "table.sfdisk"
BOOT = "boot.img"
FORMAT = 1
STREAM_BUF = 4 * 1024 * 1024  # gros blocs : moins d'appels en Python
DEFAULT_DIR = os.environ.get("CLONIX_IMAGES", "/srv/clonix/images")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_manifest(name, disk, table, description=""):
    return {
        "format": FORMAT,
        "tool": f"clonix {__version__}",
        "name": name,
        "description": description,
        "created": now_iso(),
        "source": {
            "path": disk.path,
            "model": disk.model,
            "serial": disk.serial,
            "size": disk.size,
            "sector_size": disk.sector_size,
        },
        "table": {"label": table.label, "required_bytes": table.required_bytes()},
        "files": {},
        "partitions": [],
    }


def stream_order(manifest):
    names = [MANIFEST, TABLE, BOOT]
    names += [p["file"] for p in manifest["partitions"] if p.get("file")]
    return names


def resolve(name, image_dir=None):
    if os.sep in name or name.startswith("."):
        return os.path.abspath(name)
    return os.path.join(image_dir or DEFAULT_DIR, name)


def load_manifest(fileobj):
    try:
        data = json.load(fileobj)
    except ValueError as exc:
        raise ClonixError(f"Manifeste d'image illisible : {exc}") from None
    if data.get("format") != FORMAT:
        raise ClonixError(f"Format d'image non pris en charge : {data.get('format')}")
    return data


def list_images(image_dir):
    images = []
    if not os.path.isdir(image_dir):
        return images
    for entry in sorted(os.listdir(image_dir)):
        path = os.path.join(image_dir, entry, MANIFEST)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    m = load_manifest(f)
            except ClonixError:
                continue
            size = sum(f.get("size", 0) for f in m["files"].values())
            images.append((entry, m, size))
    return images


class HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.hash = hashlib.sha256()
        self.size = 0

    def read(self, n=-1):
        buf = self.raw.read(n)
        self.hash.update(buf)
        self.size += len(buf)
        return buf


class VerifyingReader(HashingReader):
    """Lit un fichier et lève une erreur en fin de lecture si la somme diffère."""

    def __init__(self, raw, name, expected):
        super().__init__(raw)
        self.name = name
        self.expected = expected

    def read(self, n=-1):
        buf = super().read(n)
        if not buf and self.expected:
            if self.hash.hexdigest() != self.expected.get("sha256") or self.size != self.expected.get("size"):
                raise ClonixError(f"Image corrompue : {self.name} ne correspond pas à sa somme SHA-256")
        return buf


class HashingWriter:
    def __init__(self, raw):
        self.raw = raw
        self.hash = hashlib.sha256()
        self.size = 0

    def write(self, buf):
        self.hash.update(buf)
        self.size += len(buf)
        return self.raw.write(buf)

    def record(self):
        return {"size": self.size, "sha256": self.hash.hexdigest()}


class DirSource:
    """Image lue depuis un dossier."""

    def __init__(self, path):
        if not os.path.isfile(os.path.join(path, MANIFEST)):
            raise ClonixError(f"Aucune image Clonix dans {path}")
        self.path = path

    def open(self, name):
        return open(os.path.join(self.path, name), "rb")

    def close(self):
        pass


class TarStreamSource:
    """Image reçue en flux tar (multicast) : les fichiers arrivent dans l'ordre."""

    def __init__(self, fileobj):
        self.tar = tarfile.open(fileobj=fileobj, mode="r|", bufsize=STREAM_BUF)
        self.members = iter(self.tar)

    def open(self, name):
        member = next(self.members, None)
        if member is None:
            raise ClonixError(f"Flux interrompu : {name} attendu")
        if member.name != name or not member.isfile():
            raise ClonixError(f"Flux inattendu : {member.name} reçu, {name} attendu")
        return self.tar.extractfile(member)

    def close(self):
        self.tar.close()


def write_tar_stream(image_path, manifest, fileobj):
    """Écrit l'image en flux tar, dans l'ordre attendu par ``TarStreamSource``."""
    with tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT, bufsize=STREAM_BUF) as tar:
        for name in stream_order(manifest):
            tar.add(os.path.join(image_path, name), arcname=name, recursive=False)


def verify(image_path):
    """Vérifie toutes les sommes. Retourne la liste des fichiers en erreur."""
    with open(os.path.join(image_path, MANIFEST), "rb") as f:
        manifest = load_manifest(f)
    bad = []
    for name, rec in manifest["files"].items():
        path = os.path.join(image_path, name)
        if not os.path.isfile(path):
            bad.append((name, "manquant"))
            continue
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while True:
                buf = f.read(4 * 1024 * 1024)
                if not buf:
                    break
                h.update(buf)
                size += len(buf)
        if size != rec["size"] or h.hexdigest() != rec["sha256"]:
            bad.append((name, "somme SHA-256 différente"))
    return manifest, bad
