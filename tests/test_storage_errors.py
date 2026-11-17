"""Erreurs d'entrée/sortie sur le support des images : message clair et diagnostic du noyau."""

import errno
import os
import shutil
import tempfile
import unittest
from unittest import mock

from clonix import disks as dk
from clonix import image as im
from clonix import ops
from clonix.errors import ClonixError

from tests.fakes import FakeRunner

DMESG = """[  10.0] usb 2-1: new SuperSpeed USB device number 2 using xhci_hcd
[ 812.3] usb 2-1: reset SuperSpeed USB device number 2 using xhci_hcd
[ 815.9] usb 2-1: USB disconnect, device number 2
[ 815.9] blk_update_request: I/O error, dev sdb, sector 123456 op 0x1:(WRITE)
[ 816.0] exFAT-fs (dm-0): failed to write
[ 900.0] wlan0: associated
"""


class KernelRunner(FakeRunner):
    dmesg = DMESG

    def run(self, argv, **kw):
        out = super().run(argv, **kw)
        return self.dmesg if argv[0] == "dmesg" else out


class StoreErrorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_disconnect_during_capture(self):
        r = KernelRunner()
        broken = mock.patch.object(im.HashingWriter, "write", side_effect=OSError(errno.EIO, "Input/output error"))
        with broken, self.assertRaisesRegex(ClonixError, "s'est déconnecté pendant la copie"):
            ops.capture(r, "/dev/sda", "essai", self.dir)
        self.assertEqual(os.listdir(self.dir), [])  # image incomplète supprimée
        log = r.reporter.out.getvalue()
        self.assertIn("USB disconnect", log)
        self.assertNotIn("wlan0", log)

    def test_reset_and_exfat_messages(self):
        r = KernelRunner()
        r.dmesg = "[1] usb 2-1: reset SuperSpeed USB device\n"
        self.assertIn("réinitialisée", str(dk.store_io_error(r, OSError(errno.EIO, "x"))))
        r.dmesg = "[1] exFAT-fs (sdb1): error, invalid access to FAT\n"
        self.assertIn("Vérifiez le support depuis Windows", str(dk.store_io_error(r, OSError(errno.EIO, "x"))))
        r.dmesg = ""
        self.assertIn("Erreur matérielle pendant la lecture depuis",
                      str(dk.store_io_error(r, OSError(errno.EIO, "Input/output error"), writing=False)))

    def test_full_store(self):
        msg = str(dk.store_io_error(KernelRunner(), OSError(errno.ENOSPC, "No space left on device")))
        self.assertIn("plein", msg)

    def test_read_error_during_deploy(self):
        path, _ = ops.capture(FakeRunner(), "/dev/sda", "essai", self.dir)
        r = KernelRunner()
        real_read = im.VerifyingReader.read

        def failing(self, *a):
            if self.name.startswith("p2"):
                raise OSError(errno.EIO, "Input/output error")
            return real_read(self, *a)

        with mock.patch.object(im.VerifyingReader, "read", failing):
            with self.assertRaisesRegex(ClonixError, "déconnecté"):
                ops.deploy(r, im.DirSource(path), "/dev/sdc", self.dir)



class SeqRunner(FakeRunner):
    """dmesg renvoie un journal qui grossit à chaque appel."""

    def __init__(self, logs):
        super().__init__()
        self.logs = list(logs)

    def run(self, argv, **kw):
        out = super().run(argv, **kw)
        if argv[0] == "dmesg":
            return self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        return out


class UsbWatchTest(unittest.TestCase):
    def test_warns_once_on_resets(self):
        base = "[1] usb 2-1: new SuperSpeed USB device\n"
        reset = "[9] usb 2-1: reset SuperSpeed USB device number 2 using xhci_hcd\n"
        r = SeqRunner([base, base, base + reset, base + reset * 3])
        warnings = []
        r.warn = warnings.append
        w = dk.UsbWatch(r)
        self.assertEqual(w.check(), 0)
        self.assertEqual(w.check(), 1)
        self.assertEqual(w.check(), 3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("coupée ou réinitialisée 1 fois", warnings[0])
        self.assertIn("3 coupures", r.reporter.out.getvalue())

    def test_watch_runs_during_capture(self):
        reset = "[9] usb 2-1: USB disconnect, device number 2\n"
        r = SeqRunner(["", reset])
        warnings = []
        r.warn = warnings.append
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        ops.capture(r, "/dev/sda", "essai", tmp)
        self.assertTrue(any("coupée ou réinitialisée" in w for w in warnings))

    def test_no_dmesg_is_harmless(self):
        class NoDmesg(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "dmesg":
                    raise ClonixError("Outil introuvable : dmesg")
                return super().run(argv, **kw)
        self.assertEqual(dk.UsbWatch(NoDmesg()).check(), 0)

    def test_runner_watch_interval(self):
        from clonix.runner import Runner
        import io
        r = Runner(out=io.StringIO())
        calls = []
        r.add_watch(lambda: calls.append(1), every=0)
        r.plan([("x", 1)])
        r.start_step(0)
        r.emit(0.5)
        self.assertEqual(len(calls), 2)  # start_step émet aussi
        r.add_watch(lambda: 1 / 0, every=0)
        r.emit(0.6)  # une surveillance qui plante ne casse rien


if __name__ == "__main__":
    unittest.main()
