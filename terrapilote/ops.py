"""Opérations principales : capture, déploiement, clonage disque à disque."""

import io
import json
import os
import shutil

from terrapilote import disks as dk
from terrapilote import fsclone as fc
from terrapilote import image as im
from terrapilote import partitions as pt
from terrapilote.errors import TerraPiloteError


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


def _data_steps(plan, sector_size):
    return [(f"Partition {e.number} ({p.fstype or 'brute'})", e.size * sector_size)
            for e, p, mode, _ in plan if mode in ("partclone", "dd")]


def capture(runner, disk_path, name, image_dir=None, compression="zstd", level="auto", description=""):
    disk = dk.get_disk(runner, disk_path)
    dk.check_readable_source(disk)
    dest = im.resolve(name, image_dir)
    if os.path.exists(dest):
        raise TerraPiloteError(f"L'image {dest} existe déjà")
    parent = os.path.dirname(dest)
    if os.path.isdir(parent) and dk.disk_of_mount(runner, parent) == disk.path:
        raise TerraPiloteError("Le dépôt d'images est sur le disque à capturer")
    level = fc.choose_level(level, dk.fstype_of_mount(runner, parent) if os.path.isdir(parent) else "")
    comp = fc.compress_cmd(compression, level)
    if comp and not runner.which(comp[0]):
        raise TerraPiloteError(f"Compresseur introuvable : {compression}")

    table_text = pt.dump_table(runner, disk.path)
    table, plan = plan_partitions(runner, disk, table_text)
    manifest = im.new_manifest(os.path.basename(dest), disk, table, description)
    runner.plan(_data_steps(plan, table.sector_size))
    step = 0

    runner.log(f"Capture de {disk.describe()} vers {dest}")
    tmp = dest + ".partial"
    if not runner.dry_run:
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp)
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
            if mode in ("partclone", "dd"):
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
    return dest, manifest


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


def deploy(runner, source, disk_path, image_dir=None, new_uuids=False, confirm=None):
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
    table = pt.parse_dump(table_text)
    if table.required_bytes() > disk.size:
        raise TerraPiloteError(
            f"{disk.path} ({dk.human_size(disk.size)}) est trop petit pour l'image "
            f"« {manifest['name']} » ({dk.human_size(table.required_bytes())} requis)"
        )
    with source.open(im.BOOT) as f:
        reader = im.VerifyingReader(f, im.BOOT, files.get(im.BOOT))
        boot = reader.read()
        reader.read()  # déclenche la vérification

    missing = sorted({r["tool"] for r in manifest["partitions"] if r["mode"] == "partclone"
                      and not runner.which(r["tool"])})
    if missing:
        raise TerraPiloteError(f"Outils introuvables : {', '.join(missing)}")

    if confirm:
        confirm(manifest, disk)

    runner.log(f"Déploiement de « {manifest['name']} » sur {disk.describe()}")
    pt.write_table(runner, disk, table_text, new_uuids=new_uuids)
    pt.restore_boot_area(runner, disk.path, table, boot)

    runner.plan([(f"Partition {r['number']} ({r['fstype'] or 'brute'})", r["size"])
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
        flags = fc.partclone_flags(runner.supports, rec["tool"]) if mode == "partclone" else ()
        cmds.append(fc.write_cmd(mode, rec["tool"], target, flags))
        runner.start_step(step)
        step += 1
        with source.open(rec["file"]) as f:
            runner.pipeline(cmds, feed=im.VerifyingReader(f, rec["file"], files.get(rec["file"])),
                            total=rec["size"])

    runner.run(["sync"], check=False)
    runner.log(f"Déploiement terminé sur {disk.path}")
    return manifest


def open_image(name, image_dir=None):
    return im.DirSource(im.resolve(name, image_dir))


# ------------------------------------------------------------ disque à disque


def clone(runner, src_path, dst_path, new_uuids=False, confirm=None):
    src = dk.get_disk(runner, src_path)
    dst = dk.get_disk(runner, dst_path)
    if os.path.realpath(src.path) == os.path.realpath(dst.path):
        raise TerraPiloteError("Le disque source et le disque cible sont identiques")
    dk.check_readable_source(src)
    dk.check_writable_target(runner, dst)

    table_text = pt.dump_table(runner, src.path)
    table, plan = plan_partitions(runner, src, table_text)
    if table.required_bytes() > dst.size:
        raise TerraPiloteError(
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
