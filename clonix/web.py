"""Interface graphique : serveur web local + API JSON.

Sur la clé USB, un navigateur plein écran (mode kiosque) affiche cette page.
Avec ``--remote``, la même interface est accessible depuis un autre PC du
réseau, protégée par un code d'accès affiché à l'écran.
"""

import collections
import json
import os
import secrets
import subprocess
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clonix import __version__
from clonix import disks as dk
from clonix import image as im
from clonix import multicast as mc
from clonix import ops, storage
from clonix.errors import Cancelled, ClonixError
from clonix.runner import Runner

STATIC = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_PORT = 8470
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


class ApiError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class Job:
    _ids = 0

    def __init__(self, kind, title):
        Job._ids += 1
        self.id = Job._ids
        self.kind = kind
        self.title = title
        self.state = "running"
        self.error = None
        self.logs = collections.deque(maxlen=400)
        self.warnings = []
        self.notes = []
        self.progress = None
        self.started = time.time()
        self.finished = None
        self.runner = None

    # Reporter
    def log(self, message):
        self.logs.append({"t": time.time(), "msg": str(message)})

    def progress_update(self, p):
        self.progress = p

    def as_dict(self):
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "state": self.state,
            "error": self.error, "warnings": list(self.warnings), "notes": list(self.notes),
            "logs": list(self.logs),
            "progress": self.progress,
            "started": self.started, "finished": self.finished,
        }


class _JobReporter:
    def __init__(self, job):
        self.job = job

    def log(self, message):
        self.job.log(message)

    def warn(self, message):
        self.job.warnings.append(str(message))
        self.job.log(f"⚠ {message}")

    def note(self, message):
        self.job.notes.append(str(message))
        self.job.log(str(message))

    def progress(self, p):
        self.job.progress_update(p)


class App:
    def __init__(self, image_dir=im.DEFAULT_DIR, runner_factory=None, code=None, dry_run=False):
        self.image_dir = image_dir
        self.remote_port = None  # défini par serve() si l'accès réseau est ouvert
        self.dry_run = dry_run
        self.runner_factory = runner_factory or (lambda reporter: Runner(dry_run=dry_run, reporter=reporter))
        self.code = code or f"{secrets.randbelow(10 ** 6):06d}"
        self.job = None
        self.lock = threading.Lock()
        self.inspect = self.runner_factory(_JobReporter(Job("inspect", "")))
        # Au démarrage, monter automatiquement l'espace « CLONIX-DATA » de la clé
        # s'il existe, pour que le dépôt soit prêt sans action de l'utilisateur.
        if not dry_run:
            storage.auto_mount_data(self.inspect, self.image_dir)

    # ----------------------------------------------------------------- état
    def state(self):
        r = self.inspect
        protected = dk.protected_disks(r, self.image_dir)
        disks = []
        for d in dk.list_disks(r):
            disks.append({
                "path": d.path, "name": d.name, "size": d.size, "model": d.model, "serial": d.serial,
                "transport": d.transport, "removable": d.removable, "readonly": d.readonly,
                "mounted": bool(d.mountpoints), "protected": protected.get(d.path),
                "writable": not (d.readonly or d.mountpoints or d.path in protected),
                "partitions": [{"path": p.path, "number": p.number, "size": p.size, "fstype": p.fstype,
                                "label": p.label} for p in d.partitions],
            })
        images = [{"name": n, "size": s, "created": m.get("created"), "description": m.get("description", ""),
                   "source": m.get("source", {}), "required": m.get("table", {}).get("required_bytes", 0),
                   "partitions": [{"number": p["number"], "fstype": p["fstype"], "size": p["size"],
                                   "mode": p["mode"]} for p in m.get("partitions", [])]}
                  for n, m, s in im.list_images(self.image_dir)]
        mounted = os.path.ismount(self.image_dir)
        return {
            "version": __version__,
            "dry_run": self.dry_run,
            "storage": {
                "dir": self.image_dir, "mounted": mounted,
                "fstype": dk.fstype_of_mount(r, self.image_dir) if mounted else "",
                "source": _mount_source(r, self.image_dir) if mounted else "",
                "free": _free_space(self.image_dir),
                "clonix_key": bool(storage.find_data_partition(r)),
            },
            "disks": disks,
            "images": images,
            "interface": mc.default_interface(),
            "remote": ({"port": self.remote_port, "code": self.code, "addresses": _addresses(r)}
                       if self.remote_port else None),
            "job": self.job.as_dict() if self.job else None,
        }

    # ---------------------------------------------------------------- tâches
    def start(self, kind, title, fn):
        with self.lock:
            if self.job and self.job.state == "running":
                raise ApiError("Une opération est déjà en cours", HTTPStatus.CONFLICT)
            job = Job(kind, title)
            job.runner = self.runner_factory(_JobReporter(job))
            self.job = job

        def work():
            try:
                fn(job.runner)
                job.state = "done"
                if job.progress:
                    job.progress = dict(job.progress, fraction=1.0, overall=1.0, eta=0)
            except Cancelled:
                job.state = "cancelled"
                job.log("Opération annulée.")
            except ClonixError as exc:
                job.state = "error"
                job.error = str(exc)
                job.log(f"Erreur : {exc}")
            except Exception as exc:  # pragma: no cover - filet de sécurité
                job.state = "error"
                job.error = f"Erreur interne : {exc}"
                job.log(traceback.format_exc())
            finally:
                job.finished = time.time()

        threading.Thread(target=work, daemon=True).start()
        return job.as_dict()

    def cancel(self):
        if self.job and self.job.state == "running":
            self.job.runner.cancel()
        return {"ok": True}

    # -------------------------------------------------------------- actions
    def _disk(self, path, writable):
        disk = dk.get_disk(self.inspect, path)
        if writable:
            dk.check_writable_target(self.inspect, disk, self.image_dir)
        return disk

    @staticmethod
    def _confirm(disk, body):
        if body.get("confirm") != disk.name:
            raise ApiError(f"Confirmation incorrecte : tapez « {disk.name} »")

    def action(self, name, body):
        d = self.image_dir
        if name == "capture":
            disk = self._disk(body["disk"], writable=False)
            dk.check_readable_source(disk)
            image = _clean_name(body.get("name", ""))
            return self.start("capture", f"Capture de {disk.path} → {image}",
                              lambda r: ops.capture(r, disk.path, image, d,
                                                    level=body.get("level", "auto"),
                                                    description=body.get("description", ""),
                                                    slim=bool(body.get("slim", False))))
        if name == "deploy":
            disk = self._disk(body["disk"], writable=True)
            self._confirm(disk, body)
            image = body["image"]
            return self.start("deploy", f"Déploiement de « {image} » → {disk.path}",
                              lambda r: ops.deploy(r, ops.open_image(image, d), disk.path, d,
                                                   new_uuids=bool(body.get("new_uuids")),
                                                   adapt_windows=bool(body.get("adapt", True))))
        if name == "clone":
            src = self._disk(body["source"], writable=False)
            dst = self._disk(body["target"], writable=True)
            self._confirm(dst, body)
            return self.start("clone", f"Clonage {src.path} → {dst.path}",
                              lambda r: ops.clone(r, src.path, dst.path, new_uuids=bool(body.get("new_uuids"))))
        if name == "mcast-send":
            receivers = int(body.get("receivers", 1))
            if receivers < 1:
                raise ApiError("Il faut au moins un récepteur")
            image = body["image"]
            return self.start("mcast-send", f"Diffusion multicast de « {image} »",
                              lambda r: mc.send(r, image, receivers, d, interface=mc.default_interface(),
                                                max_wait=int(body.get("max_wait", 600))))
        if name == "mcast-recv":
            disk = self._disk(body["disk"], writable=True)
            self._confirm(disk, body)
            return self.start("mcast-recv", f"Réception multicast → {disk.path}",
                              lambda r: mc.receive(r, disk.path, interface=mc.default_interface(),
                                                   adapt_windows=bool(body.get("adapt", True))))
        if name == "verify":
            image = body["image"]

            def verify(r):
                r.log(f"Vérification de « {image} » : relecture de chaque fichier…")
                dk.tune_readahead(r, d)
                r.plan([(f"Vérification de « {image} »", 1)])
                r.start_step(0)
                manifest, bad = im.verify(im.resolve(image, d), progress=r.byte_progress())
                if bad:
                    raise ClonixError("Image corrompue : " + ", ".join(f"{n} ({why})" for n, why in bad))
                r.log(f"Image intacte : {len(manifest['files'])} fichiers vérifiés.")
            return self.start("verify", f"Vérification de « {image} »", verify)
        if name == "winfix":
            from clonix import winfix
            disk = self._disk(body["disk"], writable=True)
            self._confirm(disk, body)
            return self.start("winfix", f"Réparation du démarrage de Windows → {disk.path}",
                              lambda r: winfix.adapt(r, disk.path))
        if name == "bench":
            from clonix import bench
            disk = self._disk(body["disk"], writable=False)
            return self.start("bench", f"Test des vitesses ({disk.path})", lambda r: bench.run(r, disk.path, d))
        if name == "accounts":
            from clonix import password as pw
            disk = self._disk(body["disk"], writable=False)
            return pw.accounts(self.inspect, disk.path)
        if name == "password":
            from clonix import password as pw
            disk = self._disk(body["disk"], writable=True)
            self._confirm(disk, body)
            user = (body.get("user") or "").strip()
            if not user:
                raise ApiError("Indiquez le compte à réinitialiser")
            act = "set" if body.get("action") == "set" else "clear"
            pwd = body.get("password") if act == "set" else None
            verb = "Suppression" if act == "clear" else "Changement"
            return self.start("password", f"{verb} du mot de passe de « {user} » → {disk.path}",
                              lambda r: pw.reset(r, disk.path, user, act, password=pwd))
        if name == "storage":
            return self._storage(body)
        if name == "power":
            if body.get("action") not in ("reboot", "poweroff"):
                raise ApiError("Action inconnue")
            if not self.dry_run:
                subprocess.Popen(["systemctl", body["action"]])
            return {"ok": True}
        raise ApiError("Action inconnue", HTTPStatus.NOT_FOUND)

    def _storage(self, body):
        r = self.inspect
        kind = body.get("kind")
        if kind == "umount":
            storage.umount(r, self.image_dir)
        elif kind == "clonix":
            storage.mount_data_partition(r, self.image_dir)
        elif kind == "local":
            storage.mount_local(r, body["target"], self.image_dir)
        elif kind == "smb":
            storage.mount_smb(r, body["target"], body.get("user") or None, body.get("password"),
                              body.get("domain") or None, self.image_dir)
        elif kind == "nfs":
            storage.mount_nfs(r, body["target"], self.image_dir)
        else:
            raise ApiError("Type de dépôt inconnu")
        return {"ok": True}


def _clean_name(name):
    name = name.strip()
    if not name or "/" in name or name.startswith(".") or len(name) > 80:
        raise ApiError("Nom d'image invalide")
    return name


def _mount_source(runner, path):
    out = runner.run(["findmnt", "-n", "-o", "SOURCE", "--target", path], readonly=True, check=False)
    return out.strip().splitlines()[0] if out.strip() else ""


def _addresses(runner):
    out = runner.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], readonly=True, check=False)
    return [line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3]


def _free_space(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):  # statvfs n'existe pas sous Windows
        return None


class Handler(BaseHTTPRequestHandler):
    app = None  # défini par serve()
    failures = collections.Counter()
    server_version = f"Clonix/{__version__}"

    def log_message(self, fmt, *args):  # silencieux
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        ip = self.client_address[0]
        if self.failures[ip] >= 20:
            return False
        if secrets.compare_digest(self.headers.get("X-Clonix-Code", ""), self.app.code):
            return True
        self.failures[ip] += 1
        time.sleep(1)
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            if not self._authorized():
                return self._send(HTTPStatus.UNAUTHORIZED, {"error": "Code d'accès requis"})
            if path == "/api/state":
                return self._guard(self.app.state)
            return self._send(HTTPStatus.NOT_FOUND, {"error": "Introuvable"})
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self._send(HTTPStatus.NOT_FOUND, b"Introuvable", "text/plain; charset=utf-8")
        with open(full, "rb") as f:
            data = f.read()
        if name == "index.html":
            # Le code d'accès n'est donné qu'à l'écran local.
            data = data.replace(b"__CLONIX_CODE__", self.app.code.encode() if self._local() else b"")
        self._send(HTTPStatus.OK, data, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._send(HTTPStatus.NOT_FOUND, {"error": "Introuvable"})
        if not self._authorized():
            return self._send(HTTPStatus.UNAUTHORIZED, {"error": "Code d'accès requis"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "JSON invalide"})
        name = path[len("/api/"):]
        if name == "cancel":
            return self._guard(self.app.cancel)
        self._guard(lambda: self.app.action(name, body))

    def _guard(self, fn):
        try:
            return self._send(HTTPStatus.OK, fn())
        except ApiError as exc:
            return self._send(exc.status, {"error": str(exc)})
        except KeyError as exc:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": f"Champ manquant : {exc.args[0]}"})
        except (ClonixError, ValueError) as exc:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def make_server(app, host="127.0.0.1", port=DEFAULT_PORT):
    handler = type("BoundHandler", (Handler,), {"app": app, "failures": collections.Counter()})
    return ThreadingHTTPServer((host, port), handler)


def serve(app, host="127.0.0.1", port=DEFAULT_PORT):
    httpd = make_server(app, host, port)
    remote = host in ("0.0.0.0", "::")
    if remote:
        app.remote_port = port
    print(f"Clonix : interface sur http://127.0.0.1:{port}/"
          + (f" et sur le réseau (code d'accès : {app.code})" if remote else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
