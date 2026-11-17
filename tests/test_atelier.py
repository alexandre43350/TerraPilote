"""Nom des PC, mode atelier et compte à rebours d'extinction."""

import os
import shutil
import struct
import tempfile
import time
import unittest
from unittest import mock

from clonix import atelier, naming, ops, web, winfix
from clonix.errors import ClonixError

from tests.fakes import GIB, FakeRunner, lsblk_disk, lsblk_part
from tests.test_winfix import FakeHivexModule, child, node


class NamingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_check_name(self):
        self.assertEqual(naming.check_name("SALLE1-PC07"), "SALLE1-PC07")
        for bad in ("", "PC_01", "ÉLÈVE-01", "1234", "-PC", "UN-NOM-BEAUCOUP-TROP-LONG"):
            with self.assertRaises(ClonixError):
                naming.check_name(bad)

    def test_series(self):
        self.assertEqual(naming.expand("SALLE1-PC##", 7), "SALLE1-PC07")
        self.assertEqual(naming.expand("PC###", 12), "PC012")
        self.assertEqual(naming.check_pattern("SALLE1-PC##"), "SALLE1-PC##")
        with self.assertRaisesRegex(ClonixError, "trop long"):
            naming.check_pattern("SALLE-INFO-PC###")
        with self.assertRaisesRegex(ClonixError, "Une seule"):
            naming.check_pattern("S#-PC##")
        self.assertEqual(naming.peek(self.dir, "PC##"), "PC01")
        naming.commit(self.dir, "PC##", 1)
        naming.commit(self.dir, "PC##", 2)
        self.assertEqual(naming.peek(self.dir, "PC##"), "PC03")
        naming.set_next(self.dir, "PC##", 10)
        self.assertEqual(naming.peek(self.dir, "PC##"), "PC10")
        naming.commit(self.dir, "FIXE", 1)  # nom fixe : pas de compteur
        self.assertEqual(naming.counters(self.dir), {"PC##": 10})


def windows_system():
    def cs(name):
        return node(name, children=[
            node("Control", children=[node("ComputerName", children=[
                node("ComputerName", {"ComputerName": {}}), node("ActiveComputerName", {"ComputerName": {}})])]),
            node("Services", children=[node("Tcpip", children=[node("Parameters", {"Hostname": {}})]),
                                       node("stornvme", {"Start": {"key": "Start", "t": 4, "value": struct.pack("<I", 3)}})])])
    return node("ROOT", children=[cs("ControlSet001"), cs("ControlSet002"), node("Select")])


class ComputerNameTest(unittest.TestCase):
    def test_sets_every_name_value(self):
        root = windows_system()
        fake = FakeHivexModule(root)
        changes = winfix.set_computer_name("/x/SYSTEM", "Salle1-PC07", fake)
        self.assertTrue(fake.committed)
        self.assertEqual(len(changes), 6)
        for cs in ("ControlSet001", "ControlSet002"):
            cn = child(child(child(root, cs), "Control"), "ComputerName")
            for sub in ("ComputerName", "ActiveComputerName"):
                value = child(cn, sub)["values"]["ComputerName"]["value"]
                self.assertEqual(value.decode("utf-16-le"), "SALLE1-PC07\x00")
            params = child(child(child(child(root, cs), "Services"), "Tcpip"), "Parameters")
            self.assertEqual(params["values"]["NV Hostname"]["value"].decode("utf-16-le"), "Salle1-PC07\x00")

    def test_not_a_windows_hive(self):
        fake = FakeHivexModule(node("ROOT", children=[node("Software")]))
        with self.assertRaisesRegex(ClonixError, "introuvable"):
            winfix.set_computer_name("/x/SYSTEM", "PC01", fake)
        self.assertFalse(fake.committed)


def tree(*internal):
    disks = [lsblk_disk("sdv", 1000 * GIB, [lsblk_part("sdv1", 999 * GIB, "exfat", label="Ventoy"),
                                            lsblk_part("sdv2", 32 * 1024 ** 2, "vfat", label="VTOYEFI")],
                        tran="usb")]
    return disks + list(internal)


class PickTargetTest(unittest.TestCase):
    def pick(self, *internal, need=100 * GIB):
        return atelier.pick_target(FakeRunner(tree=tree(*internal)), None, need)

    def test_single_internal_disk(self):
        disk, reason = self.pick(lsblk_disk("nvme0n1", 512 * GIB, tran="nvme"))
        self.assertEqual((disk.path, reason), ("/dev/nvme0n1", None))

    def test_refuses_when_unsure(self):
        self.assertIn("aucun disque interne", self.pick()[1])
        self.assertIn("trop petit", self.pick(lsblk_disk("sda", 64 * GIB))[1])
        two = self.pick(lsblk_disk("nvme0n1", 512 * GIB, tran="nvme"), lsblk_disk("sda", 1000 * GIB))
        self.assertIsNone(two[0])
        self.assertIn("plusieurs disques internes", two[1])

    def test_usb_and_removable_never_chosen(self):
        disk, _ = self.pick(lsblk_disk("sdb", 512 * GIB, tran="usb"), lsblk_disk("sdc", 512 * GIB, rm=True),
                            lsblk_disk("nvme0n1", 256 * GIB, tran="nvme"))
        self.assertEqual(disk.path, "/dev/nvme0n1")


class AtelierAppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.tree = tree(lsblk_disk("sda", 20 * GIB, [
            lsblk_part("sda1", 100 * 1024 ** 2, "vfat"), lsblk_part("sda2", 10 * GIB, "ext4"),
            lsblk_part("sda3", GIB, "BitLocker"), lsblk_part("sda4", 512 * 1024 ** 2, "swap")]),
            lsblk_disk("sdc", 32 * GIB))
        src = FakeRunner(tree=self.tree)
        ops.capture(src, "/dev/sda", "salle", self.dir)
        self.runners = []

        def factory(rep):
            r = FakeRunner(tree=[d for d in self.tree if d["name"] != "sda"])
            r.reporter = rep
            self.runners.append(r)
            return r
        self.app = web.App(self.dir, runner_factory=factory, code="1")

    def wait_job(self):
        for _ in range(200):
            if self.app.job and self.app.job.state != "running":
                return self.app.job
            time.sleep(0.02)
        self.fail("tâche bloquée")

    def test_off_by_default(self):
        self.assertEqual(self.app.arm_atelier(start_thread=False)["phase"], "off")

    def test_save_validates(self):
        with self.assertRaisesRegex(ClonixError, "Choisissez une image"):
            self.app.action("atelier-save", {"image": "absente"})
        with self.assertRaisesRegex(ClonixError, "invalide"):
            self.app.action("atelier-save", {"image": "salle", "name_pattern": "PC_##"})
        self.app.action("atelier-save", {"image": "salle", "name_pattern": "PC##", "countdown": 2,
                                         "next_number": 5})
        cfg = atelier.load(self.dir)
        self.assertEqual((cfg["enabled"], cfg["countdown"], cfg["after"]), (True, 10, "poweroff"))
        self.assertEqual(naming.peek(self.dir, "PC##"), "PC05")

    def test_countdown_then_deploy_then_power_countdown(self):
        self.app.action("atelier-save", {"image": "salle", "name_pattern": "PC##"})
        armed = self.app.arm_atelier(start_thread=False)
        self.assertEqual((armed["phase"], armed["disk"], armed["name"]), ("countdown", "/dev/sdc", "PC01"))
        self.assertGreater(self.app.atelier_public()["remaining"], 5)
        with mock.patch.object(winfix, "rename") as rename:
            self.app.atelier_fire(atelier.load(self.dir), "/dev/sdc", {"table": {"label": "gpt"}})
            job = self.wait_job()
        self.assertEqual(job.state, "done", job.error)
        rename.assert_not_called()  # image sans Windows : pas de nom à donner
        self.assertEqual(atelier.load(self.dir)["done"], 1)
        self.assertEqual(self.app.power["phase"], "countdown")
        self.assertEqual(self.app.power["action"], "poweroff")
        self.app.action("power-cancel", {})
        self.assertEqual(self.app.power, {"phase": "off"})

    def test_blocked_when_disk_uncertain(self):
        self.app.action("atelier-save", {"image": "salle"})
        self.app.inspect.tree.append(lsblk_disk("sdd", 64 * GIB))  # 2e disque interne
        armed = self.app.arm_atelier(start_thread=False)
        self.assertEqual(armed["phase"], "blocked")
        self.assertIn("plusieurs disques internes", armed["reason"])

    def test_cancel_and_off(self):
        self.app.action("atelier-save", {"image": "salle"})
        self.app.arm_atelier(start_thread=False)
        self.app.action("atelier-cancel", {})
        self.assertEqual(self.app.atelier["phase"], "cancelled")
        self.app.action("atelier-off", {})
        self.assertFalse(atelier.load(self.dir)["enabled"])
        self.assertEqual(self.app.arm_atelier(start_thread=False)["phase"], "off")

    def test_power_now_does_not_shut_down_outside_the_key(self):
        with mock.patch("subprocess.Popen") as popen:
            self.app.action("power-now", {"action": "reboot"})
        popen.assert_not_called()
        self.assertEqual(self.app.power, {"phase": "done", "action": "reboot"})


class DeployNamedTest(unittest.TestCase):
    def test_series_number_used_only_after_success(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        t = [lsblk_disk("sda", 20 * GIB, [lsblk_part("sda1", 500 * 1024 ** 2, "ntfs")]), lsblk_disk("sdc", 32 * GIB)]
        dump = ("label: gpt\nfirst-lba: 34\nsector-size: 512\n\n"
                "/dev/sda1 : start=2048, size=1024000, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n")
        r = FakeRunner(tree=t, dumps={"/dev/sda": dump}, tools=["zstd", "partclone.ntfs"])
        ops.capture(r, "/dev/sda", "win", tmp)
        with mock.patch.object(winfix, "rename") as rename, mock.patch.object(winfix, "adapt"):
            m = ops.deploy_named(FakeRunner(tree=t, tools=["zstd", "partclone.ntfs"]), "win", "/dev/sdc", tmp, "PC##")
        rename.assert_called_once()
        self.assertEqual(rename.call_args[0][2], "PC01")
        self.assertEqual(m["computer_name"], "PC01")
        self.assertEqual(naming.peek(tmp, "PC##"), "PC02")
        with mock.patch.object(winfix, "rename", side_effect=ClonixError("pas de Windows")), \
                mock.patch.object(winfix, "adapt"):
            ops.deploy_named(FakeRunner(tree=t, tools=["zstd", "partclone.ntfs"]), "win", "/dev/sdc", tmp, "PC##")
        self.assertEqual(naming.peek(tmp, "PC##"), "PC02")  # numéro pas consommé


if __name__ == "__main__":
    unittest.main()
