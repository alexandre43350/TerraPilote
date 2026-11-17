"""Windows installé en BIOS (MBR) rendu démarrable en UEFI (expérimental)."""

import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from clonix import efiboot, uefimbr
from clonix.errors import ClonixError

from tests.fakes import DOS_DUMP, GIB, FakeRunner, lsblk_disk, lsblk_part

BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
LOADER = "{1b2c3d4e-0000-0000-0000-000000000001}"


def s(text):
    return (text + "\x00").encode("utf-16-le")


def node(name, values=None, children=()):
    n = {"name": name, "values": dict(values or {}), "children": list(children)}
    return n


def element(type_, text):
    return node(type_, {"Element": {"key": "Element", "t": 1, "value": s(text)}})


def bios_bcd():
    return node("ROOT", children=[node("Description"), node("Objects", children=[
        node(BOOTMGR, children=[node("Description"), node("Elements", children=[
            element("12000004", "Windows Boot Manager"), node("11000001")])]),
        node(LOADER, children=[node("Elements", children=[
            element("12000002", "\\Windows\\system32\\winload.exe"), element("12000004", "Windows 10")])]),
        node("{resume}", children=[node("Elements", children=[
            element("12000002", "\\Windows\\system32\\winresume.exe")])]),
        node("{memdiag}", children=[node("Elements", children=[element("12000002", "\\boot\\memtest.exe")])]),
    ])])


class FakeHivex:
    def __init__(self, root):
        self.root_node, self.committed = root, False
        mod = self

        class H:
            def __init__(self, path, write=False):
                pass

            def root(self):
                return mod.root_node

            def node_children(self, n):
                return list(n["children"])

            def node_name(self, n):
                return n["name"]

            def node_values(self, n):
                return list(n["values"].values())

            def value_key(self, v):
                return v["key"]

            def value_value(self, v):
                return v["t"], v["value"]

            def node_set_value(self, n, val):
                n["values"][val["key"]] = val

            def node_add_child(self, n, name):
                c = node(name)
                n["children"].append(c)
                return c

            def commit(self, path):
                mod.committed = True
        self.Hivex = H


def text_of(n, type_):
    el = next(c for c in n["children"][-1]["children"] if c["name"] == type_)
    return el["values"]["Element"]["value"].decode("utf-16-le").rstrip("\x00")


class PatchBcdTest(unittest.TestCase):
    def test_paths_become_uefi(self):
        root = bios_bcd()
        fake = FakeHivex(root)
        changes = uefimbr.patch_bcd("/x/BCD", fake)
        objs = {o["name"]: o for o in root["children"][1]["children"]}
        self.assertEqual(text_of(objs[BOOTMGR], "12000002"), "\\EFI\\Microsoft\\Boot\\bootmgfw.efi")
        self.assertEqual(text_of(objs[LOADER], "12000002"), "\\Windows\\system32\\winload.efi")
        self.assertEqual(text_of(objs["{resume}"], "12000002"), "\\Windows\\system32\\winresume.efi")
        self.assertEqual(text_of(objs["{memdiag}"], "12000002"), "\\EFI\\Microsoft\\Boot\\memtest.efi")
        self.assertEqual(text_of(objs[LOADER], "12000004"), "Windows 10")  # description inchangée
        self.assertTrue(fake.committed)
        self.assertEqual(len(changes), 4)

    def test_refuses_bcd_without_windows_loader(self):
        root = node("ROOT", children=[node("Objects", children=[node(BOOTMGR, children=[node("Elements")])])])
        fake = FakeHivex(root)
        with self.assertRaisesRegex(ClonixError, "winload"):
            uefimbr.patch_bcd("/x/BCD", fake)
        self.assertFalse(fake.committed)


class MakeUefiTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.parts = {}
        for dev in ("sysres", "win", "esp"):
            os.makedirs(os.path.join(self.root, dev))
        sysres = os.path.join(self.root, "sysres")
        os.makedirs(os.path.join(sysres, "Boot"))
        open(os.path.join(sysres, "Boot", "BCD"), "wb").close()
        open(os.path.join(sysres, "bootmgr"), "wb").close()
        os.makedirs(os.path.join(sysres, "System Volume Information"))
        win = os.path.join(self.root, "win")
        os.makedirs(os.path.join(win, "Windows", "Boot", "EFI"))
        os.makedirs(os.path.join(win, "Windows", "Boot", "Fonts"))
        with open(os.path.join(win, "Windows", "Boot", "EFI", "bootmgfw.efi"), "wb") as f:
            f.write(b"MZ-uefi")
        open(os.path.join(win, "Windows", "Boot", "Fonts", "wgl4_boot.ttf"), "wb").close()
        self.tree = [lsblk_disk("sdb", 20 * GIB, [
            lsblk_part("sdb1", 500 * 1024 ** 2, "ntfs", label="Réservé au système"),
            lsblk_part("sdb2", 4 * GIB, "ntfs"), lsblk_part("sdb5", 4 * GIB, "ext4")])]

    def mounted(self):
        mapping = {"/dev/sdb1": None, "/dev/sdb2": os.path.join(self.root, "win")}
        state = {"formatted": False}

        @contextlib.contextmanager
        def fake(runner, device, fstype, readonly=False):
            runner.calls.append(("mount", device, fstype, readonly))
            if device == "/dev/sdb1":
                yield os.path.join(self.root, "esp" if fstype == "vfat" else "sysres")
            else:
                yield mapping[device]
        return fake

    def test_system_reserved_becomes_efi_partition(self):
        r = FakeRunner(tree=self.tree, dumps={"/dev/sdb": DOS_DUMP})
        with mock.patch.object(uefimbr, "_mounted", self.mounted()), \
                mock.patch.object(uefimbr, "patch_bcd", return_value=["winload.exe → winload.efi"]) as patch, \
                mock.patch.object(efiboot, "repair") as repair:
            uefimbr.make_uefi_bootable(r, "/dev/sdb")
        runs = r.commands("run")
        self.assertIn(["mkfs.fat", "-F", "32", "-n", "SYSTEM", "/dev/sdb1"], runs)
        self.assertIn(["sfdisk", "--part-type", "/dev/sdb", "1", "ef"], runs)
        esp = os.path.join(self.root, "esp")
        self.assertTrue(os.path.isfile(os.path.join(esp, "bootmgr")))  # contenu d'origine remis
        self.assertTrue(os.path.isfile(os.path.join(esp, "EFI", "Microsoft", "Boot", "BCD")))
        self.assertTrue(os.path.isfile(os.path.join(esp, "EFI", "Microsoft", "Boot", "Fonts", "wgl4_boot.ttf")))
        with open(os.path.join(esp, "EFI", "Boot", "bootx64.efi"), "rb") as f:
            self.assertEqual(f.read(), b"MZ-uefi")
        self.assertFalse(os.path.exists(os.path.join(esp, "System Volume Information")))
        patch.assert_called_once()
        repair.assert_called_once_with(r, "/dev/sdb")
        # sauvegarde lue avant tout formatage
        mounts = [c for c in r.calls if c[0] == "mount" or (c[0] == "run" and c[1][0] == "mkfs.fat")]
        self.assertEqual(mounts[0][:2], ("mount", "/dev/sdb1"))

    def test_without_system_reserved(self):
        shutil.rmtree(os.path.join(self.root, "sysres", "Boot"))
        r = FakeRunner(tree=self.tree, dumps={"/dev/sdb": DOS_DUMP})
        with mock.patch.object(uefimbr, "_mounted", self.mounted()):
            with self.assertRaisesRegex(ClonixError, "Réservé au système"):
                uefimbr.make_uefi_bootable(r, "/dev/sdb")
        self.assertFalse([c for c in r.commands("run") if c[0] == "mkfs.fat" or c[:2] == ["sfdisk", "--part-type"]])

    def test_gpt_disk_untouched(self):
        r = FakeRunner()
        with self.assertRaisesRegex(ClonixError, "pas au format MBR"):
            uefimbr.make_uefi_bootable(r, "/dev/sda")


if __name__ == "__main__":
    unittest.main()
