"""Test des vitesses : trouver ce qui limite une capture ou une restauration.

Mesure en une minute environ chaque maillon de la chaîne de copie :
- lecture du disque du PC ;
- écriture puis lecture sur le support du dépôt d'images ;
- compression, décompression (zstd) et contrôle SHA-256 par le processeur, sur
  un échantillon réel du disque du PC.
Puis estime le débit d'une capture et d'une restauration, et nomme le maillon
le plus lent. Rien n'est écrit sur le disque du PC ; seul un fichier de test
temporaire est écrit (puis supprimé) dans le dépôt.
"""

import hashlib
import os
import re
import shlex
import time

from clonix import disks as dk
from clonix.errors import ClonixError

MIB = 1024 * 1024
DISK_TEST = 1024 * MIB
STORE_TEST = 2048 * MIB
SAMPLE_SIZE = 512 * MIB
BENCH_FILE = ".clonix-test-vitesse.tmp"
SAMPLE_FILE = "/tmp/clonix-echantillon"

_ZSTD_BENCH = re.compile(r"\(x?([\d.]+)\),\s*([\d.]+)\s*MB/s,\s*([\d.]+)\s*MB/s")


def parse_zstd_bench(text):
    """(rapport de compression, compression o/s, décompression o/s) depuis zstd -b."""
    found = _ZSTD_BENCH.findall(text.replace("\r", "\n"))
    if not found:
        return None
    ratio, comp, dec = found[-1]
    return float(ratio), float(comp) * 1e6, float(dec) * 1e6


def mbps(value):
    return f"{value / 1e6:,.0f}".replace(",", " ") + " Mo/s"


def estimate(r):
    """Débits estimés (données du PC par seconde) et maillon limitant.

    Retourne ((débit capture, maillon), (débit restauration, maillon)).
    """
    c = 1 / r["ratio"] if r.get("ratio") else 0.6  # octets écrits par octet du PC
    capture = {
        "la lecture du disque du PC": r.get("disk_read"),
        "la compression (processeur)": r.get("compress"),
        "l'écriture sur le support des images": r["store_write"] / c if r.get("store_write") else None,
        "le contrôle SHA-256 (processeur)": r["sha"] / c if r.get("sha") else None,
    }
    restore = {
        "la lecture du support des images": r["store_read"] / c if r.get("store_read") else None,
        "la décompression (processeur)": r.get("decompress"),
        "le contrôle SHA-256 (processeur)": r["sha"] / c if r.get("sha") else None,
    }

    def slowest(d):
        known = [(v, k) for k, v in d.items() if v]
        return min(known) if known else (None, None)

    return slowest(capture), slowest(restore)


def _timed(runner, argv, nbytes):
    start = time.monotonic()
    runner.run(argv)
    return nbytes / max(time.monotonic() - start, 1e-6)


def _drop_caches():
    os.sync()
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except OSError:
        pass


def _sample_partition(disk):
    parts = [p for p in disk.partitions if p.fstype not in ("swap", "")] or disk.partitions
    return max(parts, key=lambda p: p.size) if parts else None


def run(runner, disk_path, image_dir):
    disk = dk.get_disk(runner, disk_path)
    if not image_dir or not os.path.ismount(image_dir):
        raise ClonixError("Montez d'abord le dépôt d'images : le test mesure aussi son support.")
    store_disk = dk.disk_of_mount(runner, image_dir)
    usb = dk.usb_speed(store_disk) if store_disk else None
    dk.tune_readahead(runner, image_dir)

    steps = ["Lecture du disque du PC", "Écriture sur le support des images",
             "Lecture du support des images", "Processeur : compression", "Processeur : contrôle"]
    runner.plan([(s, 1) for s in steps])
    results = {}
    bench_path = os.path.join(image_dir, BENCH_FILE)
    try:
        runner.start_step(0)
        results["disk_read"] = _timed(
            runner, ["dd", f"if={disk.path}", "of=/dev/null", "bs=16M", f"count={DISK_TEST // (16 * MIB)}",
                     "iflag=direct"], DISK_TEST)

        runner.start_step(1)
        results["store_write"] = _timed(
            runner, ["dd", "if=/dev/zero", f"of={bench_path}", "bs=16M",
                     f"count={STORE_TEST // (16 * MIB)}", "conv=fdatasync"], STORE_TEST)

        runner.start_step(2)
        _drop_caches()
        results["store_read"] = _timed(runner, ["dd", f"if={bench_path}", "of=/dev/null", "bs=16M"], STORE_TEST)

        runner.start_step(3)
        part = _sample_partition(disk)
        if part:
            skip = 64 if part.size > 2 * DISK_TEST else 0  # sauter le début (métadonnées)
            runner.run(["dd", f"if={part.path}", f"of={SAMPLE_FILE}", "bs=16M",
                        f"count={SAMPLE_SIZE // (16 * MIB)}", f"skip={skip}", "iflag=direct"])
            out = runner.run(["sh", "-c", f"zstd -b1 -T0 {shlex.quote(SAMPLE_FILE)} 2>&1"], check=False)
            parsed = parse_zstd_bench(out)
            if parsed:
                results["ratio"], results["compress"], results["decompress"] = parsed

            runner.start_step(4)
            h = hashlib.sha256()
            start = time.monotonic()
            done = 0
            with open(SAMPLE_FILE, "rb") as f:
                while True:
                    buf = f.read(8 * MIB)
                    if not buf:
                        break
                    h.update(buf)
                    done += len(buf)
            if done:
                results["sha"] = done / max(time.monotonic() - start, 1e-6)
        runner.emit(1.0)
    finally:
        for path in (bench_path, SAMPLE_FILE):
            try:
                os.remove(path)
            except OSError:
                pass

    _report(runner, disk, store_disk, usb, results)
    return results


def _report(runner, disk, store_disk, usb, r):
    link = f", USB {usb} Mbit/s" if usb else ""
    runner.note(f"Lecture du disque du PC ({disk.name}) : {mbps(r['disk_read'])}")
    runner.note(f"Écriture sur le support des images ({os.path.basename(store_disk or '?')}{link}) : "
                f"{mbps(r['store_write'])}")
    runner.note(f"Lecture du support des images : {mbps(r['store_read'])}")
    if r.get("compress"):
        runner.note(f"Processeur : compression {mbps(r['compress'])}, décompression {mbps(r['decompress'])}, "
                    f"contrôle SHA-256 {mbps(r.get('sha', 0))} (données compressées au rapport "
                    f"{r['ratio']:.1f}:1)".replace(".", ","))
    (cap, cap_by), (res, res_by) = estimate(r)
    if cap:
        runner.note(f"→ Capture estimée : environ {mbps(cap)} de données du PC, limitée par {cap_by}.")
    if res:
        runner.note(f"→ Restauration estimée : environ {mbps(res)} de données du PC, limitée par {res_by}.")
