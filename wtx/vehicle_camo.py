"""vehicle_camo.py - which camo texture a vehicle really wears.

The materials of a tank list a placeholder camo (the Wolfpack's say us_camo_olive). The game swaps it with the
`default_skin` block of the vehicle's own BLK (gamedata/units/tankmodels/<id>.blk):

    default_skin {
        replace_tex { from:t = "us_camo_olive*"   to:t = "us_camo_modern_green*" }
        set_tex { from:t = "..._body_c*"  to:t = "..._skin_..._body_c*"  param:t = "camo_skin_tex" }   // fixed skin picture
    }

Some vehicles write the same thing as a `skin` block whose name is "default". default_skin wins when both exist.
Only camo textures are swapped here (a replace_tex whose `from` has "camo" in its name): that is what decides the colour
of the tank and its nets, and it leaves every other texture name alone.
The `set_tex ... camo_skin_tex` entries (default_fixed_skin_swaps) name a fixed skin picture for the body / turret / gun. Its
alpha decides where it shows (CDK wiki, creation_camo: white alpha covers the scalable camo, black lets it through), so
material.py lays it over the camo-painted diffuse by that alpha; it does not replace the diffuse.
"""
from __future__ import annotations

import re
from pathlib import Path

_KV = r'{key}\s*:\s*t\s*=\s*"([^"]*)"'


def _blocks(lines: list[str], key: str):
    """Yield the lines of every block that opens with `<key> {` (brace matched)."""
    i = 0
    while i < len(lines):
        if re.match(rf"\s*{key}\s*(\{{|$)", lines[i]):
            depth, j, started = 0, i, False
            while j < len(lines):
                depth += lines[j].count("{") - lines[j].count("}")
                started = started or "{" in lines[j]
                if started and depth <= 0:
                    break
                j += 1
            yield lines[i:j + 1]
            i = j
        i += 1


def _swaps(block: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for body in re.findall(r"replace_tex\s*\{([^{}]*)\}", "\n".join(block)):
        src = re.search(_KV.format(key="from"), body)
        dst = re.search(_KV.format(key="to"), body)
        if src and dst and "camo" in src.group(1).lower():
            out[src.group(1).split("*")[0].lower()] = dst.group(1)
    return out


def blk_net_lines(blk_path, limit: int = 60) -> list[str]:
    """Diagnostics: the lines of the vehicle's BLK that mention a cloth or a net (and the camo swaps), as 'line N: text'.
    The cloth under a net is not painted with the same camo as the net in the game; this shows where the BLK says so."""
    if not blk_path:
        return []
    try:
        lines = Path(blk_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    pat = re.compile(r'cloth|_net\b|_net_|\bnet_|camo_skin|replace_tex|set_tex', re.I)
    hits = [f"line {i + 1}: {ln.strip()}" for i, ln in enumerate(lines) if pat.search(ln)]
    if len(hits) > limit:
        hits = hits[:limit] + [f"... {len(hits) - limit} more matching lines"]
    return hits


def _set_tex_swaps(block: list[str]) -> dict[str, str]:
    """{clean lower-case `from`: `to` as written} of the `set_tex { ... param:t = "camo_skin_tex" }` entries of a skin block."""
    out: dict[str, str] = {}
    for body in re.findall(r"set_tex\s*\{([^{}]*)\}", "\n".join(block)):
        src = re.search(_KV.format(key="from"), body)
        dst = re.search(_KV.format(key="to"), body)
        if src and dst and re.search(r'param\s*:\s*t\s*=\s*"camo_skin_tex"', body):
            out[src.group(1).split("*")[0].lower()] = dst.group(1)
    return out


def default_camo_swaps(blk_path) -> dict[str, str]:
    """{clean lower-case texture name: replacement as written in the BLK} for the vehicle's default camo; {} when it has none."""
    if not blk_path:
        return {}
    try:
        lines = Path(blk_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    for block in _blocks(lines, "default_skin"):
        found = _swaps(block)
        if found:
            return found
    for block in _blocks(lines, "skin"):
        if any(re.search(r'name\s*:\s*t\s*=\s*"default"', ln) for ln in block[:6]):
            found = _swaps(block)
            if found:
                return found
    return {}


def camouflage_net_nodes(blk_path) -> set[str]:
    """Names of the model nodes the vehicle's BLK lists in its `camouflage_net { node:t = "..." }` block (lower case).
    These are the net meshes; every other mesh is an ordinary part, even when it shares the net's texture atlas."""
    if not blk_path:
        return set()
    try:
        lines = Path(blk_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return set()
    out: set[str] = set()
    for block in _blocks(lines, "camouflage_net"):
        for ln in block:
            m = re.search(r'node\s*:\s*t\s*=\s*"([^"]*)"', ln)
            if m:
                out.add(m.group(1).lower())
    return out


def default_fixed_skin_swaps(blk_path) -> dict[str, str]:
    """{clean lower-case diffuse name: fixed skin picture as written in the BLK} from the default skin's
    `set_tex { from to param:t = "camo_skin_tex" }` entries (Type 90: type_90_body_c* -> type_90_skin_ll_body_c*); {} when none."""
    if not blk_path:
        return {}
    try:
        lines = Path(blk_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    for block in _blocks(lines, "default_skin"):
        found = _set_tex_swaps(block)
        if found:
            return found
    for block in _blocks(lines, "skin"):
        if any(re.search(r'name\s*:\s*t\s*=\s*"default"', ln) for ln in block[:6]):
            found = _set_tex_swaps(block)
            if found:
                return found
    return {}
