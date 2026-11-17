"""Support des images débranché puis rebranché sans redémarrer ; clé de démarrage protégée."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from clonix import disks as dk
from clonix import storage, web
from clonix.errors import ClonixError, CommandError

from tests.fakes import FakeRunner, default_tree, lsblk_disk, lsblk_part


class HotplugTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.r = FakeRunner()
        self.app = web.App(self.tmp, runner_factory=lambda rep: self.r, code="1", dry_run=True)

    def test_unplug_then_replug(self):
        app = self.app
        with mock.patch("os.path.ismount", return_value=True), \
                mock.patch.object(dk, "disk_of_mount", return_value="/dev/sdb"), \
                mock.patch.object(storage, "orphan_mappers", return_value=["ventoy"]):
            app._known = {"sda", "sdb"}
            self.assertIsNone(app.hotplug_tick({"sda", "sdb"}))
            self.assertEqual(app.hotplug_tick({"sda"}), "unplugged")
        runs = self.r.commands("run")
        self.assertIn(["umount", "-l", self.tmp], runs)
        self.assertIn(["dmsetup", "remove", "--force", "ventoy"], runs)
        self.assertTrue(app.storage_event["bad"])
        self.assertIn("débranché", app.storage_event["msg"])

        mounts = iter([False, True])  # le système n'a pas encore lu les partitions au 1er essai
        link = {"kind": "usb", "label": "USB 3", "detail": "5 Gbit/s", "slow": False}
        with mock.patch("os.path.ismount", return_value=False), \
                mock.patch.object(storage, "auto_mount_data", side_effect=lambda *a: next(mounts)), \
                mock.patch.object(dk, "disk_of_mount", return_value="/dev/sdc"), \
                mock.patch.object(dk, "store_link", return_value=link):
            self.assertIsNone(app.hotplug_tick({"sda", "sdc"}))
            self.assertEqual(app.hotplug_tick({"sda", "sdc"}), "mounted")
            self.assertIsNone(app.hotplug_tick({"sda", "sdc"}))  # plus rien à faire
        self.assertIn("rebranché et monté (USB 3, 5 Gbit/s)", app.storage_event["msg"])
        self.assertFalse(app.storage_event["bad"])
        self.assertEqual(app._store_disk, "/dev/sdc")

    def test_nothing_during_a_job(self):
        app = self.app
        app.job = mock.Mock(state="running")
        with mock.patch("os.path.ismount", return_value=True), \
                mock.patch.object(dk, "disk_of_mount", return_value="/dev/sdb"):
            app._known = {"sda", "sdb"}
            self.assertIsNone(app.hotplug_tick({"sda"}))
        self.assertNotIn(["umount", "-l", self.tmp], self.r.commands("run"))

    def test_no_new_device_no_mount_attempt(self):
        with mock.patch("os.path.ismount", return_value=False), \
                mock.patch.object(storage, "auto_mount_data") as auto:
            self.app._known = {"sda"}
            self.app.hotplug_tick({"sda"})
        auto.assert_not_called()


class OrphanMapperTest(unittest.TestCase):
    def test_finds_mappers_without_disk(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for dm, name, slaves in (("dm-0", "ventoy", []), ("dm-1", "sdb1", ["sdb1"]), ("dm-2", "vg-root", ["sda2"])):
            os.makedirs(os.path.join(root, dm, "slaves"))
            os.makedirs(os.path.join(root, dm, "dm"))
            for s in slaves:
                open(os.path.join(root, dm, "slaves", s), "w").close()
            with open(os.path.join(root, dm, "dm", "name"), "w") as f:
                f.write(name + "\n")
        os.makedirs(os.path.join(root, "sda"))
        self.assertEqual(storage.orphan_mappers(root), ["ventoy"])


class MountFallbackTest(unittest.TestCase):
    def test_dead_mapper_does_not_block_real_partition(self):
        class R(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "lsblk" and "PATH,TYPE,FSTYPE,LABEL" in argv:
                    return ('{"blockdevices": [{"path": "/dev/mapper/sdb1", "type": "dm", "fstype": "exfat", '
                            '"label": "Ventoy"}, {"path": "/dev/sdc1", "type": "part", "fstype": "exfat", '
                            '"label": "Ventoy"}]}')
                if argv[0] == "mount" and "/dev/mapper/sdb1" in argv:
                    self.calls.append(("run", argv, None))
                    raise CommandError(argv, 32, "mount: /srv: can't read superblock on /dev/mapper/sdb1.")
                return super().run(argv, **kw)

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        r = R()
        self.assertEqual(storage.mount_data_partition(r, tmp), "/dev/sdc1")


class BootKeyTest(unittest.TestCase):
    def tree(self):
        return default_tree() + [
            lsblk_disk("sdv", 1000 * 1024 ** 3, [lsblk_part("sdv1", 999 * 1024 ** 3, "exfat", label="Ventoy"),
                                                  lsblk_part("sdv2", 32 * 1024 ** 2, "vfat", label="VTOYEFI")],
                       tran="usb"),
            lsblk_disk("sdw", 8 * 1024 ** 3, [lsblk_part("sdw1", 1024 ** 3, "iso9660", label="CLONIX"),
                                               lsblk_part("sdw2", 4 * 1024 ** 2, "vfat")], tran="usb"),
        ]

    def test_boot_keys_refused_even_when_not_mounted(self):
        r = FakeRunner(tree=self.tree())
        for path in ("/dev/sdv", "/dev/sdw"):
            disk = dk.get_disk(r, path)
            self.assertTrue(dk.is_boot_key(disk))
            with self.assertRaisesRegex(ClonixError, "clé de démarrage"):
                dk.check_writable_target(r, disk)
        self.assertFalse(dk.is_boot_key(dk.get_disk(r, "/dev/sdc")))

    def test_state_marks_boot_key(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        r = FakeRunner(tree=self.tree())
        app = web.App(tmp, runner_factory=lambda rep: r, code="1", dry_run=True)
        disks = {d["path"]: d for d in app.state()["disks"]}
        self.assertEqual(disks["/dev/sdv"]["protected"], "la clé de démarrage")
        self.assertFalse(disks["/dev/sdv"]["writable"])


if __name__ == "__main__":
    unittest.main()
