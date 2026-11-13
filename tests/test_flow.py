"""Diagnostic « ce qui freine » pendant une copie."""

import io
import os
import unittest

from clonix.runner import Flow, Runner, proc_cpu_seconds


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def flow(direction, cpu, cpus=4, procs=((["partclone.ntfs"], 1), (["zstd"], 2))):
    clock = Clock()
    used = {pid: 0.0 for _, pid in procs}
    f = Flow(direction, procs, cpus=cpus, clock=clock, cpu_of=lambda pid: used[pid])
    return f, clock, used


class FlowTest(unittest.TestCase):
    def test_slow_store_on_capture(self):
        f, clock, _ = flow("out", cpu=0)

        def write(buf):
            clock.t += 0.9  # bloqué dans l'écriture
        w = f.timed(write)
        for _ in range(10):
            w(b"x" * 1000)
            clock.t += 0.1
        snap = f.snapshot()
        self.assertEqual(snap["limit"], "l'écriture sur le support des images")
        self.assertAlmostEqual(snap["store_rate"], 1000)

    def test_cpu_bound_compression(self):
        f, clock, used = flow("out", cpu=0)
        clock.t = 10
        used[2] = 36.0  # zstd : 3,6 cœurs sur 4
        used[1] = 3.0
        self.assertEqual(f.snapshot()["limit"], "le processeur (compression)")

    def test_source_disk_when_nothing_else(self):
        f, clock, used = flow("out", cpu=0)
        clock.t = 10
        used[2] = 5.0
        self.assertEqual(f.snapshot()["limit"], "la lecture du disque du PC")

    def test_slow_store_read_on_deploy(self):
        f, clock, _ = flow("in", cpu=0)

        def read(n):
            clock.t += 1
            return b"y" * 500
        r = f.timed(read)
        for _ in range(5):
            r(8)
        snap = f.snapshot()
        self.assertEqual(snap["limit"], "la lecture du support des images")
        self.assertAlmostEqual(snap["store_rate"], 500)

    def test_single_core_tool(self):
        f, clock, used = flow("out", cpu=0)
        clock.t = 10
        used[1] = 9.6  # partclone : un cœur à fond
        used[2] = 6.0
        snap = f.snapshot()
        self.assertEqual(snap["limit"], "le processeur (partclone.ntfs, qui n'utilise qu'un cœur)")
        self.assertIn("partclone.ntfs 0,96 cœur", f.describe(snap))

    def test_too_early(self):
        f, clock, _ = flow("out", cpu=0)
        clock.t = 1
        self.assertIsNone(f.snapshot())

    def test_reads_own_cpu_time(self):
        self.assertIsNotNone(proc_cpu_seconds(os.getpid()))
        self.assertIsNone(proc_cpu_seconds(-1))

    def test_real_pipeline_records_measure(self):
        r = Runner(out=io.StringIO())
        r.plan([("copie", 1)])
        out = io.BytesIO()
        r.pipeline([["sh", "-c", "head -c 3000000 /dev/zero; sleep 3.2"]], sink=out)
        self.assertEqual(len(out.getvalue()), 3000000)
        self.assertIsNotNone(r.slowest)
        self.assertIn("limit", r.slowest)


if __name__ == "__main__":
    unittest.main()
