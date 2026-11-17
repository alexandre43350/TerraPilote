"""Entrée de démarrage UEFI recréée après un déploiement (erreur HP 3F0)."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from clonix import efiboot

from tests.fakes import GPT_DUMP, FakeRunner

ESP_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
EFIBOOTMGR = f"""BootCurrent: 0003
BootOrder: 0001,0003,0002
Boot0001* Windows Boot Manager	HD(1,GPT,12345678-dead-beef-0000-000000000000,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)
Boot0002* Windows Boot Manager	HD(1,GPT,99999999-0000-0000-0000-000000000099,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)
Boot0003* USB SSD	PciRoot(0x0)/Pci(0x14,0x0)/USB(3,0)
"""


class EfiRunner(FakeRunner):
    def __init__(self):
        super().__init__(tools=["efibootmgr", "zstd"])

    def run(self, argv, **kw):
        out = super().run(argv, **kw)
        if argv[:2] == ["efibootmgr", "-v"]:
            return EFIBOOTMGR
        if argv[0] == "blkid":
            return ESP_UUID + "\n"
        return out


class EfiBootTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.efi = os.path.join(self.root, "efi")
        os.makedirs(self.efi)
        self.byid = os.path.join(self.root, "by-partuuid")
        os.makedirs(self.byid)
        # partition d'un 2e disque encore présente : son entrée doit rester
        open(os.path.join(self.byid, "99999999-0000-0000-0000-000000000099"), "w").close()
        self.esp = os.path.join(self.root, "esp")
        os.makedirs(os.path.join(self.esp, "EFI", "Microsoft", "Boot"))
        with open(os.path.join(self.esp, "EFI", "Microsoft", "Boot", "bootmgfw.efi"), "wb") as f:
            f.write(b"MZ-windows")

    def repair(self, r):
        os.makedirs(self.esp, exist_ok=True)
        with mock.patch("tempfile.mkdtemp", return_value=self.esp), mock.patch("os.rmdir"):
            return efiboot.repair(r, "/dev/sda", efi_sysfs=self.efi, byid=self.byid)

    def test_recreates_windows_entry_and_fallback(self):
        r = EfiRunner()
        self.assertEqual(self.repair(r), "Windows Boot Manager")
        runs = r.commands("run")
        self.assertIn(["mount", "-t", "vfat", "/dev/sda1", self.esp], runs)
        # entrée orpheline supprimée, celle du 2e disque gardée
        self.assertIn(["efibootmgr", "--quiet", "--bootnum", "0001", "--delete-bootnum"], runs)
        self.assertNotIn(["efibootmgr", "--quiet", "--bootnum", "0002", "--delete-bootnum"], runs)
        self.assertIn(["efibootmgr", "--quiet", "--create", "--disk", "/dev/sda", "--part", "1",
                       "--label", "Windows Boot Manager", "--loader", "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"], runs)
        with open(os.path.join(self.esp, "EFI", "Boot", "bootx64.efi"), "rb") as f:
            self.assertEqual(f.read(), b"MZ-windows")

    def test_existing_fallback_kept_case_insensitive(self):
        os.makedirs(os.path.join(self.esp, "efi", "BOOT"))
        with open(os.path.join(self.esp, "efi", "BOOT", "BOOTX64.EFI"), "wb") as f:
            f.write(b"original")
        self.repair(EfiRunner())
        with open(os.path.join(self.esp, "efi", "BOOT", "BOOTX64.EFI"), "rb") as f:
            self.assertEqual(f.read(), b"original")

    def test_bios_mode_does_nothing(self):
        r = EfiRunner()
        self.assertIsNone(efiboot.repair(r, "/dev/sda", efi_sysfs=os.path.join(self.root, "absent")))
        self.assertFalse([c for c in r.commands("run") if c[0] == "efibootmgr"])

    def test_no_loader_found(self):
        shutil.rmtree(os.path.join(self.esp, "EFI"))
        r = EfiRunner()
        self.assertIsNone(self.repair(r))
        self.assertFalse([c for c in r.commands("run") if c[:2] == ["efibootmgr", "--quiet"]])

    def test_parse_entries(self):
        entries = efiboot.parse_entries(EFIBOOTMGR)
        self.assertEqual(entries[0], ("0001", "Windows Boot Manager", "12345678-dead-beef-0000-000000000000"))
        self.assertEqual(entries[2], ("0003", "USB SSD", None))

    def test_dump_has_esp(self):
        self.assertIn("C12A7328", GPT_DUMP)



class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        self.esp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.esp, True)

    def diagnose(self, r, disk="/dev/sda"):
        with mock.patch("tempfile.mkdtemp", return_value=self.esp), mock.patch("os.rmdir"):
            return efiboot.diagnose(r, disk)

    def test_healthy_windows(self):
        os.makedirs(os.path.join(self.esp, "EFI", "Microsoft", "Boot"))
        for name in ("bootmgfw.efi", "BCD"):
            open(os.path.join(self.esp, "EFI", "Microsoft", "Boot", name), "w").close()
        r = FakeRunner(tools=["fsck.fat"])
        lines = self.diagnose(r)
        self.assertIn("✓ Partition EFI /dev/sda1 : système de fichiers FAT valide.", lines)
        self.assertIn("✓ chargeur Windows (bootmgfw.efi) présent sur /dev/sda1", lines)
        self.assertIn("✓ configuration de démarrage Windows (BCD) présent sur /dev/sda1", lines)
        self.assertIn("✗ chargeur de secours (bootx64.efi) absent sur /dev/sda1", lines)

    def test_broken_fat(self):
        from clonix.errors import CommandError

        class R(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "fsck.fat":
                    raise CommandError(argv, 1, "Logical sector size is zero.")
                return super().run(argv, **kw)
        lines = self.diagnose(R(tools=["fsck.fat"]))
        self.assertTrue(lines[0].startswith("✗ Partition EFI /dev/sda1 : système de fichiers FAT abîmé"))

    def test_mbr_disk(self):
        from tests.fakes import DOS_DUMP
        r = FakeRunner(dumps={"/dev/sdb": DOS_DUMP})
        lines = self.diagnose(r, "/dev/sdb")
        self.assertIn("format MBR", lines[0])

    def test_gpt_without_esp(self):
        dump = GPT_DUMP.replace("C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7")
        lines = self.diagnose(FakeRunner(dumps={"/dev/sda": dump}))
        self.assertIn("Aucune partition EFI", lines[0])


if __name__ == "__main__":
    unittest.main()
