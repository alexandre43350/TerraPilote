import os
import tempfile
import unittest
from unittest import mock

from clonix import bench
from clonix.errors import ClonixError

from tests.fakes import FakeRunner


class ParseTest(unittest.TestCase):
    def test_parse_zstd_bench_new_and_old_formats(self):
        new = " 1#clonix-echantillo :  536870912 ->  281402816 (x1.908),  612.4 MB/s, 1874.1 MB/s"
        old = "\r 1#sample : 536870912 -> 268435456 (2.000), 450.0 MB/s , 1500.0 MB/s "
        self.assertEqual(bench.parse_zstd_bench(new), (1.908, 612.4e6, 1874.1e6))
        self.assertEqual(bench.parse_zstd_bench(old.replace(" , ", ", ")), (2.0, 450e6, 1500e6))
        self.assertIsNone(bench.parse_zstd_bench("zstd: error"))


class EstimateTest(unittest.TestCase):
    def test_write_bound_capture_and_read_bound_restore(self):
        r = {"disk_read": 1800e6, "store_write": 60e6, "store_read": 40e6,
             "compress": 600e6, "decompress": 1800e6, "sha": 900e6, "ratio": 2.0}
        (cap, cap_by), (res, res_by) = bench.estimate(r)
        self.assertEqual(cap, 120e6)  # 60 Mo/s écrits = 120 Mo/s de données du PC (rapport 2:1)
        self.assertIn("écriture", cap_by)
        self.assertEqual(res, 80e6)
        self.assertIn("lecture du support", res_by)

    def test_cpu_bound(self):
        r = {"disk_read": 1800e6, "store_write": 900e6, "store_read": 900e6,
             "compress": 150e6, "decompress": 300e6, "sha": 2000e6, "ratio": 2.0}
        (cap, cap_by), (res, res_by) = bench.estimate(r)
        self.assertIn("compression", cap_by)
        self.assertIn("décompression", res_by)


class RunTest(unittest.TestCase):
    def test_requires_mounted_store(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        with self.assertRaisesRegex(ClonixError, "dépôt"):
            bench.run(FakeRunner(), "/dev/sda", tmp)

    def test_run_measures_and_reports(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        sample = os.path.join(tmp, "echantillon")
        zstd_out = " 1#echantillon :  536870912 ->  268435456 (x2.000),  600.0 MB/s, 1800.0 MB/s"

        class Bench(FakeRunner):
            def run(self, argv, **kw):
                if argv[0] == "dd" and any(a.startswith("of=" + sample) for a in argv):
                    with open(sample, "wb") as f:
                        f.write(os.urandom(1024 * 1024))
                    self.calls.append(("run", argv, None))
                    return ""
                if argv[0] == "sh":
                    self.calls.append(("run", argv, None))
                    return zstd_out
                return super().run(argv, **kw)

        r = Bench()
        with mock.patch.object(bench, "SAMPLE_FILE", sample), \
                mock.patch("os.path.ismount", return_value=True), \
                mock.patch.object(bench, "_drop_caches"):
            res = bench.run(r, "/dev/sda", tmp)
        self.assertEqual(res["ratio"], 2.0)
        self.assertIn("sha", res)
        dds = [c for c in r.commands("run") if c[0] == "dd"]
        self.assertTrue(any("if=/dev/sda" in c for c in dds))  # lecture seule du disque du PC
        self.assertFalse(any(a.startswith("of=/dev/sd") for c in dds for a in c))  # jamais d'écriture dessus
        out = r.reporter.out.getvalue()
        self.assertIn("Capture estimée", out)
        self.assertIn("Restauration estimée", out)
        self.assertFalse(os.path.exists(sample))  # échantillon supprimé


if __name__ == "__main__":
    unittest.main()
