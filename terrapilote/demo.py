"""Mode démonstration : disques simulés, aucune écriture réelle.

``terrapilote web --demo`` permet d'essayer l'interface sur n'importe quel PC.
"""

import json
import tempfile
import time

from terrapilote import ops
from terrapilote.runner import Reporter, Runner
from terrapilote.web import App

GIB = 1024 ** 3
MIB = 1024 ** 2

DUMPS = {
    "/dev/nvme0n1": """label: gpt
label-id: 5B3C7D10-AE11-4F7E-9D2B-8C0F4E1A2B3C
device: /dev/nvme0n1
unit: sectors
first-lba: 34
last-lba: 1000215182
sector-size: 512

/dev/nvme0n1p1 : start=2048, size=614400, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI system partition"
/dev/nvme0n1p2 : start=616448, size=32768, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE, name="Microsoft reserved partition"
/dev/nvme0n1p3 : start=649216, size=497664000, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7, name="Basic data partition"
/dev/nvme0n1p4 : start=498313216, size=2097152, type=DE94BBA4-06D1-4D40-A16A-BFD50179D6AC
""",
}


def _disk(name, size, model, tran, parts=(), mount=None, rm=False):
    return {"name": name, "path": f"/dev/{name}", "type": "disk", "size": size, "model": model,
            "serial": f"DEMO-{name.upper()}", "tran": tran, "rm": rm, "ro": False, "log-sec": 512,
            "mountpoint": None, "children": [
                {"name": f"{name}{'p' if name[-1].isdigit() else ''}{i}",
                 "path": f"/dev/{name}{'p' if name[-1].isdigit() else ''}{i}", "type": "part",
                 "size": s, "fstype": fs, "label": label, "uuid": f"uuid-{name}-{i}",
                 "mountpoint": mount if i == 1 else None}
                for i, (s, fs, label) in enumerate(parts, start=1)]}


TREE = [
    _disk("nvme0n1", 512 * GIB, "Samsung SSD 980 PRO 512GB", "nvme", [
        (300 * MIB, "vfat", "SYSTEM"), (16 * MIB, None, ""), (237 * GIB, "ntfs", "Windows"),
        (1 * GIB, "ntfs", "Recovery")]),
    _disk("sda", 1000 * GIB, "WDC WD10EZEX-08WN4A0", "sata", [(1000 * GIB - 2 * MIB, "ext4", "donnees")]),
    _disk("sdb", 500 * GIB, "Crucial MX500 500GB", "sata"),
    _disk("sdc", 32 * GIB, "SanDisk Ultra Fit", "usb", [(32 * GIB, "iso9660", "TERRAPILOTE")],
          mount="/run/live/medium", rm=True),
]


class DemoRunner(Runner):
    """Simule les outils disque ; les étapes longues durent quelques secondes."""

    speed = 4.0  # secondes par étape

    def __init__(self, reporter=None):
        super().__init__(reporter=reporter or Reporter())

    def which(self, tool):
        return f"/usr/sbin/{tool}"

    def supports(self, tool, option):
        return True

    def run(self, argv, *, input=None, capture=True, readonly=False, check=True):
        argv = [str(a) for a in argv]
        self.check_cancel()
        if argv[0] == "lsblk" and "-J" in argv:
            return json.dumps({"blockdevices": TREE})
        if argv[0] == "lsblk" and "-s" in argv:
            src = argv[-1]
            disk = src[:-2] if "p" in src[-2:] else src.rstrip("0123456789")
            return f"{src} part\n{disk} disk\n"
        if argv[0] == "findmnt":
            if argv[-1] == "/run/live/medium":
                return "/dev/sdc1\n"
            return "cifs\n" if "FSTYPE" in argv else "//nas/images\n"
        if argv[:2] == ["sfdisk", "--dump"]:
            if argv[2] in DUMPS:
                return DUMPS[argv[2]]
            return DUMPS["/dev/nvme0n1"].replace("/dev/nvme0n1p", argv[2] + "")
        if not readonly:
            self.log(f"  $ {' '.join(argv)}")
            time.sleep(0.2)
        return ""

    def pipeline(self, cmds, *, feed=None, sink=None, total=None):
        self.check_cancel()
        if feed is not None:
            while feed.read(1 << 20):
                pass
        rate = 900e6 + 300e6 * (len(cmds) % 2)
        steps = 40
        for i in range(1, steps + 1):
            if self.cancelled:
                break
            time.sleep(self.speed / steps)
            f = i / steps
            self.emit(f, rate=rate * (0.9 + 0.2 * ((i * 7) % 5) / 5), eta=self.speed * (1 - f) * 30)
        self.check_cancel()
        if sink is not None:
            data = b"DEMO" * 1024
            sink.write(data)
            return len(data)
        return 0

    def read_at(self, path, offset, size):
        return bytes(size)

    def write_at(self, path, offset, data):
        self.log(f"  écriture de la zone d'amorçage ({len(data)} octets) sur {path}")

    def popen(self, argv, *, stdin=None, stdout=None):
        raise RuntimeError("multicast simulé")


class DemoApp(App):
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="terrapilote-demo-")
        super().__init__(self.tmp.name, runner_factory=lambda rep: DemoRunner(rep), code="000000")
        seed = DemoRunner()
        seed.speed = 0
        seed.reporter = _Quiet()
        ops.capture(seed, "/dev/nvme0n1", "windows11-poste-type", self.tmp.name,
                    description="Windows 11 Pro 24H2 + Office, sysprep")
        ops.capture(seed, "/dev/nvme0n1", "salle-info-2026", self.tmp.name,
                    description="Image salle informatique")

    def state(self):
        s = super().state()
        s["storage"].update(mounted=True, fstype="cifs", source="//nas/images", free=1.7 * 1024 ** 4)
        s["demo"] = True
        return s

    def _storage(self, body):
        return {"ok": True}

    def action(self, name, body):
        if name in ("mcast-send", "mcast-recv"):
            return self._fake_multicast(name, body)
        return super().action(name, body)

    def _fake_multicast(self, name, body):
        def work(r):
            r.plan([("Diffusion multicast" if name == "mcast-send" else "Réception multicast", 10 * GIB)])
            r.log("En attente des récepteurs…" if name == "mcast-send" else "En attente de l'émetteur…")
            time.sleep(1.5)
            r.log("Transmission en cours")
            r.start_step(0)
            r.pipeline([["udp-sender"]])
            r.log("Terminé")
        title = (f"Diffusion multicast de « {body.get('image')} »" if name == "mcast-send"
                 else f"Réception multicast → {body.get('disk')}")
        if name == "mcast-recv":
            disk = self._disk(body["disk"], writable=True)
            self._confirm(disk, body)
        return self.start(name, title, work)


class _Quiet:
    def log(self, m):
        pass

    def progress(self, p):
        pass
