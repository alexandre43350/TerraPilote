"""Déploiement sur un disque plus grand (agrandissement) ou plus petit (image WIM)."""

import os
import shutil
import tempfile
import unittest

from clonix import fsclone as fc
from clonix import image as im
from clonix import ops
from clonix import partitions as pt
from clonix.errors import ClonixError
from clonix.runner import parse_wimlib

from tests.fakes import DOS_DUMP, GIB, FakeRunner, lsblk_disk, lsblk_part

MIB = 1024 ** 2
SECTORS = 256 * GIB // 512
REC_START = SECTORS - 2097152 - 2048
WIN_DUMP = f"""label: gpt
label-id: 11111111-2222-3333-4444-555555555555
device: /dev/sdw
unit: sectors
first-lba: 34
last-lba: {SECTORS - 34}
sector-size: 512

/dev/sdw1 : start=2048, size=204800, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=AAAAAAAA-0000-0000-0000-000000000001, name="EFI system partition"
/dev/sdw2 : start=206848, size=32768, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE, uuid=AAAAAAAA-0000-0000-0000-000000000002
/dev/sdw3 : start=239616, size={REC_START - 239616}, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7, uuid=AAAAAAAA-0000-0000-0000-000000000003, name="Basic data partition"
/dev/sdw4 : start={REC_START}, size=2097152, type=DE94BBA4-06D1-4D40-A16A-BFD50179D6AC, uuid=AAAAAAAA-0000-0000-0000-000000000004, attrs="RequiredPartition GUID:63"
"""
TOOLS = ["zstd", "partclone.fat", "partclone.ntfs", "wimlib-imagex", "mkntfs", "ntfsresize"]


def win_tree():
    return [
        lsblk_disk("sdw", 256 * GIB, [
            lsblk_part("sdw1", 100 * MIB, "vfat", label="SYSTEM"),
            lsblk_part("sdw2", 16 * MIB),
            lsblk_part("sdw3", (REC_START - 239616) * 512, "ntfs", label="Windows"),
            lsblk_part("sdw4", GIB, "ntfs", label="Recovery"),
        ]),
        lsblk_disk("sdx", 128 * GIB),
        lsblk_disk("sdy", 512 * GIB),
        lsblk_disk("sdz", 32 * GIB),
    ]


class WinRunner(FakeRunner):
    used_mb = 40000  # « Space in use » rapporté par ntfsresize

    def __init__(self, tools=TOOLS):
        super().__init__(tree=win_tree(), dumps={"/dev/sdw": WIN_DUMP}, tools=tools)

    def run(self, argv, **kw):
        out = super().run(argv, **kw)
        if argv[0] == "ntfsresize" and "--info" in argv:
            return f"Space in use       : {self.used_mb} MB (16.2%)\n"
        return out


def entries(text):
    return {e.number: (e.start, e.size) for e in pt.parse_dump(text).entries}


class RelayoutTest(unittest.TestCase):
    def test_grow_moves_recovery_to_the_end(self):
        text, size = pt.relayout(WIN_DUMP, 512 * GIB, 3)
        e = entries(text)
        end = 512 * GIB // 512 - 34
        self.assertEqual(e[1], (2048, 204800))
        self.assertEqual(e[2], (206848, 32768))
        self.assertEqual(e[3][0], 239616)
        self.assertEqual(e[4][1], 2097152)
        self.assertEqual(e[4][0], e[3][0] + e[3][1])
        self.assertEqual(e[4][0] % 2048, 0)
        self.assertLessEqual(e[4][0] + e[4][1], end)
        self.assertGreater(e[4][0] + e[4][1], end - 2048)
        self.assertEqual(size, e[3][1] * 512)
        # identifiants, types et attributs gardés
        self.assertIn("uuid=AAAAAAAA-0000-0000-0000-000000000004", text)
        self.assertIn('attrs="RequiredPartition GUID:63"', text)
        self.assertIn("type=DE94BBA4-06D1-4D40-A16A-BFD50179D6AC", text)

    def test_shrink_and_minimum(self):
        text, size = pt.relayout(WIN_DUMP, 128 * GIB, 3, min_bytes=50 * GIB)
        self.assertLess(size, 128 * GIB)
        self.assertLessEqual(pt.parse_dump(text).required_bytes(), 128 * GIB)
        with self.assertRaisesRegex(ClonixError, "trop petit"):
            pt.relayout(WIN_DUMP, 32 * GIB, 3, min_bytes=50 * GIB)

    def test_extended_mbr_refused(self):
        with self.assertRaisesRegex(ClonixError, "étendues"):
            pt.relayout(DOS_DUMP, 64 * GIB, 1)


class ResizeDeployTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def capture(self, **kw):
        r = WinRunner()
        path, manifest = ops.capture(r, "/dev/sdw", "win", self.dir, **kw)
        return r, path, manifest

    def sfdisk_script(self, r):
        return next(c[2] for c in r.calls if c[0] == "run" and c[1][0] == "sfdisk" and "--dump" not in c[1])

    def test_resizable_capture_uses_wim_for_windows_partition(self):
        r, path, m = self.capture(resizable=True)
        modes = {p["number"]: p["mode"] for p in m["partitions"]}
        self.assertEqual(modes, {1: "partclone", 2: "dd", 3: "wim", 4: "partclone"})
        self.assertIn("p3.wim.zst", os.listdir(path))
        wim = next(c for c in r.commands("pipe") if c[0][0] == fc.WIMLIB)
        self.assertEqual(wim[0][:6], ["wimlib-imagex", "capture", "/dev/sdw3", "-", "Clonix", "--pipable"])
        rec = m["partitions"][2]
        self.assertEqual(rec["min_size"], -(-(int(40e9 * 1.1) + 4 * GIB) // MIB) * MIB)
        self.assertLess(m["table"]["min_bytes"], 64 * GIB)
        self.assertEqual(im.verify(path)[1], [])

    def test_wimlib_total_preferred_for_minimum(self):
        class Measured(WinRunner):
            def pipeline(self, cmds, **kw):
                if cmds[0][0] == fc.WIMLIB:
                    self.last_wim_total = 20 * 10 ** 9
                return super().pipeline(cmds, **kw)

        r = Measured()
        _, m = ops.capture(r, "/dev/sdw", "win", self.dir, resizable=True)
        self.assertEqual(m["partitions"][2]["min_size"], -(-(int(20e9 * 1.1) + 4 * GIB) // MIB) * MIB)

    def test_wim_image_deploys_on_smaller_disk(self):
        _, path, _ = self.capture(resizable=True)
        r = WinRunner()
        ops.deploy(r, im.DirSource(path), "/dev/sdx", self.dir)
        e = entries(self.sfdisk_script(r))
        self.assertLessEqual((e[4][0] + e[4][1]) * 512, 128 * GIB)
        self.assertIn(["mkntfs", "--quick", "--force", "--label", "Windows", "/dev/sdx3"], r.commands("run"))
        apply = next(c for c in r.commands("pipe") if c[-1][0] == fc.WIMLIB)
        self.assertEqual(apply, [["zstd", "-q", "-d", "-c"], ["wimlib-imagex", "apply", "-", "1", "/dev/sdx3"]])
        recovery = next(c for c in r.commands("pipe") if c[-1][-1] == "/dev/sdx4")
        self.assertEqual(recovery[-1][0], "partclone.ntfs")

    def test_wim_image_refuses_disk_below_data_size(self):
        _, path, _ = self.capture(resizable=True)
        r = WinRunner()
        with self.assertRaisesRegex(ClonixError, "trop petit"):
            ops.deploy(r, im.DirSource(path), "/dev/sdz", self.dir, confirm=lambda m, d: self.fail())
        self.assertFalse([c for c in r.calls if c[0] in ("write", "pipe")])

    def test_block_image_on_smaller_disk_explains_option(self):
        _, path, _ = self.capture()
        r = WinRunner()
        with self.assertRaisesRegex(ClonixError, "Image adaptable aux disques plus petits"):
            ops.deploy(r, im.DirSource(path), "/dev/sdx", self.dir)

    def test_block_image_grows_on_bigger_disk(self):
        _, path, _ = self.capture()
        r = WinRunner()
        ops.deploy(r, im.DirSource(path), "/dev/sdy", self.dir)
        e = entries(self.sfdisk_script(r))
        self.assertGreater(e[3][1] * 512, 500 * GIB)
        grow = [c for c in r.calls if c[0] == "run" and c[1][0] == "ntfsresize"]
        self.assertEqual(grow[0][1], ["ntfsresize", "--force", "--no-progress-bar", "/dev/sdy3"])
        self.assertEqual(grow[0][2], "y\n")
        self.assertFalse([c for c in r.commands("run") if c[0] == "mkntfs"])

    def test_no_expand_keeps_layout(self):
        _, path, _ = self.capture()
        r = WinRunner()
        ops.deploy(r, im.DirSource(path), "/dev/sdy", self.dir, expand=False)
        self.assertEqual(entries(self.sfdisk_script(r)), entries(WIN_DUMP))
        self.assertFalse([c for c in r.commands("run") if c[0] == "ntfsresize"])

    def test_missing_grow_tool_keeps_layout(self):
        _, path, _ = self.capture()
        r = WinRunner(tools=["zstd", "partclone.fat", "partclone.ntfs"])
        ops.deploy(r, im.DirSource(path), "/dev/sdy", self.dir)
        self.assertEqual(entries(self.sfdisk_script(r)), entries(WIN_DUMP))

    def test_wim_parser(self):
        self.assertEqual(parse_wimlib("Archiving file data: 120 MiB of 900 MiB (13%) done"),
                         (120 * MIB, 900 * MIB))
        self.assertEqual(parse_wimlib("\rExtracting file data: 2 GiB of 10 GiB (20%) done"),
                         (2 * GIB, 10 * GIB))
        self.assertEqual(parse_wimlib("300 MiB of 600 MiB (uncompressed) written (50% done)"),
                         (300 * MIB, 600 * MIB))
        self.assertIsNone(parse_wimlib("Scanning \"/dev/sda3\""))
        self.assertIsNone(parse_wimlib("Applying metadata to files: 1200 of 5000 (24%) done"))


if __name__ == "__main__":
    unittest.main()
