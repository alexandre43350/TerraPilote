import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from clonix import image as im
from clonix import ops, winfix
from clonix.errors import ClonixError

from tests.fakes import GIB, GPT_DUMP, FakeRunner, lsblk_disk, lsblk_part


def node(name, values=None, children=()):
    n = {"name": name, "values": dict(values or {}), "children": list(children), "parent": None}
    for c in n["children"]:
        c["parent"] = n
    return n


class FakeHivexModule:
    """Imite l'API python3-hivex sur un arbre en mémoire."""

    def __init__(self, root):
        self.root_node = root
        self.opened = []
        self.committed = False
        module = self

        class Hivex:
            def __init__(self, path, write=False):
                module.opened.append((path, write))

            def root(self):
                return module.root_node

            def node_children(self, n):
                return list(n["children"])

            def node_name(self, n):
                return n["name"]

            def node_set_value(self, n, val):
                n["values"][val["key"]] = val

            def node_delete_child(self, n):
                n["parent"]["children"].remove(n)

            def commit(self, path):
                module.committed = True

        self.Hivex = Hivex


def system_hive():
    def service(name, override=False):
        return node(name, {"Start": {"key": "Start", "t": 4, "value": struct.pack("<I", 3)}},
                    [node("StartOverride", {"0": {}})] if override else [])

    def control_set(name):
        return node(name, children=[node("Services", children=[
            service("stornvme", override=True), service("storahci", override=True), service("Tcpip")])])

    return node("ROOT", children=[control_set("ControlSet001"), control_set("ControlSet002"), node("Select")])


def child(n, name):
    return next(c for c in n["children"] if c["name"].lower() == name.lower())


class EnableDriversTest(unittest.TestCase):
    def test_enables_storage_drivers_in_every_control_set(self):
        root = system_hive()
        fake = FakeHivexModule(root)
        changes = winfix.enable_storage_drivers("/x/SYSTEM", fake)
        self.assertEqual(fake.opened, [("/x/SYSTEM", True)])
        self.assertTrue(fake.committed)
        for cs in ("ControlSet001", "ControlSet002"):
            services = child(child(root, cs), "Services")
            for drv in ("stornvme", "storahci"):
                svc = child(services, drv)
                self.assertEqual(svc["values"]["Start"], {"key": "Start", "t": 4, "value": struct.pack("<I", 0)})
                self.assertEqual(svc["children"], [])  # StartOverride supprimé
            self.assertEqual(child(services, "Tcpip")["values"]["Start"]["value"], struct.pack("<I", 3))
        self.assertEqual(len(changes), 8)

    def test_not_a_windows_hive(self):
        fake = FakeHivexModule(node("ROOT", children=[node("Software")]))
        with self.assertRaises(ClonixError):
            winfix.enable_storage_drivers("/x/SYSTEM", fake)
        self.assertFalse(fake.committed)


class AdaptTest(unittest.TestCase):
    def _fixture(self):
        root = tempfile.mkdtemp(prefix="fx-win-")
        self.addCleanup(shutil.rmtree, root)
        cfg = os.path.join(root, "WINDOWS", "System32", "config")  # casse différente exprès
        os.makedirs(cfg)
        open(os.path.join(cfg, "SYSTEM"), "wb").close()
        return root

    def _runner(self, fixture):
        tree = [lsblk_disk("sdc", 64 * GIB, [
            lsblk_part("sdc1", 100 * 1024 * 1024, "vfat"),
            lsblk_part("sdc3", 60 * GIB, "ntfs"),
        ])]

        class MountRunner(FakeRunner):
            mountpoints = []

            def run(self, argv, **kw):
                argv = [str(a) for a in argv]
                if argv[0] == "mount":
                    self.calls.append(("run", argv, None))
                    self.mountpoints.append(argv[-1])
                    shutil.copytree(fixture, argv[-1], dirs_exist_ok=True)
                    return ""
                return super().run(argv, **kw)

        return MountRunner(tree=tree)

    def test_adapt_mounts_windows_and_edits_registry(self):
        r = self._runner(self._fixture())
        fake = FakeHivexModule(system_hive())
        changes = winfix.adapt(r, "/dev/sdc", hivex_mod=fake)
        self.assertTrue(changes)
        mount = next(c[1] for c in r.calls if c[0] == "run" and c[1][0] == "mount")
        self.assertEqual(mount[:6], ["mount", "-t", "ntfs-3g", "-o", "remove_hiberfile", "/dev/sdc3"])
        self.assertTrue(fake.opened[0][0].endswith(os.path.join("config", "SYSTEM")))
        self.assertIn(["umount", mount[-1]], r.commands("run"))
        shutil.rmtree(mount[-1], ignore_errors=True)

    def test_adapt_without_windows(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        r = self._runner(empty)
        with self.assertRaisesRegex(ClonixError, "Aucune installation Windows"):
            winfix.adapt(r, "/dev/sdc", hivex_mod=FakeHivexModule(system_hive()))
        for mp in r.mountpoints:
            shutil.rmtree(mp, ignore_errors=True)


class DeployAdaptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tree = [
            lsblk_disk("sda", 20 * GIB, [lsblk_part("sda1", 100 * 1024 * 1024, "vfat"),
                                         lsblk_part("sda2", 10 * GIB, "ntfs")]),
            lsblk_disk("sdc", 32 * GIB),
        ]
        self.tree = tree
        r = FakeRunner(tree=tree, tools=["zstd", "partclone.fat", "partclone.ntfs"])
        self.path, _ = ops.capture(r, "/dev/sda", "win", self.tmp.name)

    def _deploy(self, **kw):
        r = FakeRunner(tree=self.tree, tools=["zstd", "partclone.fat", "partclone.ntfs"])
        ops.deploy(r, im.DirSource(self.path), "/dev/sdc", self.tmp.name, **kw)
        return r

    def test_deploy_adapts_windows_when_asked(self):
        with mock.patch.object(winfix, "adapt") as adapt:
            self._deploy(adapt_windows=True)
        adapt.assert_called_once()
        self.assertEqual(adapt.call_args[0][1], "/dev/sdc")

    def test_deploy_without_adapt(self):
        with mock.patch.object(winfix, "adapt") as adapt:
            self._deploy()
        adapt.assert_not_called()

    def test_adapt_failure_does_not_fail_deploy(self):
        with mock.patch.object(winfix, "adapt", side_effect=ClonixError("pas de hivex")):
            r = self._deploy(adapt_windows=True)
        self.assertIn("Adaptation de Windows impossible : pas de hivex", r.reporter.out.getvalue())


if __name__ == "__main__":
    unittest.main()
