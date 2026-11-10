"""Exécution des commandes système.

Toutes les commandes passent par un ``Runner`` : cela permet le mode
``--dry-run`` (affiche ce qui serait fait sans rien écrire) et de simuler les
outils disque dans les tests.
"""

import os
import shlex
import shutil
import subprocess
import sys
import threading

from terrapilote.errors import CommandError, TerraPiloteError

CHUNK = 4 * 1024 * 1024


def fmt(argv):
    return " ".join(shlex.quote(str(a)) for a in argv)


class Runner:
    def __init__(self, dry_run=False, verbose=False, out=None):
        self.dry_run = dry_run
        self.verbose = verbose
        self.out = out or sys.stderr

    def log(self, message):
        print(message, file=self.out, flush=True)

    def which(self, tool):
        return shutil.which(tool)

    def run(self, argv, *, input=None, capture=True, readonly=False, check=True):
        """Lance une commande et retourne sa sortie standard (texte).

        ``readonly=True`` signale une commande d'inspection, exécutée même en
        mode ``--dry-run``.
        """
        argv = [str(a) for a in argv]
        if self.dry_run and not readonly:
            self.log(f"[dry-run] {fmt(argv)}")
            return ""
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

    def pipeline(self, cmds, *, feed=None, sink=None):
        """Enchaîne ``cmds`` (cmd1 | cmd2 | ...).

        ``feed`` : objet fichier binaire injecté sur l'entrée de la première
        commande. ``sink`` : objet fichier binaire qui reçoit la sortie de la
        dernière commande. Retourne le nombre d'octets écrits dans ``sink``.
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
        if self.verbose:
            self.log("+ " + " | ".join(fmt(c) for c in cmds))

        procs = []
        written = 0
        prev_stdout = None
        feeder = None
        try:
            for i, argv in enumerate(cmds):
                first, last = i == 0, i == len(cmds) - 1
                stdin = subprocess.PIPE if (first and feed is not None) else prev_stdout
                stdout = subprocess.PIPE if (not last or sink is not None) else None
                try:
                    p = subprocess.Popen(argv, stdin=stdin, stdout=stdout)
                except FileNotFoundError:
                    raise TerraPiloteError(f"Outil introuvable : {argv[0]}") from None
                if prev_stdout is not None:
                    prev_stdout.close()  # le processus suivant garde sa copie
                prev_stdout = p.stdout
                procs.append(p)

            if feed is not None:
                feeder = threading.Thread(target=_feed, args=(feed, procs[0].stdin), daemon=True)
                feeder.start()
            if sink is not None:
                src = procs[-1].stdout
                while True:
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    sink.write(buf)
                    written += len(buf)
                src.close()
            if feeder is not None:
                feeder.join()
        finally:
            for p in procs:
                p.wait()

        if feeder is not None and getattr(feeder, "error", None):
            raise feeder.error
        for argv, p in zip(cmds, procs):
            if p.returncode != 0:
                raise CommandError(argv, p.returncode)
        return written

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
        if self.verbose:
            self.log(f"+ {fmt(argv)}")
        try:
            return subprocess.Popen(argv, stdin=stdin, stdout=stdout)
        except FileNotFoundError:
            raise TerraPiloteError(f"Outil introuvable : {argv[0]}") from None


def _feed(src, stdin):
    me = threading.current_thread()
    try:
        while True:
            buf = src.read(CHUNK)
            if not buf:
                break
            stdin.write(buf)
    except BrokenPipeError:
        pass  # le code retour de la commande dira ce qui s'est passé
    except Exception as exc:  # erreur de lecture, somme de contrôle…
        me.error = exc
    finally:
        try:
            stdin.close()
        except BrokenPipeError:
            pass
