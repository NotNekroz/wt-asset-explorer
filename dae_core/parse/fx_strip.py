"""
fx_strip.py - remove the in-game effect geometry (jet flames, gun fire, flares ...) from an exported model.

The game spawns these as quads / cards that only make sense with its shaders and particle system. In Blender they
are just flat orange planes sticking out of the aircraft, so they are cut from the export; effects are then
animated by hand.

What counts as an effect is decided by two regexes, matched case-insensitively:
  FX_NODE_RE  on the node / object name       (flare1, jet_flame1, gunfire2 ...)
  FX_MAT_RE   on the material name            (gunfire, jet_flame_diamonds, multi_blade_slow_propmask_fan_... ...)
A mesh is dropped when its node matches, or when every primitive / face group uses an fx material.
Add more words to FX_WORDS to cover more effects (e.g. "contrail"). Every removal is reported through `say`.

GLB: the mesh is detached from the node. The node itself stays, so animation channels that target it and the
     hierarchy below it are untouched. (The mesh data stays inside the file but nothing references it.)
OBJ: faces of matching objects / materials are removed. Vertex lines stay, so the remaining face indices are valid.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

try:
    from parse.a2d_anim import read_glb, write_glb
except ImportError:                      # running outside the app (tests)
    from a2d_anim import read_glb, write_glb

Say = Callable[[str], None]

FX_WORDS = ["flare", "jet_flame", "gunfire", "propmask"]
_alt = "|".join(re.escape(w) for w in FX_WORDS)
FX_NODE_RE = re.compile(rf"^(?:{_alt})\d*(?:[._]\d+)*$", re.I)
FX_MAT_RE = re.compile(rf"(?:{'|'.join(re.escape(w) for w in FX_WORDS if w != 'flare')})", re.I)


def is_fx_node(name) -> bool:
    return bool(name) and bool(FX_NODE_RE.match(name))


def is_fx_material(name) -> bool:
    return bool(name) and bool(FX_MAT_RE.search(name))


def strip_glb(path, say: Say = print) -> int:
    """Remove effect geometry from a .glb in place. Returns the number of nodes / primitives removed.

    A node whose NAME is an effect loses its mesh. Otherwise only the primitives that use an effect material are
    dropped (a part that mixes real geometry with an effect card keeps the real geometry)."""
    js, binb = read_glb(path)
    mats = [m.get("name") for m in js.get("materials", [])]
    meshes = js.get("meshes", [])
    removed = []
    for n in js.get("nodes", []):
        mi = n.get("mesh")
        if mi is None:
            continue
        label = n.get("name") or f"mesh{mi}"
        if is_fx_node(n.get("name")):
            del n["mesh"]; n.pop("skin", None)
            removed.append(label)
            continue
        prims = meshes[mi].get("primitives", [])
        keep = [p for p in prims if not (p.get("material") is not None and is_fx_material(mats[p["material"]]))]
        if len(keep) == len(prims):
            continue
        if not keep:
            del n["mesh"]; n.pop("skin", None)
            removed.append(label)
        else:                                          # private mesh without the effect primitives
            m2 = dict(meshes[mi]); m2["primitives"] = keep
            meshes.append(m2)
            n["mesh"] = len(meshes) - 1
            removed.append(label + " (effect part only)")
    if removed:
        write_glb(path, js, binb)
        say(f"  removed {len(removed)} effect mesh(es) from the GLB: " + ", ".join(sorted(set(removed))[:20])
            + (" ..." if len(set(removed)) > 20 else ""))
    else:
        say("  no effect meshes found in the GLB")
    return len(removed)


def strip_obj(path, say: Say = print) -> int:
    """Remove the faces of effect objects / materials from a .obj in place. Returns the number of face lines dropped."""
    p = Path(path)
    raw = p.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    out, dropped, names = [], 0, set()
    obj, mat = "", ""
    for line in raw.decode("utf-8", "replace").splitlines():
        tag = line.split(None, 1)
        key = tag[0] if tag else ""
        arg = tag[1].strip() if len(tag) > 1 else ""
        if key in ("o", "g"):
            obj = arg
        elif key == "usemtl":
            mat = arg
        elif key in ("f", "l", "p") and (is_fx_node(obj) or is_fx_material(mat)):
            dropped += 1
            names.add(obj or mat)
            continue
        out.append(line)
    if dropped:
        p.write_bytes((nl.join(out) + nl).encode("utf-8"))
        say(f"  removed {dropped} effect face(s) from the OBJ: " + ", ".join(sorted(names)[:20]))
    else:
        say("  no effect faces found in the OBJ")
    return dropped