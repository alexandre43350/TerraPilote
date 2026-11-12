import io
import json
import os
import tempfile
import unittest
from unittest import mock

from clonix import disks as dk
from clonix import fsclone as fc
from clonix import image as im
from clonix import ops
from clonix import partitions as pt
from clonix.cli import Cancelled, ask_erase
from clonix.errors import CommandError, ClonixError
from clonix.runner import Runner

from tests.fakes import DOS_DUMP, GIB, GPT_DUMP, FakeRunner, default_tree, lsblk_disk


class DisksTest(unittest.TestCase):
    def test_part_path(self):
        self.assertEqual(dk.part_path("/dev/sda", 2), "/dev/sda2")
        self.assertEqual(dk.part_path("/dev/nvme0n1", 1), "/dev/nvme0n1p1")
        self.assertEqual(dk.part_path("/dev/mmcblk0", 3), "/dev/mmcblk0p3")
        self.assertEqual(dk.part_number("/dev/nvme0n1p12"), 12)

    def test_parse_lsblk(self):
        tree = default_tree() + [{"name": "loop0", "type": "loop", "size": 1}, {"name": "zram0", "type": "disk"}]
        disks = dk.parse_lsblk(json.dumps({"blockdevices": tree}))
        self.assertEqual([d.path for d in disks], ["/dev/sda", "/dev/sdc", "/dev/sdd", "/dev/sde", "/dev/sdu"])
        sda = disks[0]
        self.assertEqual([p.number for p in sda.partitions], [1, 2, 3, 4])
        self.assertEqual(sda.partitions[1].fstype, "ext4")
        self.assertEqual(disks[3].mountpoints, ["/home"])
        self.assertTrue(disks[4].removable)

    def test_old_lsblk_string_values(self):
        node = {"name": "sda", "type": "disk", "size": "1000", "rm": "1", "ro": "0", "log-sec": "4096"}
        d = dk.parse_lsblk(json.dumps({"blockdevices": [node]}))[0]
        self.assertEqual((d.size, d.removable, d.readonly, d.sector_size), (1000, True, False, 4096))

    def test_mounted_lvm_child_marks_disk_busy(self):
        part = {"name": "sda1", "type": "part", "size": 1, "children": [
            {"name": "vg-root", "type": "lvm", "mountpoint": "/"}]}
        d = dk.parse_lsblk(json.dumps({"blockdevices": [lsblk_disk("sda", 10, [part])]}))[0]
        self.assertEqual(d.mountpoints, ["/"])

    def test_live_usb_is_protected(self):
        r = FakeRunner(live_disk="/dev/sdu")
        self.assertIn("/dev/sdu", dk.protected_disks(r))
        with self.assertRaisesRegex(ClonixError, "clé USB"):
            dk.check_writable_target(r, dk.get_disk(r, "/dev/sdu"))

    def test_mounted_target_refused(self):
        r = FakeRunner()
        with self.assertRaisesRegex(ClonixError, "utilisé"):
            dk.check_writable_target(r, dk.get_disk(r, "/dev/sde"))


class PartitionsTest(unittest.TestCase):
    def test_parse_gpt(self):
        t = pt.parse_dump(GPT_DUMP)
        self.assertEqual(t.label, "gpt")
        self.assertEqual([e.number for e in t.entries], [1, 2, 3, 4])
        self.assertEqual(t.first_start, 2048)
        self.assertEqual(t.required_bytes(), (23275520 + 1048576 + 33) * 512)
        self.assertEqual(t.boot_area_bytes(), 2048 * 512)
        self.assertEqual(t.boot_regions(2048 * 512), [(0, 440), (34 * 512, 2048 * 512)])

    def test_parse_dos(self):
        t = pt.parse_dump(DOS_DUMP)
        self.assertEqual(t.label, "dos")
        self.assertEqual([e.number for e in t.entries], [1, 2, 5])
        self.assertEqual(t.boot_regions(2048 * 512), [(0, 440), (512, 2048 * 512)])

    def test_unsupported_label(self):
        with self.assertRaises(ClonixError):
            pt.parse_dump("label: sun\n")

    def test_rewrite_for_nvme_target(self):
        out = pt.rewrite_dump(GPT_DUMP, "/dev/nvme0n1")
        self.assertNotIn("device:", out)
        self.assertNotIn("last-lba", out)
        self.assertIn("label-id: 11111111", out)
        self.assertIn("/dev/nvme0n1p1 : start=        2048", out)
        self.assertIn('name="EFI"', out)
        self.assertIn("uuid=AAAAAAAA-0000-0000-0000-000000000002", out)

    def test_rewrite_new_uuids(self):
        out = pt.rewrite_dump(GPT_DUMP, "/dev/sdc", new_uuids=True)
        self.assertNotIn("uuid=", out)
        self.assertNotIn("label-id", out)
        self.assertIn('type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"', out)
        out = pt.rewrite_dump(DOS_DUMP, "/dev/sdc", new_uuids=True)
        self.assertIn("/dev/sdc5 : start=     1028096, size=     4093952, type=83", out)
        self.assertIn("type=7, bootable", out)

    def test_write_table_checks_size(self):
        r = FakeRunner()
        small = dk.get_disk(r, "/dev/sdd")
        with self.assertRaisesRegex(ClonixError, "trop petit"):
            pt.write_table(r, small, GPT_DUMP)
        self.assertFalse(r.commands("run")[1:])  # rien n'a été écrit après lsblk


class FsCloneTest(unittest.TestCase):
    def test_plan_mode(self):
        which = {"partclone.extfs", "partclone.ntfs"}.__contains__
        P = lambda fs: dk.Partition("/dev/x1", 1, 1, fs)  # noqa: E731
        self.assertEqual(fc.plan_mode(P("ext4"), "83", which), ("partclone", "partclone.extfs"))
        self.assertEqual(fc.plan_mode(P("ntfs"), "7", which), ("partclone", "partclone.ntfs"))
        self.assertEqual(fc.plan_mode(P("xfs"), "83", which), ("dd", None))  # outil absent
        self.assertEqual(fc.plan_mode(P("BitLocker"), "7", which), ("dd", None))
        self.assertEqual(fc.plan_mode(P(""), "", which), ("dd", None))
        self.assertEqual(fc.plan_mode(P("swap"), "82", which), ("swap", None))
        self.assertEqual(fc.plan_mode(P(""), "5", which), ("skip", None))

    def test_commands(self):
        self.assertEqual(fc.read_cmd("partclone", "partclone.ntfs", "/dev/sda2"),
                         ["partclone.ntfs", "-c", "-s", "/dev/sda2", "-o", "-"])
        self.assertEqual(fc.write_cmd("partclone", "partclone.ntfs", "/dev/sdb2"),
                         ["partclone.ntfs", "-r", "-s", "-", "-o", "/dev/sdb2"])
        self.assertEqual(fc.copy_cmds("partclone", "partclone.fat", "/dev/a1", "/dev/b1", ["-z", "9"]),
                         [["partclone.fat", "-b", "-z", "9", "-s", "/dev/a1", "-o", "/dev/b1"]])
        self.assertEqual(fc.mkswap_cmd("/dev/b4", "u", "l"), ["mkswap", "-U", "u", "-L", "l", "/dev/b4"])


class CaptureDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _capture(self, **kw):
        r = FakeRunner()
        path, manifest = ops.capture(r, "/dev/sda", "poste-type", self.dir, description="test", **kw)
        return r, path, manifest

    def test_capture_writes_image(self):
        r, path, m = self._capture()
        self.assertEqual(path, os.path.join(self.dir, "poste-type"))
        self.assertEqual(sorted(os.listdir(path)), sorted([
            "clonix.json", "table.sfdisk", "boot.img", ".ventoyignore",
            "p1.partclone.zst", "p2.partclone.zst", "p3.dd.zst"]))
        modes = [(p["number"], p["mode"]) for p in m["partitions"]]
        self.assertEqual(modes, [(1, "partclone"), (2, "partclone"), (3, "dd"), (4, "swap")])
        self.assertEqual(m["partitions"][3]["uuid"], "swap-uuid")
        pipes = r.commands("pipe")
        self.assertEqual(pipes[0], [["partclone.fat", "-c", "-s", "/dev/sda1", "-o", "-"],
                                    ["zstd", "-q", "-T0", "-1", "-c"]])
        self.assertEqual(pipes[2][0][:2], ["dd", "if=/dev/sda3"])
        with open(os.path.join(path, "boot.img"), "rb") as f:
            self.assertEqual(len(f.read()), 2048 * 512)
        manifest, bad = im.verify(path)
        self.assertEqual(bad, [])
        self.assertEqual(manifest["source"]["serial"], "SN-sda")
        self.assertEqual([n for n, _, _ in im.list_images(self.dir)], ["poste-type"])

    def test_capture_refuses_mounted_source_and_existing_name(self):
        r = FakeRunner(dumps={"/dev/sde": DOS_DUMP})
        with self.assertRaisesRegex(ClonixError, "montées"):
            ops.capture(r, "/dev/sde", "x", self.dir)
        self._capture()
        with self.assertRaisesRegex(ClonixError, "existe déjà"):
            self._capture()

    def test_failed_capture_leaves_no_image(self):
        class Failing(FakeRunner):
            def pipeline(self, cmds, **kw):
                if "/dev/sda2" in cmds[0]:
                    raise CommandError(cmds[0], 1)
                return super().pipeline(cmds, **kw)

        with self.assertRaises(CommandError):
            ops.capture(Failing(), "/dev/sda", "rate", self.dir)
        self.assertEqual(os.listdir(self.dir), [])

    def test_deploy_to_larger_disk(self):
        _, path, _ = self._capture()
        r = FakeRunner()
        asked = []
        ops.deploy(r, im.DirSource(path), "/dev/sdc", self.dir, confirm=lambda m, d: asked.append(d.path))
        self.assertEqual(asked, ["/dev/sdc"])
        runs = r.commands("run")
        self.assertIn(["wipefs", "--all", "--force", "/dev/sdc"], runs)
        sfdisk = next(c for c in r.calls if c[0] == "run" and c[1][0] == "sfdisk" and "--dump" not in c[1])
        self.assertIn("/dev/sdc1 : start=", sfdisk[2])
        self.assertNotIn("last-lba", sfdisk[2])
        self.assertIn(["mkswap", "-U", "swap-uuid", "-L", "sw", "/dev/sdc4"], runs)
        writes = [(c[1], c[2], len(c[3])) for c in r.calls if c[0] == "write"]
        self.assertEqual(writes, [("/dev/sdc", 0, 440), ("/dev/sdc", 34 * 512, (2048 - 34) * 512)])
        pipes = r.commands("pipe")
        self.assertEqual(pipes[0], [["zstd", "-q", "-d", "-c"],
                                    ["partclone.fat", "-r", "-s", "-", "-o", "/dev/sdc1"]])
        self.assertEqual(pipes[2][1][:2], ["dd", "of=/dev/sdc3"])
        self.assertEqual(len(pipes), 3)

    def test_deploy_refuses_small_disk_before_writing(self):
        _, path, _ = self._capture()
        r = FakeRunner()
        with self.assertRaisesRegex(ClonixError, "trop petit"):
            ops.deploy(r, im.DirSource(path), "/dev/sdd", self.dir, confirm=lambda m, d: self.fail())
        self.assertFalse([c for c in r.calls if c[0] in ("write", "pipe")])

    def test_deploy_cancel_writes_nothing(self):
        _, path, _ = self._capture()
        r = FakeRunner()

        def cancel(m, d):
            raise Cancelled("non")

        with self.assertRaises(Cancelled):
            ops.deploy(r, im.DirSource(path), "/dev/sdc", self.dir, confirm=cancel)
        self.assertEqual({c[1][0] for c in r.calls if c[0] == "run"}, {"lsblk", "findmnt"})
        self.assertFalse([c for c in r.calls if c[0] in ("write", "pipe")])

    def test_deploy_detects_corruption(self):
        _, path, _ = self._capture()
        with open(os.path.join(path, "p2.partclone.zst"), "ab") as f:
            f.write(b"!")
        _, bad = im.verify(path)
        self.assertEqual(bad, [("p2.partclone.zst", "somme SHA-256 différente")])
        with self.assertRaisesRegex(ClonixError, "corrompue"):
            ops.deploy(FakeRunner(), im.DirSource(path), "/dev/sdc", self.dir)

    def test_deploy_missing_partclone_tool(self):
        _, path, _ = self._capture()
        r = FakeRunner(tools=["zstd"])
        with self.assertRaisesRegex(ClonixError, "partclone.extfs, partclone.fat"):
            ops.deploy(r, im.DirSource(path), "/dev/sdc", self.dir)
        self.assertFalse([c for c in r.calls if c[0] in ("write", "pipe")])

    def test_multicast_tar_stream_roundtrip(self):
        _, path, manifest = self._capture()
        buf = io.BytesIO()
        im.write_tar_stream(path, manifest, buf)
        buf.seek(0)
        r = FakeRunner()
        ops.deploy(r, im.TarStreamSource(buf), "/dev/sdc", None)
        self.assertEqual(len(r.commands("pipe")), 3)
        self.assertEqual(r.last_fed, b"DATA:dd if=/dev/sda3 bs=16M status=progress")

    def test_tar_stream_wrong_order(self):
        _, path, manifest = self._capture()
        buf = io.BytesIO()
        manifest = dict(manifest, partitions=list(reversed(manifest["partitions"])))
        im.write_tar_stream(path, manifest, buf)
        buf.seek(0)
        with self.assertRaisesRegex(ClonixError, "Flux inattendu"):
            ops.deploy(FakeRunner(), im.TarStreamSource(buf), "/dev/sdc", None)

    def test_dry_run_writes_nothing(self):
        r = FakeRunner(dry_run=True)
        ops.capture(r, "/dev/sda", "essai", self.dir)
        self.assertEqual(os.listdir(self.dir), [])


class SpeedTest(unittest.TestCase):
    def test_level_auto(self):
        self.assertEqual(fc.choose_level("auto", "ext4"), 1)
        self.assertEqual(fc.choose_level("auto", "cifs"), "adapt")
        self.assertEqual(fc.choose_level("5", "cifs"), "5")
        self.assertEqual(fc.compress_cmd("zstd", "adapt"), ["zstd", "-q", "-T0", "--adapt=min=1,max=9", "-c"])

    def test_partclone_flags_only_when_supported(self):
        new = {"partclone.extfs": "-z, --buffer_size SIZE\n -a, --checksum-mode=MODE"}.get
        sup = lambda tool, opt: opt in (new(tool) or "")  # noqa: E731
        self.assertEqual(fc.partclone_flags(sup, "partclone.extfs", capture=True),
                         ["-z", str(fc.PARTCLONE_BUFFER), "-a0"])
        self.assertEqual(fc.partclone_flags(sup, "partclone.extfs"), ["-z", str(fc.PARTCLONE_BUFFER)])
        self.assertEqual(fc.partclone_flags(sup, "partclone.ntfs", capture=True), [])

    def test_network_store_uses_adaptive_level(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = FakeRunner()
        r.mount_fstype = "cifs"
        r.help_text["partclone.extfs"] = "--buffer_size --checksum-mode"
        ops.capture(r, "/dev/sda", "net", tmp.name)
        pipes = r.commands("pipe")
        self.assertEqual(pipes[1][0], ["partclone.extfs", "-c", "-z", str(fc.PARTCLONE_BUFFER), "-a0",
                                       "-s", "/dev/sda2", "-o", "-"])
        self.assertIn("--adapt=min=1,max=9", pipes[1][1])

    def test_progress_parsers(self):
        from clonix.runner import parse_dd, parse_partclone
        frac, rate, left = parse_partclone(
            "Elapsed: 00:00:10, Remaining: 00:01:05, Completed:  13.37%,   6.00GB/min,")
        self.assertAlmostEqual(frac, 0.1337)
        self.assertAlmostEqual(rate, 6 * 1024 ** 3 / 60)
        self.assertEqual(left, 65)
        self.assertEqual(parse_partclone("Total Time: 00:00:03, Ave. Rate: 1.3GB/min, 100.00% completed!"), None)
        self.assertEqual(parse_dd("1073741824 bytes (1.1 GB, 1.0 GiB) copied, 2.5 s, 430 MB/s"), 1073741824)
        self.assertIsNone(parse_dd("8+0 records in"))

    def test_real_progress_and_overall(self):
        events = []

        class Rec:
            def log(self, m):
                pass

            def progress(self, p):
                events.append(p)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = os.path.join(tmp.name, "partclone.fake")
        with open(tool, "w") as f:
            f.write("#!/bin/sh\n"
                    "printf 'Partclone v0.3\\n' >&2\n"
                    "printf 'Elapsed: 00:00:01, Remaining: 00:00:01, Completed:  50.00%%,  1.00GB/min,\\r' >&2\n"
                    "printf '\\033[AElapsed: 00:00:02, Remaining: 00:00:00, Completed: 100.00%%,  1.00GB/min,\\n' >&2\n")
        os.chmod(tool, 0o755)
        r = Runner(reporter=Rec())
        r.plan([("a", 100), ("b", 300)])
        r.start_step(1)
        r.pipeline([[tool]])
        self.assertEqual([round(e["overall"], 3) for e in events], [0.25, 0.625, 1.0])
        self.assertEqual(events[-1]["label"], "b")
        self.assertEqual(events[-1]["step"], 2)

    def test_cancel_kills_pipeline(self):
        import threading
        r = Runner()
        threading.Timer(0.3, r.cancel).start()
        from clonix.errors import Cancelled
        with self.assertRaises(Cancelled):
            r.pipeline([["sleep", "30"]])


class CloneTest(unittest.TestCase):
    def test_clone(self):
        r = FakeRunner()
        ops.clone(r, "/dev/sda", "/dev/sdc", confirm=lambda s, d: None)
        pipes = r.commands("pipe")
        self.assertEqual(pipes, [
            [["partclone.fat", "-b", "-s", "/dev/sda1", "-o", "/dev/sdc1"]],
            [["partclone.extfs", "-b", "-s", "/dev/sda2", "-o", "/dev/sdc2"]],
            [["dd", "if=/dev/sda3", "bs=16M", "status=progress"],
             ["dd", "of=/dev/sdc3", "bs=16M", "iflag=fullblock", "conv=fsync"]],
        ])
        self.assertIn(["mkswap", "-U", "swap-uuid", "-L", "sw", "/dev/sdc4"], r.commands("run"))

    def test_clone_same_disk_and_small_target(self):
        r = FakeRunner()
        with self.assertRaisesRegex(ClonixError, "identiques"):
            ops.clone(r, "/dev/sda", "/dev/sda")
        with self.assertRaisesRegex(ClonixError, "plus petit"):
            ops.clone(r, "/dev/sda", "/dev/sdd")


class RunnerTest(unittest.TestCase):
    def test_pipeline_feed_and_sink(self):
        r = Runner()
        out = io.BytesIO()
        payload = os.urandom(3 * 1024 * 1024) * 3  # plus grand que les tampons de tube
        n = r.pipeline([["gzip", "-1", "-c"], ["gzip", "-d", "-c"]], feed=io.BytesIO(payload), sink=out)
        self.assertEqual(n, len(payload))
        self.assertEqual(out.getvalue(), payload)

    def test_pipeline_failure(self):
        with self.assertRaises(CommandError) as ctx:
            Runner().pipeline([["sh", "-c", "exit 3"], ["cat"]], sink=io.BytesIO())
        self.assertEqual(ctx.exception.returncode, 3)

    def test_feed_error_propagates(self):
        _ = im  # VerifyingReader lève une erreur en fin de lecture

        class Bad(io.BytesIO):
            def read(self, n=-1):
                buf = super().read(n)
                if not buf:
                    raise ClonixError("somme")
                return buf

        with self.assertRaisesRegex(ClonixError, "somme"):
            Runner().pipeline([["cat"]], feed=Bad(b"abc"), sink=io.BytesIO())

    def test_missing_tool(self):
        with self.assertRaisesRegex(ClonixError, "introuvable"):
            Runner().run(["outil-qui-n-existe-pas"])


class ConfirmTest(unittest.TestCase):
    def test_ask_erase(self):
        d = dk.Disk(path="/dev/sdc", size=32 * GIB)
        ask_erase(d, "test", stream_in=io.StringIO("sdc\n"), stream_out=io.StringIO())
        with self.assertRaises(Cancelled):
            ask_erase(d, "test", stream_in=io.StringIO("oui\n"), stream_out=io.StringIO())


if __name__ == "__main__":
    unittest.main()


class ClonixKeyStorageTest(unittest.TestCase):
    def _tree(self, data_label="CLONIX-DATA"):
        from tests.fakes import lsblk_disk, lsblk_part, GIB
        return [
            lsblk_disk("sda", 20 * GIB, [lsblk_part("sda1", 10 * GIB, "ext4")]),
            lsblk_disk("sdu", 16 * GIB, [
                lsblk_part("sdu1", 2 * GIB, "iso9660", mount="/run/live/medium"),
                lsblk_part("sdu2", 14 * GIB, "exfat", label=data_label),
            ], tran="usb", rm=True),
        ]

    def test_find_data_partition(self):
        from clonix import storage
        r = FakeRunner(tree=self._tree(), live_disk="/dev/sdu")
        part = storage.find_data_partition(r)
        self.assertEqual(part.path, "/dev/sdu2")

    def test_find_data_partition_absent(self):
        from clonix import storage
        r = FakeRunner(tree=self._tree(data_label="PERSO"), live_disk="/dev/sdu")
        self.assertIsNone(storage.find_data_partition(r))

    def test_mount_data_partition(self):
        import tempfile, os
        from clonix import storage
        r = FakeRunner(tree=self._tree(), live_disk="/dev/sdu")
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: os.path.isdir(tmp) and os.rmdir(tmp))
        storage.mount_data_partition(r, tmp)
        self.assertIn(["mount", "/dev/sdu2", tmp], r.commands("run"))

    def test_mount_data_partition_missing_raises(self):
        from clonix import storage
        r = FakeRunner(tree=self._tree(data_label="PERSO"), live_disk="/dev/sdu")
        with self.assertRaisesRegex(ClonixError, "CLONIX-DATA"):
            storage.mount_data_partition(r, "/tmp/nowhere-clonix")


class PartcloneTwoLineProgressTest(unittest.TestCase):
    """Sortie réelle de partclone : la ligne « current block … Complete » ne doit
    pas fausser l'avancement ni l'estimation du temps restant."""

    def test_block_line_ignored(self):
        from clonix.runner import parse_partclone
        self.assertIsNone(parse_partclone(
            "current block:     123456, total block:  124000000, Complete:   1.50%"))

    def test_eta_follows_used_data_not_partition(self):
        events = []

        class Rec:
            def log(self, m):
                pass

            def progress(self, p):
                events.append(p)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = os.path.join(tmp.name, "partclone.fake")
        # 50 % des données utilisées copiées, mais seulement 1,5 % de la partition
        # parcourue (Windows est rangé au début du disque).
        with open(tool, "w") as f:
            f.write("#!/bin/sh\n"
                    "printf 'Elapsed: 00:03:00, Remaining: 00:03:00, Completed:  50.00%%,   6.40GB/min,\\n' >&2\n"
                    "printf 'current block:    1860000, total block:  124000000, Complete:   1.50%%\\033[A\\n' >&2\n")
        os.chmod(tool, 0o755)
        r = Runner(reporter=Rec())
        r.plan([("Partition 3 (ntfs)", 510 * GIB)])
        r.start_step(0)
        r.pipeline([[tool]])
        self.assertEqual(len(events), 2)  # start_step + la ligne « Completed » uniquement
        last = events[-1]
        self.assertAlmostEqual(last["fraction"], 0.5)
        self.assertEqual(last["eta"], 180)  # 3 min, pas ~1h20
        self.assertAlmostEqual(last["rate"], 6.4 * 1024 ** 3 / 60)


class VentoyStorageTest(unittest.TestCase):
    """Clonix démarré via Ventoy : les images vont dans la partition Ventoy."""

    def _tree(self, remount=True):
        from tests.fakes import lsblk_disk, lsblk_part, GIB
        part = lsblk_part("sda1", 256 * GIB, "exfat", label="Ventoy", children=[
            {"name": "ventoy", "path": "/dev/mapper/ventoy", "type": "dm",
             "fstype": "iso9660", "label": "CLONIX", "mountpoint": "/run/live/medium"},
        ] + ([{"name": "sda1", "path": "/dev/mapper/sda1", "type": "dm",
               "fstype": "exfat", "label": "Ventoy", "mountpoint": None}] if remount else []))
        return [
            lsblk_disk("nvme0n1", 512 * GIB, [lsblk_part("nvme0n1p3", 510 * GIB, "ntfs")]),
            lsblk_disk("sda", 256 * GIB, [part], tran="usb"),
        ]

    def test_mapper_copy_preferred(self):
        from clonix import storage
        r = FakeRunner(tree=self._tree())
        self.assertEqual(storage.find_data_partition(r).path, "/dev/mapper/sda1")
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        self.assertEqual(storage.mount_data_partition(r, tmp), "/dev/mapper/sda1")

    def test_locked_partition_explains_remount(self):
        from clonix import storage

        class Busy(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "mount":
                    raise CommandError(argv, 32, "mount: /dev/sda1 already mounted or mount point busy.")
                return super().run(argv, **kw)

        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        with self.assertRaisesRegex(ClonixError, "VTOY_LINUX_REMOUNT"):
            storage.mount_data_partition(Busy(tree=self._tree(remount=False)), tmp)
        # au démarrage, l'échec reste silencieux
        self.assertFalse(storage.auto_mount_data(Busy(tree=self._tree(remount=False)), tmp))

    def test_clonix_data_wins_over_ventoy(self):
        from clonix import storage
        from tests.fakes import lsblk_disk, lsblk_part, GIB
        tree = self._tree() + [lsblk_disk("sdb", 64 * GIB, [lsblk_part("sdb1", 64 * GIB, "exfat", label="CLONIX-DATA")])]
        self.assertEqual(storage.find_data_partition(FakeRunner(tree=tree)).path, "/dev/sdb1")


class SlimWindowsTest(unittest.TestCase):
    def _runner(self):
        from tests.fakes import lsblk_disk, lsblk_part, GIB
        tree = [lsblk_disk("sda", 20 * GIB, [
            lsblk_part("sda1", 100 * 1024 * 1024, "vfat", label="EFI"),
            lsblk_part("sda2", 10 * GIB, "ntfs", label="Windows"),
        ])]
        return FakeRunner(tree=tree, tools=["zstd", "partclone.fat", "partclone.ntfs"])

    def test_capture_slim_removes_windows_scratch_files_first(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = self._runner()
        ops.capture(r, "/dev/sda", "win", tmp.name, slim=True)
        runs = r.commands("run")
        mount = next(c for c in runs if c[0] == "mount")
        self.assertEqual(mount[:5], ["mount", "-t", "ntfs-3g", "-o", "remove_hiberfile"])
        self.assertEqual(mount[5], "/dev/sda2")
        rm = next(c for c in runs if c[0] == "rm")
        self.assertTrue(rm[2].endswith("pagefile.sys") and rm[3].endswith("swapfile.sys"))
        self.assertIn(["umount", mount[6]], runs)
        # l'allègement a lieu avant la copie
        order = [c[0] for c in r.calls]
        self.assertLess(r.calls.index(next(c for c in r.calls if c[0] == "run" and c[1][0] == "rm")),
                        order.index("pipe"))
        self.assertTrue(os.path.isfile(os.path.join(tmp.name, "win", ".ventoyignore")))

    def test_capture_without_slim_does_not_touch_source(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = self._runner()
        ops.capture(r, "/dev/sda", "win", tmp.name)
        self.assertFalse([c for c in r.commands("run") if c[0] in ("mount", "rm")])

    def test_slim_failure_is_not_fatal(self):
        class NoMount(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "mount":
                    raise CommandError(argv, 14, "volume is dirty")
                return super().run(argv, **kw)

        from tests.fakes import lsblk_disk, lsblk_part, GIB
        tree = [lsblk_disk("sda", 20 * GIB, [lsblk_part("sda2", 10 * GIB, "ntfs")])]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = GPT_DUMP.replace("/dev/sda1 :", "/dev/sda9 :")  # le dump a 4 partitions ; peu importe
        r = NoMount(tree=tree, dumps={"/dev/sda": dump}, tools=["zstd", "partclone.ntfs"])
        ops.capture(r, "/dev/sda", "win", tmp.name, slim=True)  # ne lève pas
        self.assertIn("allègement impossible", r.reporter.out.getvalue())


class VerifyProgressTest(unittest.TestCase):
    def test_verify_reports_progress(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path, manifest = ops.capture(FakeRunner(), "/dev/sda", "img", tmp.name)
        seen = []
        _, bad = im.verify(path, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(bad, [])
        total = im.image_bytes(manifest)
        self.assertEqual(seen[-1], (total, total))

    def test_byte_progress_emits_and_can_cancel(self):
        events = []

        class Rec:
            def log(self, m):
                pass

            def progress(self, p):
                events.append(p)

        from clonix.errors import Cancelled
        r = Runner(reporter=Rec())
        r.plan([("Vérification", 1)])
        r.start_step(0)
        cb = r.byte_progress()
        cb(50, 100)
        self.assertEqual(events[-1]["fraction"], 0.5)
        r.cancel()
        with self.assertRaises(Cancelled):
            cb(60, 100)


class KernelTableCheckTest(unittest.TestCase):
    def test_matching_geometry_passes(self):
        r = FakeRunner()
        r.geometry = {"/dev/sdc1": (2048, 204800), "/dev/sdc2": (206848, 20971520),
                      "/dev/sdc3": (21178368, 2097152), "/dev/sdc4": (23275520, 1048576)}
        pt.write_table(r, dk.get_disk(r, "/dev/sdc"), GPT_DUMP)

    def test_stale_kernel_table_stops_before_copy(self):
        r = FakeRunner()
        r.geometry = {"/dev/sdc3": (999, 1)}  # le noyau garde l'ancienne disposition
        with mock.patch("clonix.partitions.time.sleep"):
            with self.assertRaisesRegex(ClonixError, "pas pris en compte"):
                pt.write_table(r, dk.get_disk(r, "/dev/sdc"), GPT_DUMP)
        self.assertIn(["blockdev", "--rereadpt", "/dev/sdc"], r.commands("run"))


class HibernatedCaptureTest(unittest.TestCase):
    def test_warns_when_windows_was_not_shut_down(self):
        from tests.fakes import lsblk_disk, lsblk_part, GIB

        class Probe(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "ntfs-3g.probe":
                    raise CommandError(argv, 14, "Windows is hibernated")
                return super().run(argv, **kw)

        tree = [lsblk_disk("sda", 20 * GIB, [lsblk_part("sda2", 10 * GIB, "ntfs")])]
        r = Probe(tree=tree, tools=["zstd", "partclone.ntfs", "ntfs-3g.probe"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ops.capture(r, "/dev/sda", "win", tmp.name)
        self.assertIn("powercfg /h off", r.reporter.out.getvalue())


class UsbSpeedTest(unittest.TestCase):
    def _sysfs(self, speed):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        usbdev = os.path.join(root, "devices", "pci0000:00", "0000:00:14.0", "usb2", "2-1")
        disk = os.path.join(usbdev, "2-1:1.0", "host0", "target0:0:0", "0:0:0:0")
        os.makedirs(disk)
        for name, value in (("speed", speed), ("idVendor", "0bda")):
            with open(os.path.join(usbdev, name), "w") as f:
                f.write(value + "\n")
        os.makedirs(os.path.join(root, "block", "sda"))
        os.symlink(disk, os.path.join(root, "block", "sda", "device"))
        return os.path.join(root, "block")

    def test_reads_usb_link_speed(self):
        self.assertEqual(dk.usb_speed("/dev/sda", sysfs=self._sysfs("480")), 480)
        self.assertEqual(dk.usb_speed("/dev/sda", sysfs=self._sysfs("5000")), 5000)

    def test_not_usb(self):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        os.makedirs(os.path.join(root, "devices", "pci0000:00", "nvme0"))
        os.makedirs(os.path.join(root, "block", "nvme0n1"))
        os.symlink(os.path.join(root, "devices", "pci0000:00", "nvme0"),
                   os.path.join(root, "block", "nvme0n1", "device"))
        self.assertIsNone(dk.usb_speed("/dev/nvme0n1", sysfs=os.path.join(root, "block")))

    def test_warns_on_usb2_store(self):
        class Store(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "findmnt":
                    return "/dev/sda1\n"
                return super().run(argv, **kw)

        r = Store()
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        self.assertEqual(dk.check_usb_link(r, tmp, sysfs=self._sysfs("480")), 480)
        self.assertIn("USB 2", r.reporter.out.getvalue())
        r2 = Store()
        dk.check_usb_link(r2, tmp, sysfs=self._sysfs("5000"))
        self.assertNotIn("USB 2", r2.reporter.out.getvalue())


class ReadaheadTest(unittest.TestCase):
    def test_sets_readahead_on_store_chain(self):
        class Store(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "findmnt":
                    return "/dev/mapper/sda1\n"
                if argv[0] == "lsblk" and "-s" in argv:
                    return "/dev/mapper/sda1\n/dev/sda1\n/dev/sda\n"
                return super().run(argv, **kw)

        r = Store()
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        self.assertEqual(dk.tune_readahead(r, tmp), ["/dev/mapper/sda1", "/dev/sda1", "/dev/sda"])
        self.assertIn(["blockdev", "--setra", "32768", "/dev/sda"], r.commands("run"))

    def test_network_share_untouched(self):
        r = FakeRunner()  # findmnt répond « tmpfs »
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        self.assertEqual(dk.tune_readahead(r, tmp), [])
        self.assertFalse([c for c in r.commands("run") if c[0] == "blockdev"])
