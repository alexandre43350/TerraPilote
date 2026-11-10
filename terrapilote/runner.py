"""Exécution des commandes système.

Toutes les commandes passent par un ``Runner`` : cela permet le mode
``--dry-run`` (affiche ce qui serait fait sans rien écrire), l'annulation
depuis l'interface, le suivi de progression et la simulation des outils disque
dans les tests.

Optimisations du chemin de données :
- tubes agrandis à 1 Mio entre les processus (moins de changements de contexte) ;
- lecture et écriture côté Python dans deux fils séparés avec une file
  tampon : le calcul SHA-256 et l'écriture du fichier se recouvrent avec la
  lecture (hashlib et les E/S libèrent le GIL) ;
- blocs de 8 Mio.
"""

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

try:
    import fcntl  # absent sous Windows (mode démo uniquement)
except ImportError:
    fcntl = None

from terrapilote.errors import Cancelled, CommandError, TerraPiloteError

CHUNK = 8 * 1024 * 1024
QUEUE_DEPTH = 6
PIPE_SIZE = 1024 * 1024
C_LOCALE = dict(os.environ, LC_ALL="C")  # sorties de dd/partclone non traduites

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PC_DONE = re.compile(r"Complete(?:d)?:\s*([\d.]+)%")
_PC_RATE = re.compile(r"([\d.]+)\s*([KMGT]?)i?B/min")
_PC_LEFT = re.compile(r"Remaining:\s*(\d+):(\d+):(\d+)")
_DD_BYTES = re.compile(r"^(\d+) bytes")
_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def fmt(argv):
    return " ".join(shlex.quote(str(a)) for a in argv)


def progress_kind(argv):
    name = os.path.basename(str(argv[0]))
    if name.startswith("partclone."):
        return "partclone"
    if name == "dd" and "status=progress" in argv:
        return "dd"
    return None


def parse_partclone(line):
    """Retourne (fraction, débit o/s, secondes restantes) ou None."""
    m = _PC_DONE.search(line)
    if not m:
        return None
    rate = left = None
    r = _PC_RATE.search(line)
    if r:
        rate = float(r.group(1)) * _UNITS[r.group(2)] / 60
    t = _PC_LEFT.search(line)
    if t:
        h, mi, s = (int(x) for x in t.groups())
        left = h * 3600 + mi * 60 + s
    return min(float(m.group(1)) / 100, 1.0), rate, left


def parse_dd(line):
    m = _DD_BYTES.match(line.strip())
    return int(m.group(1)) if m else None


class Reporter:
    """Reçoit les messages et la progression (console par défaut)."""

    def __init__(self, out=None):
        self.out = out or sys.stderr
        self._bar = False

    def log(self, message):
        if self._bar:
            print(file=self.out)
            self._bar = False
        print(message, file=self.out, flush=True)

    def progress(self, p):
        if not getattr(self.out, "isatty", lambda: False)():
            return
        width = 30
        filled = int(p["fraction"] * width)
        rate = f" {human_rate(p['rate'])}" if p.get("rate") else ""
        eta = f" reste {human_time(p['eta'])}" if p.get("eta") is not None else ""
        line = (f"\r  [{'█' * filled}{'░' * (width - filled)}] {p['overall'] * 100:5.1f} %"
                f"  {p['label']}{rate}{eta}")
        print(line[:120].ljust(100), end="", file=self.out, flush=True)
        self._bar = True


def human_rate(bps):
    for unit in ("o/s", "Ko/s", "Mo/s", "Go/s"):
        if bps < 1000 or unit == "Go/s":
            return f"{bps:.0f} {unit}" if unit == "o/s" else f"{bps:.1f} {unit}"
        bps /= 1000


def human_time(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Runner:
    def __init__(self, dry_run=False, verbose=False, out=None, reporter=None):
        self.dry_run = dry_run
        self.verbose = verbose
        self.reporter = reporter or Reporter(out)
        self.cancelled = False
        self._procs = set()
        self._lock = threading.Lock()
        self._steps = []
        self._step = -1
        self._step_started = 0.0
        self._help_cache = {}

    # ----------------------------------------------------------- rapports
    def log(self, message):
        self.reporter.log(message)

    def plan(self, steps):
        """Déclare les étapes longues : liste de (libellé, poids en octets)."""
        self._steps = [(label, max(int(w), 1)) for label, w in steps]
        self._step = -1

    def start_step(self, index):
        self._step = index
        self._step_started = time.monotonic()
        self.emit(0.0)

    def emit(self, fraction, rate=None, eta=None, done_bytes=None):
        if not self._steps or self._step < 0:
            return
        label, weight = self._steps[self._step]
        total = sum(w for _, w in self._steps)
        before = sum(w for _, w in self._steps[: self._step])
        elapsed = time.monotonic() - self._step_started
        if rate is None and elapsed > 0.5:
            done = done_bytes if done_bytes is not None else fraction * weight
            rate = done / elapsed
        if eta is None and rate and fraction < 1:
            eta = (1 - fraction) * weight / rate
        self.reporter.progress({
            "label": label,
            "step": self._step + 1,
            "steps": len(self._steps),
            "plan": [lbl for lbl, _ in self._steps],
            "fraction": fraction,
            "overall": (before + fraction * weight) / total,
            "rate": rate,
            "eta": eta,
        })

    # ------------------------------------------------------------- annulation
    def cancel(self):
        self.cancelled = True
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass

    def check_cancel(self):
        if self.cancelled:
            raise Cancelled("Opération annulée")

    def _track(self, p):
        with self._lock:
            self._procs.add(p)

    def _untrack(self, p):
        with self._lock:
            self._procs.discard(p)

    # ---------------------------------------------------------------- outils
    def which(self, tool):
        return shutil.which(tool)

    def supports(self, tool, option):
        """``tool --help`` mentionne-t-il ``option`` ? (résultat mis en cache)"""
        if tool not in self._help_cache:
            try:
                proc = subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=10)
                self._help_cache[tool] = proc.stdout + proc.stderr
            except (OSError, subprocess.TimeoutExpired):
                self._help_cache[tool] = ""
        return option in self._help_cache[tool]

    def run(self, argv, *, input=None, capture=True, readonly=False, check=True):
        """Lance une commande et retourne sa sortie standard (texte).

        ``readonly=True`` signale une commande d'inspection, exécutée même en
        mode ``--dry-run``.
        """
        argv = [str(a) for a in argv]
        if self.dry_run and not readonly:
            self.log(f"[dry-run] {fmt(argv)}")
            return ""
        self.check_cancel()
        if self.verbose:
            self.log(f"+ {fmt(argv)}")
        try:
            proc = subprocess.run(
                argv,
                input=input,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except FileNotFoundError:
            raise TerraPiloteError(f"Outil introuvable : {argv[0]}") from None
        if check and proc.returncode != 0:
            raise CommandError(argv, proc.returncode, proc.stderr if capture else "")
        return proc.stdout if capture else ""

    def pipeline(self, cmds, *, feed=None, sink=None, total=None):
        """Enchaîne ``cmds`` (cmd1 | cmd2 | ...).

        ``feed`` : objet fichier binaire injecté sur l'entrée de la première
        commande. ``sink`` : objet fichier binaire qui reçoit la sortie de la
        dernière commande. ``total`` : taille en octets des données copiées par
        ``dd`` (pour la progression). Retourne le nombre d'octets écrits dans
        ``sink``.
        """
        cmds = [[str(a) for a in c] for c in cmds]
        if self.dry_run:
            text = " | ".join(fmt(c) for c in cmds)
            if feed is not None:
                text = f"<flux> | {text}"
            if sink is not None:
                text = f"{text} > <fichier>"
            self.log(f"[dry-run] {text}")
            return 0
        self.check_cancel()
        if self.verbose:
            self.log("+ " + " | ".join(fmt(c) for c in cmds))

        procs, watchers, tails = [], [], {}
        written = [0]
        prev_stdout = None
        feeder = None
        try:
            for i, argv in enumerate(cmds):
                first, last = i == 0, i == len(cmds) - 1
                stdin = subprocess.PIPE if (first and feed is not None) else prev_stdout
                stdout = subprocess.PIPE if (not last or sink is not None) else None
                kind = progress_kind(argv)
                try:
                    p = subprocess.Popen(argv, stdin=stdin, stdout=stdout, env=C_LOCALE,
                                         stderr=subprocess.PIPE if kind else None)
                except FileNotFoundError:
                    raise TerraPiloteError(f"Outil introuvable : {argv[0]}") from None
                self._track(p)
                procs.append(p)
                for pipe in (p.stdin, p.stdout):
                    _grow_pipe(pipe)
                if prev_stdout is not None:
                    prev_stdout.close()  # le processus suivant garde sa copie
                prev_stdout = p.stdout
                if kind:
                    tails[i] = []
                    w = threading.Thread(target=self._watch, args=(p.stderr, kind, total, tails[i]),
                                         daemon=True)
                    w.start()
                    watchers.append(w)

            def count(buf):
                sink.write(buf)
                written[0] += len(buf)

            if feed is not None:
                stdin = procs[0].stdin
                feeder = _Pump(feed.read, stdin.write, on_end=stdin.close)
                feeder.start()
            if sink is not None:
                pump = _Pump(procs[-1].stdout.read, count)
                pump.run()
                procs[-1].stdout.close()
            if feeder is not None:
                feeder.join()
                if feeder.error:
                    raise feeder.error
        except BaseException:
            for p in procs:
                try:
                    p.kill()
                except OSError:
                    pass
            raise
        finally:
            for p in procs:
                p.wait()
                self._untrack(p)
            for w in watchers:
                w.join(timeout=5)

        self.check_cancel()
        for i, (argv, p) in enumerate(zip(cmds, procs)):
            if p.returncode != 0:
                raise CommandError(argv, p.returncode, "\n".join(tails.get(i, [])[-6:]))
        return written[0]

    def _watch(self, stream, kind, total, tail):
        """Lit le stderr de partclone/dd, en tire la progression."""
        buf = b""
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                self._progress_line(_ANSI.sub("", raw.decode("utf-8", "replace")).strip(), kind, total, tail)
        if buf:
            self._progress_line(_ANSI.sub("", buf.decode("utf-8", "replace")).strip(), kind, total, tail)
        stream.close()

    def _progress_line(self, line, kind, total, tail):
        if not line:
            return
        if kind == "partclone":
            parsed = parse_partclone(line)
            if parsed:
                self.emit(*parsed)
                return
        else:
            n = parse_dd(line)
            if n is not None:
                if total:
                    self.emit(min(n / total, 1.0), done_bytes=n)
                return
        tail.append(line)
        del tail[:-20]
        if self.verbose:
            self.log(f"  {line}")

    # ------------------------------------------------------------ accès direct
    def read_at(self, path, offset, size):
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    def write_at(self, path, offset, data):
        if self.dry_run:
            self.log(f"[dry-run] écrire {len(data)} octets dans {path} à l'offset {offset}")
            return
        if self.verbose:
            self.log(f"+ écrire {len(data)} octets dans {path} @ {offset}")
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def popen(self, argv, *, stdin=None, stdout=None):
        """Démarre une commande longue (multicast) et retourne le ``Popen``."""
        argv = [str(a) for a in argv]
        self.check_cancel()
        if self.verbose:
            self.log(f"+ {fmt(argv)}")
        try:
            p = subprocess.Popen(argv, stdin=stdin, stdout=stdout)
        except FileNotFoundError:
            raise TerraPiloteError(f"Outil introuvable : {argv[0]}") from None
        self._track(p)
        _grow_pipe(p.stdin)
        _grow_pipe(p.stdout)
        return p

    def release(self, p):
        self._untrack(p)


def _grow_pipe(pipe):
    if pipe is None or fcntl is None or not hasattr(fcntl, "F_SETPIPE_SZ"):
        return
    try:
        fcntl.fcntl(pipe.fileno(), fcntl.F_SETPIPE_SZ, PIPE_SIZE)
    except OSError:
        pass


class _Pump:
    """Copie read() → write() avec un fil lecteur et une file tampon.

    Le fil lecteur et l'écrivain avancent en parallèle : pendant qu'un bloc est
    haché et écrit, le suivant est déjà en cours de lecture.
    """

    def __init__(self, read, write, on_end=None):
        self.read = read
        self.write = write
        self.on_end = on_end
        self.error = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._safe_run, daemon=True)
        self._thread.start()

    def join(self):
        self._thread.join()

    def _safe_run(self):
        try:
            self.run()
        except BaseException as exc:
            self.error = exc

    def run(self):
        q = queue.Queue(QUEUE_DEPTH)
        stop = threading.Event()
        read_error = []

        def reader():
            try:
                while not stop.is_set():
                    buf = self.read(CHUNK)
                    q.put(buf)
                    if not buf:
                        return
            except BaseException as exc:
                read_error.append(exc)
                q.put(b"")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        finished = False
        try:
            while True:
                buf = q.get()
                if not buf:
                    break
                self.write(buf)
            finished = True
        except BrokenPipeError:
            pass  # le code retour de la commande dira ce qui s'est passé
        finally:
            stop.set()
            if finished:
                t.join()
            else:
                # Libère une place pour que le lecteur voie « stop » et s'arrête ;
                # s'il est bloqué en lecture, l'arrêt des processus le débloquera.
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            if self.on_end:
                try:
                    self.on_end()
                except BrokenPipeError:
                    pass
        if read_error:
            raise read_error[0]
