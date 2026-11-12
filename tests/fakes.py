"""Simulateur des outils disque pour les tests (aucun disque réel touché)."""

import io
import json

from clonix.runner import Runner

GPT_DUMP = """label: gpt
label-id: 11111111-2222-3333-4444-555555555555
device: /dev/sda
unit: sectors
first-lba: 34
last-lba: 41943006
sector-size: 512

/dev/sda1 : start=        2048, size=      204800, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=AAAAAAAA-0000-0000-0000-000000000001, name="EFI"
/dev/sda2 : start=      206848, size=    20971520, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=AAAAAAAA-0000-0000-0000-000000000002
/dev/sda3 : start=    21178368, size=     2097152, type=0657FD6D-A4AB-43C4-84E5-0933C84B4F4F, uuid=AAAAAAAA-0000-0000-0000-000000000003
/dev/sda4 : start=    23275520, size=     1048576, type=E6D6D379-F507-44C2-A23C-238F2A3DF928, uuid=AAAAAAAA-0000-0000-0000-000000000004
"""

DOS_DUMP = """label: dos
label-id: 0x1234abcd
device: /dev/sdb
unit: sectors
sector-size: 512

/dev/sdb1 : start=        2048, size=     1024000, type=7, bootable
/dev/sdb2 : start=     1026048, size=     4096000, type=5
/dev/sdb5 : start=     1028096, size=     4093952, type=83
"""

GIB = 1024 ** 3


def lsblk_disk(name, size, children=(), mount=None, tran="sata", rm=False, model="DISQUE"):
    return {
        "name": name, "path": f"/dev/{name}", "type": "disk", "size": size, "fstype": None,
        "mountpoint": mount, "model": model, "serial": f"SN-{name}", "tran": tran,
        "rm": rm, "ro": False, "log-sec": 512, "children": list(children),
    }


def lsblk_part(name, size, fstype=None, mount=None, uuid="", label="", children=()):
    return {
        "name": name, "path": f"/dev/{name}", "type": "part", "size": size, "fstype": fstype,
        "mountpoint": mount, "uuid": uuid, "label": label, "children": list(children),
    }


def default_tree():
    return [
        lsblk_disk("sda", 20 * GIB, [
            lsblk_part("sda1", 100 * 1024 * 1024, "vfat", label="EFI"),
            lsblk_part("sda2", 10 * GIB, "ext4", uuid="ext4-uuid"),
            lsblk_part("sda3", GIB, "BitLocker"),
            lsblk_part("sda4", 512 * 1024 * 1024, "swap", uuid="swap-uuid", label="sw"),
        ]),
        lsblk_disk("sdc", 32 * GIB),
        lsblk_disk("sdd", 8 * GIB),
        lsblk_disk("sde", 32 * GIB, [lsblk_part("sde1", 32 * GIB, "ext4", mount="/home")]),
        lsblk_disk("sdu", 16 * GIB, [lsblk_part("sdu1", 16 * GIB, "iso9660", mount="/run/live/medium")],
                   tran="usb", rm=True),
    ]


class FakeRunner(Runner):
    def __init__(self, tree=None, dumps=None, tools=None, live_disk=None, dry_run=False):
        super().__init__(dry_run=dry_run, out=io.StringIO())
        self.tree = tree if tree is not None else default_tree()
        self.dumps = dumps or {"/dev/sda": GPT_DUMP}
        self.tools = set(tools if tools is not None else
                         ["zstd", "gzip", "partclone.fat", "partclone.extfs", "partclone.ntfs"])
        self.live_disk = live_disk
        self.calls = []  # ("run", argv, input) / ("pipe", cmds) / ("write", path, offset, data)
        self.disk_bytes = {}  # contenu lu par read_at
        self.mount_fstype = "ext4"
        self.help_text = {}  # outil -> sortie de --help simulée

    def supports(self, tool, option):
        return option in self.help_text.get(tool, "")

    # -- outils
    def which(self, tool):
        return f"/usr/sbin/{tool}" if tool in self.tools else None

    def run(self, argv, *, input=None, capture=True, readonly=False, check=True):
        argv = [str(a) for a in argv]
        if self.dry_run and not readonly:
            self.calls.append(("dry", argv, input))
            return ""
        self.calls.append(("run", argv, input))
        if argv[0] == "lsblk" and "-J" in argv:
            return json.dumps({"blockdevices": self.tree})
        if argv[0] == "lsblk" and "-s" in argv:
            src = argv[-1]
            disk = src.rstrip("0123456789")
            return f"{src} part\n{disk} disk\n"
        if argv[0] == "findmnt":
            target = argv[-1]
            if "FSTYPE" in argv:
                return self.mount_fstype + "\n"
            if target == "/run/live/medium" and self.live_disk:
                return f"{self.live_disk}1\n"
            return "tmpfs\n"
        if argv[:2] == ["sfdisk", "--dump"]:
            return self.dumps[argv[2]]
        return ""

    def pipeline(self, cmds, *, feed=None, sink=None, total=None):
        cmds = [[str(a) for a in c] for c in cmds]
        self.calls.append(("pipe", cmds))
        if self.dry_run:
            return 0
        data = b""
        if feed is not None:
            while True:
                buf = feed.read(65536)
                if not buf:
                    break
                data += buf
        if sink is not None:
            payload = ("DATA:" + " ".join(cmds[0])).encode()
            sink.write(payload)
            return len(payload)
        self.last_fed = data
        return 0

    geometry = None  # {chemin de partition: (début, taille)} ; None = pas de contrôle

    def partition_geometry(self, part_path):
        return (self.geometry or {}).get(part_path)

    def read_at(self, path, offset, size):
        data = self.disk_bytes.get(path, bytes(range(256)) * 4096)
        return data[offset:offset + size]

    def write_at(self, path, offset, data):
        self.calls.append(("write", path, offset, bytes(data)))

    # -- aides pour les assertions
    def commands(self, kind="run"):
        return [c[1] for c in self.calls if c[0] == kind]
