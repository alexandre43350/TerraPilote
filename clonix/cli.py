"""Interface en ligne de commande : ``clonix <commande>``."""

import argparse
import os
import sys

from clonix import __version__
from clonix import disks as dk
from clonix import image as im
from clonix import multicast as mc
from clonix import ops, storage
from clonix.errors import Cancelled, ClonixError
from clonix.runner import Runner


def ask_erase(disk, what, assume_yes=False, stream_in=None, stream_out=None):
    """Demande de taper le nom du disque avant de l'effacer."""
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stderr
    print(f"\n  ATTENTION : {what}", file=stream_out)
    print(f"  Le disque {disk.describe()} va être ENTIÈREMENT EFFACÉ.", file=stream_out)
    for p in disk.partitions:
        print(f"    - {p.path} {dk.human_size(p.size)} {p.fstype} {p.label}".rstrip(), file=stream_out)
    if assume_yes:
        return
    print(f"  Tapez « {disk.name} » pour confirmer : ", end="", file=stream_out, flush=True)
    answer = stream_in.readline().strip()
    if answer != disk.name:
        raise Cancelled("Opération annulée")


def cmd_disks(runner, args):
    protected = dk.protected_disks(runner, args.dir)
    for d in dk.list_disks(runner):
        flags = []
        if d.path in protected:
            flags.append(f"protégé : {protected[d.path]}")
        if d.mountpoints:
            flags.append("monté")
        print(d.describe() + (f"  [{', '.join(flags)}]" if flags else ""))
        for p in d.partitions:
            print(f"    {p.path:<16} {dk.human_size(p.size):>10}  {p.fstype:<8} {p.label}".rstrip())
    return 0


def cmd_images(runner, args):
    images = im.list_images(args.dir)
    if not images:
        print(f"Aucune image dans {args.dir}")
    for name, m, size in images:
        src = m["source"]
        print(f"{name:<28} {dk.human_size(size):>10}  {m['created'][:16]}  "
              f"{src.get('model') or ''} {dk.human_size(src['size'])}  {m.get('description', '')}".rstrip())
    return 0


def cmd_capture(runner, args):
    ops.capture(runner, args.disk, args.name, args.dir, compression=args.compression,
                level=args.level, description=args.description)
    return 0


def cmd_deploy(runner, args):
    source = ops.open_image(args.name, args.dir)
    ops.deploy(
        runner, source, args.disk, args.dir, new_uuids=args.new_uuids,
        confirm=lambda m, d: ask_erase(d, f"déploiement de l'image « {m['name']} »", args.yes or runner.dry_run),
    )
    return 0


def cmd_clone(runner, args):
    ops.clone(
        runner, args.source, args.target, new_uuids=args.new_uuids,
        confirm=lambda s, d: ask_erase(d, f"clonage de {s.path}", args.yes or runner.dry_run),
    )
    return 0


def cmd_verify(runner, args):
    path = im.resolve(args.name, args.dir)
    manifest, bad = im.verify(path)
    for name, reason in bad:
        print(f"ERREUR {name} : {reason}")
    if bad:
        return 1
    print(f"Image « {manifest['name']} » intacte ({len(manifest['files'])} fichiers vérifiés)")
    return 0


def _net_opts(args):
    return {"interface": args.interface or mc.default_interface(), "portbase": args.portbase}


def cmd_mcast_send(runner, args):
    mc.send(runner, args.name, args.receivers, args.dir, max_wait=args.max_wait,
            max_bitrate=args.max_bitrate, **_net_opts(args))
    return 0


def cmd_mcast_recv(runner, args):
    disk = dk.get_disk(runner, args.disk)
    dk.check_writable_target(runner, disk)
    ask_erase(disk, "réception d'une image multicast", args.yes or runner.dry_run)
    mc.receive(runner, disk.path, new_uuids=args.new_uuids, **_net_opts(args))
    return 0


def cmd_mount(runner, args):
    if args.kind == "clonix":
        storage.mount_data_partition(runner, args.dir)
        runner.log(f"Dépôt d'images monté sur {args.dir}")
        return 0
    if not args.target:
        raise ClonixError("Indiquez l'emplacement du dépôt (partition, partage, export…)")
    if args.kind == "local":
        storage.mount_local(runner, args.target, args.dir)
    elif args.kind == "smb":
        password = None
        if args.user:
            import getpass
            password = os.environ.get("CLONIX_SMB_PASSWORD") or getpass.getpass("Mot de passe : ")
        storage.mount_smb(runner, args.target, args.user, password, args.domain, args.dir)
    elif args.kind == "nfs":
        storage.mount_nfs(runner, args.target, args.dir)
    else:
        storage.mount_sshfs(runner, args.target, args.dir)
    runner.log(f"Dépôt d'images monté sur {args.dir}")
    return 0


def cmd_umount(runner, args):
    storage.umount(runner, args.dir)
    return 0


def cmd_web(runner, args):
    from clonix import web
    if args.demo:
        from clonix.demo import DemoApp
        app = DemoApp()
    else:
        app = web.App(args.dir, dry_run=args.dry_run)
    host = args.host or ("0.0.0.0" if args.remote else "127.0.0.1")
    web.serve(app, host, args.port)
    return 0


def cmd_password(runner, args):
    from clonix import password as pw
    if args.list:
        info = pw.accounts(runner, args.disk)
        print(f"Système : {info['os']} sur {info['partition']}")
        for a in info["accounts"]:
            print(f"  {a['name']}" + ("  (administrateur)" if a["admin"] else ""))
        return 0
    if not args.user:
        raise ClonixError("Indiquez le compte avec --user (ou --list pour les voir)")
    action = "set" if args.set else "clear"
    new = None
    if args.set:
        import getpass
        new = os.environ.get("CLONIX_NEW_PASSWORD") or getpass.getpass(f"Nouveau mot de passe pour {args.user} : ")

    def confirm(disk, os_type, user, act):
        verb = "SUPPRIMER le mot de passe de" if act == "clear" else "CHANGER le mot de passe de"
        if args.yes or runner.dry_run:
            return
        print(f"\n  Clonix va {verb} « {user} » sur {disk.path} ({os_type}).", file=sys.stderr)
        print(f"  Tapez « {disk.name} » pour confirmer : ", end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip() != disk.name:
            raise Cancelled("Opération annulée")

    pw.reset(runner, args.disk, args.user, action, password=new, confirm=confirm)
    return 0


def cmd_menu(runner, args):
    from clonix import tui
    return tui.main(runner, args.dir)


def build_parser():
    p = argparse.ArgumentParser(prog="clonix", description="Clonage et déploiement d'images disque.")
    p.add_argument("--version", action="version", version=f"clonix {__version__}")
    p.add_argument("--dry-run", action="store_true", help="afficher les actions sans rien écrire")
    p.add_argument("-v", "--verbose", action="store_true", help="afficher chaque commande lancée")
    p.add_argument("--dir", default=im.DEFAULT_DIR, help=f"dépôt d'images (défaut : {im.DEFAULT_DIR})")
    sub = p.add_subparsers(dest="command", metavar="commande")

    def add(name, func, help):
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    add("menu", cmd_menu, "menu interactif (par défaut)")
    add("disks", cmd_disks, "lister les disques")
    add("images", cmd_images, "lister les images du dépôt")

    sp = add("capture", cmd_capture, "capturer un disque dans une image")
    sp.add_argument("disk", help="disque source, ex. /dev/sda")
    sp.add_argument("name", help="nom de l'image")
    sp.add_argument("--compression", choices=["zstd", "gzip", "none"], default="zstd")
    sp.add_argument("--level", default="auto",
                    help="niveau zstd 1-19, « adapt », ou « auto » (défaut : 1 en local, adaptatif sur le réseau)")
    sp.add_argument("--description", default="")

    sp_deploy = add("deploy", cmd_deploy, "restaurer une image sur un disque")
    sp_deploy.add_argument("name", help="nom ou chemin de l'image")
    sp_deploy.add_argument("disk", help="disque cible, ex. /dev/sda")

    sp2 = add("clone", cmd_clone, "copier un disque directement sur un autre")
    sp2.add_argument("source")
    sp2.add_argument("target")

    sp3 = add("verify", cmd_verify, "vérifier les sommes SHA-256 d'une image")
    sp3.add_argument("name")

    sp4 = add("mcast-send", cmd_mcast_send, "diffuser une image en multicast")
    sp4.add_argument("name")
    sp4.add_argument("--receivers", "-n", type=int, required=True, help="nombre de PC récepteurs attendus")
    sp4.add_argument("--max-wait", type=int, default=600,
                     help="démarrer après N secondes même s'il manque des récepteurs (défaut : 600)")
    sp4.add_argument("--max-bitrate", help="débit maximal, ex. 500m")

    sp5 = add("mcast-recv", cmd_mcast_recv, "recevoir une image multicast et la restaurer")
    sp5.add_argument("disk")

    for s in (sp4, sp5):
        s.add_argument("--interface", help="interface réseau (défaut : première active)")
        s.add_argument("--portbase", type=int, default=mc.DEFAULT_PORTBASE)
    for s in (sp_deploy, sp2, sp5):
        s.add_argument("--yes", action="store_true", help="ne pas demander de confirmation")
        s.add_argument("--new-uuids", action="store_true",
                       help="nouveaux identifiants de disque/partitions (déconseillé pour Windows, voir README)")

    sp6 = add("mount", cmd_mount, "monter le dépôt d'images")
    sp6.add_argument("kind", choices=["clonix", "local", "smb", "nfs", "sshfs"])
    sp6.add_argument("target", nargs="?",
                     help="/dev/sdb1, //serveur/partage, serveur:/export ou user@hote:/chemin ; inutile pour « clonix »")
    sp6.add_argument("--user", help="utilisateur SMB")
    sp6.add_argument("--domain", help="domaine SMB")

    add("umount", cmd_umount, "démonter le dépôt d'images")

    sp8 = add("password", cmd_password, "réinitialiser le mot de passe administrateur d'un poste déployé")
    sp8.add_argument("disk", help="disque du poste, ex. /dev/sda")
    sp8.add_argument("--user", help="compte à réinitialiser (ex. Administrator, root)")
    sp8.add_argument("--list", action="store_true", help="lister les comptes du poste")
    sp8.add_argument("--set", action="store_true",
                     help="définir un nouveau mot de passe (Linux) au lieu de l'effacer")
    sp8.add_argument("--yes", action="store_true", help="ne pas demander de confirmation")

    sp7 = add("web", cmd_web, "interface graphique (serveur web local)")
    sp7.add_argument("--port", type=int, default=8470)
    sp7.add_argument("--host", help="adresse d'écoute (défaut : 127.0.0.1)")
    sp7.add_argument("--remote", action="store_true",
                     help="accessible depuis le réseau (protégé par un code d'accès)")
    sp7.add_argument("--demo", action="store_true", help="disques simulés, aucune écriture")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_menu
    runner = Runner(dry_run=args.dry_run, verbose=args.verbose)
    if (hasattr(os, "geteuid") and os.geteuid() != 0 and not args.dry_run and args.func not in (cmd_images, cmd_verify)
            and not getattr(args, "demo", False)):
        print("clonix doit être lancé en root (sudo).", file=sys.stderr)
        return 2
    try:
        return args.func(runner, args)
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130
    except ClonixError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
