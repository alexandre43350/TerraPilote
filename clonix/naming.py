"""Noms des PC déployés : nom fixe ou série numérotée (SALLE1-PC## → SALLE1-PC01, PC02…).

Le prochain numéro de chaque série est gardé sur le support des images, pour
continuer la série d'un PC à l'autre et d'un démarrage à l'autre.
"""

import json
import os
import re

from clonix.errors import ClonixError

STATE_FILE = ".clonix-noms.json"
_VALID = re.compile(r"^[A-Za-z0-9-]+$")
_HASHES = re.compile(r"#+")
MAX_LEN = 15  # limite des noms d'ordinateur Windows (NetBIOS)


def check_name(name):
    """Lève une erreur claire si ``name`` n'est pas un nom d'ordinateur Windows valable."""
    if not name:
        raise ClonixError("Nom du PC vide")
    if len(name) > MAX_LEN:
        raise ClonixError(f"Nom du PC trop long : « {name} » ({len(name)} caractères, {MAX_LEN} au plus)")
    if not _VALID.match(name):
        raise ClonixError(f"Nom du PC invalide : « {name} » (lettres sans accent, chiffres et tirets seulement)")
    if name.isdigit():
        raise ClonixError(f"Nom du PC invalide : « {name} » (pas uniquement des chiffres)")
    if name.startswith("-") or name.endswith("-"):
        raise ClonixError(f"Nom du PC invalide : « {name} » (pas de tiret au début ni à la fin)")
    return name


def is_series(pattern):
    return "#" in (pattern or "")


def expand(pattern, number):
    """« SALLE1-PC## », 7 → « SALLE1-PC07 » (les # donnent le nombre de chiffres)."""
    if not is_series(pattern):
        return pattern
    return _HASHES.sub(lambda m: str(number).zfill(len(m.group(0))), pattern, count=1)


def check_pattern(pattern):
    """Vérifie un nom ou une série (avec le plus long numéro possible)."""
    pattern = (pattern or "").strip()
    if is_series(pattern):
        if len(_HASHES.findall(pattern)) > 1:
            raise ClonixError("Une seule suite de # dans le nom (ex. SALLE1-PC##)")
        width = len(_HASHES.search(pattern).group(0))
        check_name(expand(pattern, 10 ** width - 1))
    else:
        check_name(pattern)
    return pattern


def _load(image_dir):
    try:
        with open(os.path.join(image_dir, STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def counters(image_dir):
    """{série: prochain numéro}."""
    return {k: int(v) for k, v in _load(image_dir).items() if isinstance(v, int)}


def next_number(image_dir, pattern):
    return counters(image_dir).get(pattern, 1)


def peek(image_dir, pattern):
    """Nom que recevra le prochain PC (sans réserver le numéro)."""
    return expand(pattern, next_number(image_dir, pattern)) if pattern else ""


def commit(image_dir, pattern, number):
    """Le PC numéro ``number`` est fait : la série reprendra au suivant."""
    if not is_series(pattern):
        return
    data = _load(image_dir)
    data[pattern] = max(int(data.get(pattern, 1)), number + 1)
    tmp = os.path.join(image_dir, STATE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, os.path.join(image_dir, STATE_FILE))


def set_next(image_dir, pattern, number):
    """Fixe le prochain numéro d'une série (pour reprendre ou recommencer)."""
    if not is_series(pattern):
        return
    data = _load(image_dir)
    data[pattern] = max(int(number), 1)
    tmp = os.path.join(image_dir, STATE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, os.path.join(image_dir, STATE_FILE))
