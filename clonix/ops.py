"""Opérations principales : capture, déploiement, clonage disque à disque."""

import io
import json
import os
import re
import shutil
import tempfile
import time

from clonix import disks as dk
from clonix import fsclone as fc
from clonix import image as im
from clonix import partitions as pt
from clonix.errors import ClonixError, CommandError


def _types_by_number(table_text):
    """Type de chaque partition, lu dans le dump sfdisk (pour repérer les étendues)."""
    types = {}
    for raw in table_text.splitlines():
        line = raw.strip()
        if line.startswith("/") and ":" in line:
            dev, fields = line.split(":", 1)
            types[dk.part_number(dev.strip())] = pt._fields(fields).get("type", "")
    return types


def plan_partitions(runner, disk, table_text):
    table = pt.parse_dump(table_text)
    types = _types_by_number(table_text)
    by_number = {p.number: p for p in disk.partitions}
    plan = []
    for entry in table.entries:
        part = by_number.get(entry.number) or dk.Partition(
            path=dk.part_path(disk.path, entry.number), number=entry.number, size=entry.size * table.sector_size
        )
        mode, tool = fc.plan_mode(part, str(types.get(entry.number, "")), runner.which)
        plan.append((entry, part, mode, tool))
    return table, plan


# --------------------------------------------------------------------- capture


MODE_LABELS = {"partclone": "blocs utilisés", "dd": "copie intégrale", "wim": "fichier par fichier"}
DATA_MODES = ("partclone", "dd", "wim")


def _step_label(number, fstype, mode):
    return f"Partition {number} ({fstype or 'brute'}) · {MODE_LABELS.get(mode, mode)}"


def _data_steps(plan, sector_size):
    return [(_step_label(e.number, p.fstype, mode), e.size * sector_size)
            for e, p, mode, _ in plan if mode in DATA_MODES]


WINDOWS_SCRATCH = ("pagefile.sys", "swapfile.sys")


def slim_windows(runner, disk):
    """Supprime les fichiers jetables de Windows avant la capture.

    - hiberfil.sys (veille prolongée / démarrage rapide) : supprimé par ntfs-3g
      grâce à l'option remove_hiberfile. Le déployer sur un autre PC est aussi
      une source de problèmes au premier démarrage.
    - pagefile.sys, swapfile.sys : fichiers d'échange, recréés par Windows.
    Ces fichiers pèsent souvent 5 à 20 Go et se compressent mal ; AOMEI et
    Acronis les excluent aussi. En cas d'échec sur une partition (volume marqué
    à vérifier, chiffré…), on continue sans l'alléger.
    """
    for part in disk.partitions:
        if part.fstype != "ntfs":
            continue
        mp = tempfile.mkdtemp(prefix="clonix-slim-")
        try:
            try:
                runner.run(["mount", "-t", "ntfs-3g", "-o", "remove_hiberfile", part.path, mp])
            except ClonixError as exc:
                runner.log(f"  {part.path} : allègement impossible ({exc}), copie complète")
                continue
            try:
                runner.run(["rm", "-f"] + [os.path.join(mp, f) for f in WINDOWS_SCRATCH])
                runner.log(f"  {part.path} : fichiers d'échange et de veille prolongée retirés")
            finally:
                runner.run(["umount", mp], check=False)
        finally:
            try:
                os.rmdir(mp)
            except OSError:
                pass


def windows_not_shut_down(runner, part):
    """Windows a-t-il laissé la partition en démarrage rapide / veille prolongée ?

    ntfs-3g.probe --readwrite renvoie 14 (veille prolongée) ou 15 (volume mal
    démonté) dans ces cas.
    """
    if part.fstype != "ntfs" or not runner.which("ntfs-3g.probe"):
        return False
    try:
        runner.run(["ntfs-3g.probe", "--readwrite", part.path], readonly=True)
    except CommandError as exc:
        return exc.returncode in (14, 15)
    return False


GIB = 1024 ** 3
WIM_MARGIN = 4 * GIB  # place libre laissée à Windows en plus de ses fichiers
_NTFS_USED = re.compile(r"Space in use\s*:\s*(\d+)\s*MB")


def ntfs_used_bytes(runner, part_path):
    """Espace occupé d'un volume NTFS (lecture seule, sans montage), ou None."""
    if not runner.which("ntfsresize"):
        return None
    try:
        out = runner.run(["ntfsresize", "--info", "--force", "--no-progress-bar", part_path],
                         readonly=True, check=False)
    except ClonixError:
        return None
    m = _NTFS_USED.search(out or "")
    return int(m.group(1)) * 1000 * 1000 if m else None


def wim_min_size(used, part_size):
    """Taille minimale de la partition Windows recréée : données + 10 % + 4 Gio."""
    if not used:
        return part_size
    need = int(used * 1.1) + WIM_MARGIN
    need = -(-need // pt.ALIGN_BYTES) * pt.ALIGN_BYTES
    return min(need, part_size)


def pick_resizable(plan, sector_size):
    """La partition qui s'adapte à la taille du disque : la plus grande NTFS (C:)."""
    best = None
    for entry, part, mode, tool in plan:
        if part.fstype in fc.WIM_FSTYPES and mode == "partclone":
            if best is None or entry.size > best[0].size:
                best = (entry, part, mode, tool)
    return best


def capture(runner, disk_path, name, image_dir=None, compression="zstd", level="auto", description="",
            slim=False, resizable=False):
    disk = dk.get_disk(runner, disk_path)
    dk.check_readable_source(disk)
    dest = im.resolve(name, image_dir)
    if os.path.exists(dest):
        raise ClonixError(f"L'image {dest} existe déjà")
    parent = os.path.dirname(dest)
    if os.path.isdir(parent) and dk.disk_of_mount(runner, parent) == disk.path:
        raise ClonixError("Le dépôt d'images est sur le disque à capturer")
    store_disk = dk.disk_of_mount(runner, parent) if os.path.isdir(parent) else None
    level = fc.choose_level(level, dk.fstype_of_mount(runner, parent) if os.path.isdir(parent) else "",
                            on_usb=bool(store_disk and dk.usb_speed(store_disk) is not None))
    comp = fc.compress_cmd(compression, level)
    if comp and not runner.which(comp[0]):
        raise ClonixError(f"Compresseur introuvable : {compression}")

    if slim:
        runner.log("Allègement : retrait des fichiers jetables de Windows…")
        slim_windows(runner, disk)
    else:
        for part in disk.partitions:
            if windows_not_shut_down(runner, part):
                runner.warn(
                    f"Windows n'était pas complètement arrêté sur {part.path} (démarrage rapide "
                    "ou veille prolongée). Clonix corrigera la copie au déploiement, mais pour une "
                    "image propre : dans Windows, tapez « powercfg /h off » dans une invite de "
                    "commandes en administrateur, éteignez le PC, puis recapturez.")
    table_text = pt.dump_table(runner, disk.path)
    table, plan = plan_partitions(runner, disk, table_text)
    wim_number = None
    if resizable:
        chosen = pick_resizable(plan, table.sector_size)
        if table.label != "gpt":
            runner.warn("Image adaptable impossible : le disque n'est pas en GPT (démarrage UEFI). "
                        "Image classique, à déployer sur un disque au moins aussi grand.")
        elif chosen is None:
            runner.warn("Image adaptable impossible : aucune partition Windows (NTFS) lisible. "
                        "Image classique, à déployer sur un disque au moins aussi grand.")
        elif not runner.which(fc.WIMLIB):
            raise ClonixError("Outil introuvable : wimlib-imagex (paquet wimtools)")
        else:
            wim_number = chosen[0].number
            plan = [(e, p, "wim", fc.WIMLIB) if e.number == wim_number else (e, p, m, t)
                    for e, p, m, t in plan]
    manifest = im.new_manifest(os.path.basename(dest), disk, table, description)
    runner.plan(_data_steps(plan, table.sector_size))
    step = 0

    runner.log(f"Capture de {disk.describe()} vers {dest}")
    dk.check_usb_link(runner, image_dir)
    started = time.monotonic()
    tmp = dest + ".partial"
    if not runner.dry_run:
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp)
        # Si le dépôt est une clé Ventoy, ce fichier empêche Ventoy d'afficher le
        # « boot.img » de l'image dans son menu de démarrage.
        open(os.path.join(tmp, ".ventoyignore"), "w").close()
    try:
        _write_small(runner, tmp, manifest, im.TABLE, table_text.encode())
        _write_small(runner, tmp, manifest, im.BOOT, pt.save_boot_area(runner, disk.path, table))

        ext = fc.extension(compression)
        for entry, part, mode, tool in plan:
            rec = {
                "number": entry.number,
                "fstype": part.fstype,
                "label": part.label,
                "uuid": part.uuid,
                "size": entry.size * table.sector_size,
                "mode": mode,
                "tool": tool,
                "compression": None,
                "file": None,
            }
            if mode == "wim":
                rec["file"] = f"p{entry.number}.wim{ext}"
                rec["compression"] = compression
                runner.log(f"→ partition {entry.number} ({part.fstype}) : fichier par fichier (WIM)")
                used = ntfs_used_bytes(runner, part.path)
                config = os.path.join(tempfile.gettempdir(), "clonix-wim-exclude.txt")
                if not runner.dry_run:
                    with open(config, "w", encoding="utf-8") as f:
                        f.write(fc.WIM_EXCLUDE)
                cmds = [fc.wim_capture_cmd(part.path, config)]
                if comp:
                    cmds.append(comp)
                runner.start_step(step)
                step += 1
                runner.last_wim_total = None
                _write_stream(runner, tmp, manifest, rec["file"], cmds, rec["size"])
                rec["min_size"] = wim_min_size(runner.last_wim_total or used, rec["size"])
                manifest["table"]["min_bytes"] = (table.required_bytes() - rec["size"]
                                                  + rec["min_size"] + 4 * pt.ALIGN_BYTES)
                runner.log(f"  partition {entry.number} : au moins {dk.human_size(rec['min_size'])} "
                           "nécessaires au déploiement")
            elif mode in ("partclone", "dd"):
                rec["file"] = f"p{entry.number}.{mode}{ext}"
                rec["compression"] = compression
                runner.log(f"→ partition {entry.number} ({part.fstype or 'brute'}) : {mode}")
                flags = fc.partclone_flags(runner.supports, tool, capture=True) if tool else ()
                cmds = [fc.read_cmd(mode, tool, part.path, flags)]
                if comp:
                    cmds.append(comp)
                runner.start_step(step)
                step += 1
                _write_stream(runner, tmp, manifest, rec["file"], cmds, rec["size"])
            else:
                runner.log(f"→ partition {entry.number} ({part.fstype or 'étendue'}) : {mode}")
            manifest["partitions"].append(rec)

        manifest["finished"] = im.now_iso()
        if not runner.dry_run:
            with open(os.path.join(tmp, im.MANIFEST), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
            os.rename(tmp, dest)
    except BaseException:
        if not runner.dry_run and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    runner.log(f"Image prête : {dest}")
    _summary(runner, "Capture", started, sum(f.get("size", 0) for f in manifest["files"].values()), "écrits")
    return dest, manifest


def _summary(runner, what, started, nbytes, verb, visible=True):
    """Bilan de fin : durée, volume de l'image et débit moyen."""
    if runner.dry_run:
        return
    elapsed = max(time.monotonic() - started, 0.001)
    minutes, seconds = divmod(int(elapsed), 60)
    rate = nbytes / elapsed / 1e6
    text = (f"{what} : {minutes} min {seconds:02d} s, {dk.human_size(nbytes)} {verb} "
            f"(moyenne {rate:.0f} Mo/s)")
    (runner.note if visible else runner.log)(text)


def _write_small(runner, folder, manifest, name, data):
    if runner.dry_run:
        runner.log(f"[dry-run] écrire {name} ({len(data)} octets)")
        return
    with open(os.path.join(folder, name), "wb") as f:
        w = im.HashingWriter(f)
        w.write(data)
    manifest["files"][name] = w.record()


def _write_stream(runner, folder, manifest, name, cmds, total):
    if runner.dry_run:
        runner.pipeline(cmds, sink=io.BytesIO(), total=total)
        return
    with open(os.path.join(folder, name), "wb") as f:
        w = im.HashingWriter(f)
        runner.pipeline(cmds, sink=w, total=total)
        f.flush()
        os.fsync(f.fileno())
    manifest["files"][name] = w.record()


# ------------------------------------------------------------------- déploiement


SMALLER_HINT = ("Pour la mettre sur un disque plus petit, recapturez le PC d'origine en cochant "
                "« Image adaptable aux disques plus petits ».")
EXPAND_MIN_GAIN = GIB  # en dessous, agrandir la partition ne vaut pas le temps passé


def plan_layout(manifest, table_text, disk, expand=True):
    """Adapte la table de l'image à la taille du disque cible.

    Retourne ``(dump à écrire, {numéro: nouvelle taille en octets})``. Seule la
    partition principale (la plus grande NTFS ou ext) change de taille :
    - disque plus grand : elle est agrandie pour utiliser tout le disque ;
    - disque plus petit : seulement si elle a été capturée fichier par fichier.
    """
    table = pt.parse_dump(table_text)
    required = table.required_bytes()
    recs = [r for r in manifest["partitions"]
            if r.get("fstype") in fc.GROWABLE and r.get("mode") in ("partclone", "wim")]
    rec = max(recs, key=lambda r: (r["mode"] == "wim", r["size"]), default=None)
    entry = next((e for e in table.entries if rec and e.number == rec["number"]), None)
    movable = entry is not None and (
        table.label == "gpt" or all(e.start < entry.start for e in table.entries if e is not entry))
    if required > disk.size:
        if not movable or rec["mode"] != "wim":
            raise ClonixError(
                f"{disk.path} ({dk.human_size(disk.size)}) est trop petit pour l'image "
                f"« {manifest['name']} » ({dk.human_size(required)} requis). {SMALLER_HINT}")
        text, size = pt.relayout(table_text, disk.size, rec["number"], rec.get("min_size", rec["size"]))
        return text, {rec["number"]: size}
    if expand and movable and disk.size - required >= EXPAND_MIN_GAIN:
        text, size = pt.relayout(table_text, disk.size, rec["number"], rec["size"])
        return text, {rec["number"]: size}
    return table_text, {}


def grow_filesystem(runner, fstype, device):
    """Agrandit le système de fichiers à la taille de sa partition (agrandie)."""
    for argv, stdin, check in fc.grow_cmds(fstype, device):
        if not check:
            try:
                runner.run(argv, input=stdin)
            except CommandError as exc:
                if exc.returncode > 2:  # e2fsck : 1 et 2 = erreurs corrigées
                    raise
            continue
        runner.run(argv, input=stdin)


def deploy(runner, source, disk_path, image_dir=None, new_uuids=False, confirm=None, adapt_windows=False,
           expand=True):
    """Restaure une image (``DirSource`` ou ``TarStreamSource``) sur ``disk_path``.

    ``confirm(manifest, disk)`` est appelé après lecture du manifeste et avant
    toute écriture ; il doit lever une exception pour annuler.
    """
    disk = dk.get_disk(runner, disk_path)
    dk.check_writable_target(runner, disk, image_dir)

    with source.open(im.MANIFEST) as f:
        manifest = im.load_manifest(f)
    files = manifest["files"]
    with source.open(im.TABLE) as f:
        reader = im.VerifyingReader(f, im.TABLE, files.get(im.TABLE))
        table_text = reader.read().decode()
        reader.read()  # déclenche la vérification
    layout_text, resized = plan_layout(manifest, table_text, disk, expand=expand)
    table = pt.parse_dump(table_text)
    with source.open(im.BOOT) as f:
        reader = im.VerifyingReader(f, im.BOOT, files.get(im.BOOT))
        boot = reader.read()
        reader.read()  # déclenche la vérification

    needed = {r["tool"] for r in manifest["partitions"] if r["mode"] == "partclone"}
    if any(r["mode"] == "wim" for r in manifest["partitions"]):
        needed |= {fc.WIMLIB, "mkntfs"}
    for number in resized:
        rec = next(r for r in manifest["partitions"] if r["number"] == number)
        grow_tools = {argv[0] for argv, _, _ in fc.grow_cmds(rec["fstype"], "")}
        if rec["mode"] == "partclone" and not all(runner.which(t) for t in grow_tools):
            runner.log("Outils d'agrandissement absents : la taille des partitions de l'image est gardée")
            layout_text, resized = table_text, {}
            break
    missing = sorted(t for t in needed if not runner.which(t))
    if missing:
        raise ClonixError(f"Outils introuvables : {', '.join(missing)}")

    if confirm:
        confirm(manifest, disk)

    runner.log(f"Déploiement de « {manifest['name']} » sur {disk.describe()}")
    dk.check_usb_link(runner, image_dir)
    dk.tune_readahead(runner, image_dir)
    started = time.monotonic()
    for number, size in resized.items():
        old = next(r["size"] for r in manifest["partitions"] if r["number"] == number)
        verb = "agrandie" if size > old else "réduite"
        runner.note(f"Partition {number} {verb} : {dk.human_size(old)} → {dk.human_size(size)} "
                    f"pour s'adapter au disque de {dk.human_size(disk.size)}")
    pt.write_table(runner, disk, layout_text, new_uuids=new_uuids)
    pt.restore_boot_area(runner, disk.path, table, boot)

    runner.plan([(_step_label(r["number"], r["fstype"], r["mode"]), r["size"])
                 for r in manifest["partitions"] if r.get("file")])
    step = 0
    for rec in manifest["partitions"]:
        target = dk.part_path(disk.path, rec["number"])
        mode = rec["mode"]
        runner.log(f"→ partition {rec['number']} ({rec['fstype'] or mode}) : {mode}")
        if mode == "swap":
            runner.run(fc.mkswap_cmd(target, rec.get("uuid"), rec.get("label")))
            continue
        if mode == "skip":
            continue
        cmds = []
        decomp = fc.decompress_cmd(rec["compression"])
        if decomp:
            cmds.append(decomp)
        if mode == "wim":
            runner.run(fc.mkntfs_cmd(target, rec.get("label")))
            cmds.append(fc.wim_apply_cmd(target))
        else:
            flags = fc.partclone_flags(runner.supports, rec["tool"]) if mode == "partclone" else ()
            cmds.append(fc.write_cmd(mode, rec["tool"], target, flags))
        runner.start_step(step)
        step += 1
        part_started = time.monotonic()
        with source.open(rec["file"]) as f:
            runner.pipeline(cmds, feed=im.VerifyingReader(f, rec["file"], files.get(rec["file"])),
                            total=rec["size"])
        _summary(runner, f"  partition {rec['number']}", part_started,
                 files.get(rec["file"], {}).get("size", 0), "lus", visible=False)
        if mode == "partclone" and resized.get(rec["number"], 0) > rec["size"]:
            runner.log(f"  agrandissement du système de fichiers de la partition {rec['number']}…")
            try:
                grow_filesystem(runner, rec["fstype"], target)
            except ClonixError as exc:
                runner.warn(f"La partition {rec['number']} n'a pas pu être agrandie ({exc}). Le PC "
                            "démarrera normalement mais l'espace en plus restera inutilisé.")

    runner.run(["sync"], check=False)
    if adapt_windows and any(r.get("fstype") == "ntfs" for r in manifest["partitions"]):
        from clonix import winfix
        try:
            winfix.adapt(runner, disk.path)
        except ClonixError as exc:
            runner.warn(f"Adaptation de Windows impossible : {exc}. Windows risque de ne pas "
                        "démarrer (écran bleu INACCESSIBLE_BOOT_DEVICE).")
    runner.log(f"Déploiement terminé sur {disk.path}")
    _summary(runner, "Déploiement", started, im.image_bytes(manifest), "lus")
    return manifest


def open_image(name, image_dir=None):
    return im.DirSource(im.resolve(name, image_dir))


# ------------------------------------------------------------ disque à disque


def clone(runner, src_path, dst_path, new_uuids=False, confirm=None):
    src = dk.get_disk(runner, src_path)
    dst = dk.get_disk(runner, dst_path)
    if os.path.realpath(src.path) == os.path.realpath(dst.path):
        raise ClonixError("Le disque source et le disque cible sont identiques")
    dk.check_readable_source(src)
    dk.check_writable_target(runner, dst)

    table_text = pt.dump_table(runner, src.path)
    table, plan = plan_partitions(runner, src, table_text)
    if table.required_bytes() > dst.size:
        raise ClonixError(
            f"{dst.path} ({dk.human_size(dst.size)}) est plus petit que l'espace partitionné "
            f"de {src.path} ({dk.human_size(table.required_bytes())})"
        )
    boot = pt.save_boot_area(runner, src.path, table)

    if confirm:
        confirm(src, dst)

    runner.log(f"Clonage {src.describe()} → {dst.describe()}")
    pt.write_table(runner, dst, table_text, new_uuids=new_uuids)
    pt.restore_boot_area(runner, dst.path, table, boot)
    runner.plan(_data_steps(plan, table.sector_size))
    step = 0
    for entry, part, mode, tool in plan:
        target = dk.part_path(dst.path, entry.number)
        runner.log(f"→ partition {entry.number} ({part.fstype or mode}) : {mode}")
        if mode == "swap":
            runner.run(fc.mkswap_cmd(target, part.uuid, part.label))
        elif mode != "skip":
            flags = fc.partclone_flags(runner.supports, tool) if tool else ()
            runner.start_step(step)
            step += 1
            runner.pipeline(fc.copy_cmds(mode, tool, part.path, target, flags),
                            total=entry.size * table.sector_size)
    runner.run(["sync"], check=False)
    runner.log(f"Clonage terminé : {dst.path}")
