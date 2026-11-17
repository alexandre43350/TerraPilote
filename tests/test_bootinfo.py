"""Mesure de la durée du démarrage."""

import unittest
from unittest import mock

from clonix import bootinfo

from tests.fakes import FakeRunner

ANALYZE = ("Startup finished in 4.512s (firmware) + 1min 2.250s (loader) + 3.801s (kernel) "
           "+ 12.345s (userspace) = 1min 22.908s\ngraphical.target reached after 12.2s in userspace.\n")
DMESG = """[    1.204000] usb 2-1: new SuperSpeed USB device
[    3.100000] ISO 9660 Extensions: Microsoft Joliet Level 3
[    3.100200] ISO 9660 Extensions: RRIP_1991A
[    5.950000] loop0: detected capacity change from 0 to 1843200
[    5.960000] squashfs: version 4.0 (2009/01/31) Phillip Lougher
"""


class BootInfoTest(unittest.TestCase):
    def test_durations(self):
        self.assertAlmostEqual(bootinfo.parse_duration("1min 2.5s"), 62.5)
        self.assertAlmostEqual(bootinfo.parse_duration("345ms"), 0.345)
        parts = bootinfo.parse_systemd_analyze(ANALYZE)
        self.assertAlmostEqual(parts["firmware"], 4.512)
        self.assertAlmostEqual(parts["loader"], 62.25)
        self.assertAlmostEqual(parts["kernel"], 3.801)
        self.assertAlmostEqual(parts["userspace"], 12.345)
        self.assertEqual(bootinfo.parse_systemd_analyze("Bootup is not yet finished"), {})

    def test_toram_from_kernel_log(self):
        self.assertAlmostEqual(bootinfo.toram_seconds(DMESG), 2.85)
        self.assertIsNone(bootinfo.toram_seconds("[1.0] usb: rien\n"))

    def test_gather_in_ram(self):
        class R(FakeRunner):
            def run(self, argv, **kw):
                out = super().run(argv, **kw)
                return {"systemd-analyze": ANALYZE, "dmesg": DMESG}.get(argv[0], out)

        r = R()
        r.mount_fstype = "tmpfs"
        with mock.patch("os.path.isdir", return_value=True), \
                mock.patch.object(bootinfo, "ram_copy_bytes", return_value=900_000_000):
            info = bootinfo.gather(r, screen_ready=21.4)
        self.assertEqual((info["in_ram"], info["ram_bytes"], info["screen"]), (True, 900_000_000, 21.4))
        self.assertAlmostEqual(info["toram"], 2.85)
        self.assertAlmostEqual(info["kernel"], 3.801)

    def test_gather_from_key(self):
        r = FakeRunner()  # /run/live/medium en ext4 : pas en mémoire
        with mock.patch("os.path.isdir", return_value=True):
            info = bootinfo.gather(r, screen_ready=18.0)
        self.assertFalse(info["in_ram"])
        self.assertIsNone(info["toram"])


if __name__ == "__main__":
    unittest.main()
