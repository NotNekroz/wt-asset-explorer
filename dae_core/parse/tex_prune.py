"""
tex_prune.py - drop the materials and texture files an exported model does not use.

The exporter writes the textures of EVERY material of the game model into <out>/textures, but the finished file
often shows only part of it: a loadout hides pylons and pods, effect stripping removes flames and gun fire. Their
materials stay in the file and their textures stay on disk. After the export is complete:

  prune_obj_materials   remove the newmtl blocks of the .mtl that no remaining face uses
  prune_glb_materials   remove the materials / textures / images that no mesh of a node uses (indices are remapped)
  prune_textures_dir    delete image files in <out>/textures that no .mtl and no .glb of the folder refers to

The textures folder is shared by every export of the same vehicle (OBJ and GLB, other loadouts), so a file is
deleted only when NONE of the .mtl / .glb files in the folder refers to it. Nothing is deleted when no reference
at all could be read, so a parse problem can never wipe the folder.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

try:
    from parse.a2d_anim import read_glb, write_glb
except ImportError:                      # running outside the app (tests)
    from a2d_anim import read_glb, write_glb

Say = Callable[[str], None]
IMAGE_EXTS = (".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp", ".webp", ".ktx2")
_MTL_TEX_KEYS = ("bump", "disp", "decal", "refl")


# ------------------------------------------------------------------------------------------------------ OBJ / MTL
def _mtl_tex_ref(line: str):
    tok = line.split()
    if len(tok) > 1 and (tok[0].lower().startswith("map_") or tok[0].lower() in _MTL_TEX_KEYS):
        return tok[-1]
    return None


def prune_obj_materials(path, say: Say = print) -> int:
    """Remove the materials no face of the .obj uses from its .mtl file(s). Returns the number of blocks removed."""
    p = Path(path)
    raw = p.read_bytes().decode("utf-8", "replace")
    used: set[str] = set()
    mtl_files: list[Path] = []
    cur = ""
    for ln in raw.splitlines():
        tag = ln.split(None, 1)
        key = tag[0] if tag else ""
        arg = tag[1].strip() if len(tag) > 1 else ""
        if key == "usemtl":
            cur = arg
        elif key == "f" and cur:
            used.add(cur)
        elif key == "mtllib" and arg:
            mtl_files.append(p.parent / arg)
    removed = 0
    for m in dict.fromkeys(mtl_files):
        if not m.is_file():
            continue
        data = m.read_bytes()
        text = data.decode("utf-8", "replace")
        nl = "\r\n" if b"\r\n" in data else "\n"
        out: list[str] = []
        keep = True
        for ln in text.splitlines():
            s = ln.strip()
            if s.lower().startswith("newmtl"):
                parts = s.split(None, 1)
                keep = (parts[1].strip() if len(parts) > 1 else "") in used
                if not keep:
                    removed += 1
            if keep:
                out.append(ln)
        if len(out) != len(text.splitlines()):
            m.write_bytes((nl.join(out) + nl).encode("utf-8"))
    if removed:
        say(f"  textures: removed {removed} unused material(s) from the .mtl")
    return removed


# ------------------------------------------------------------------------------------------------------ GLB
def _walk_tex_refs(obj, fn) -> None:
    """Call fn(dict) for every glTF textureInfo ({"index": n}) found under a key ending in 'Texture'."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and k.lower().endswith("texture") and isinstance(v.get("index"), int):
                fn(v)
            _walk_tex_refs(v, fn)
    elif isinstance(obj, list):
        for v in obj:
            _walk_tex_refs(v, fn)


def _tex_sources(t: dict) -> list[int]:
    out = [t["source"]] if isinstance(t.get("source"), int) else []
    for ext in (t.get("extensions") or {}).values():
        if isinstance(ext, dict) and isinstance(ext.get("source"), int):
            out.append(ext["source"])
    return out


def prune_glb_materials(path, say: Say = print) -> tuple[int, int]:
    """Drop materials no node's mesh uses, then the textures / images only those used. Returns (materials, images) removed."""
    js, binb = read_glb(path)
    mats = js.get("materials") or []
    meshes = js.get("meshes") or []
    if not mats:
        return 0, 0
    used_meshes = {n["mesh"] for n in js.get("nodes", []) if isinstance(n.get("mesh"), int)}
    used_mats = {pr["material"] for mi in used_meshes if 0 <= mi < len(meshes)
                 for pr in meshes[mi].get("primitives", []) if isinstance(pr.get("material"), int)}
    keep_m = sorted(m for m in used_mats if 0 <= m < len(mats))
    mat_map = {old: new for new, old in enumerate(keep_m)}

    new_mats = [mats[i] for i in keep_m]
    textures = js.get("textures") or []
    images = js.get("images") or []
    used_tex: set[int] = set()
    _walk_tex_refs(new_mats, lambda d: used_tex.add(d["index"]))
    keep_t = sorted(t for t in used_tex if 0 <= t < len(textures))
    tex_map = {old: new for new, old in enumerate(keep_t)}
    used_img = {s for t in keep_t for s in _tex_sources(textures[t])}
    keep_i = sorted(i for i in used_img if 0 <= i < len(images))
    img_map = {old: new for new, old in enumerate(keep_i)}

    removed_m, removed_i = len(mats) - len(keep_m), len(images) - len(keep_i)
    if not removed_m and not removed_i and len(textures) == len(keep_t):
        return 0, 0

    for me in meshes:                                  # primitives of meshes no node uses fall back to the default material
        for pr in me.get("primitives", []):
            if isinstance(pr.get("material"), int):
                if pr["material"] in mat_map:
                    pr["material"] = mat_map[pr["material"]]
                else:
                    pr.pop("material")
    _walk_tex_refs(new_mats, lambda d: d.__setitem__("index", tex_map[d["index"]]))
    new_tex = []
    for t in (textures[i] for i in keep_t):
        if isinstance(t.get("source"), int):
            t["source"] = img_map[t["source"]]
        for ext in (t.get("extensions") or {}).values():
            if isinstance(ext, dict) and isinstance(ext.get("source"), int):
                ext["source"] = img_map[ext["source"]]
        new_tex.append(t)
    js["materials"] = new_mats
    if textures:
        js["textures"] = new_tex
    if images:
        js["images"] = [images[i] for i in keep_i]
    for k in ("materials", "textures", "images"):
        if k in js and not js[k]:
            js.pop(k)
    write_glb(path, js, binb)
    if removed_m or removed_i:
        say(f"  textures: removed {removed_m} unused material(s) and {removed_i} unused image reference(s) from the .glb")
    return removed_m, removed_i


def _glb_json(path: Path) -> dict:
    """Only the JSON chunk of a .glb (the binary chunk of a finished vehicle can be tens of MB)."""
    with open(path, "rb") as f:
        head = f.read(20)
        jl = struct.unpack_from("<I", head, 12)[0]
        return json.loads(f.read(jl))


# ------------------------------------------------------------------------------------------------------ texture folder
def referenced_textures(out_dir) -> set[str]:
    """Lower-case absolute paths of every image the .mtl / .glb / .gltf files directly in out_dir refer to."""
    out_dir = Path(out_dir)
    refs: set[str] = set()

    def add(base: Path, rel: str) -> None:
        try:
            refs.add(str((base / unquote(rel.replace("\\", "/"))).resolve()).lower())
        except OSError:
            pass

    for f in out_dir.glob("*.mtl"):
        try:
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                r = _mtl_tex_ref(ln)
                if r:
                    add(f.parent, r)
        except OSError:
            pass
    for f in list(out_dir.glob("*.glb")) + list(out_dir.glob("*.gltf")):
        try:
            js = _glb_json(f) if f.suffix.lower() == ".glb" else json.loads(f.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001 - an unreadable model: keep everything, see below
            return set()
        for im in js.get("images", []):
            uri = im.get("uri")
            if uri and not uri.startswith("data:"):
                add(f.parent, uri)
    return refs


def prune_textures_dir(out_dir, say: Say = print) -> int:
    """Delete the images in <out_dir>/textures that no model file of the folder refers to. Returns the bytes freed."""
    out_dir = Path(out_dir)
    tex_dir = out_dir / "textures"
    if not tex_dir.is_dir():
        return 0
    refs = referenced_textures(out_dir)
    if not refs:
        say("  textures: no texture references found in the model files - nothing deleted")
        return 0
    freed = n = 0
    for f in tex_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS and str(f.resolve()).lower() not in refs:
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
                n += 1
            except OSError:
                pass
    for d in sorted((x for x in tex_dir.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    say(f"  textures: deleted {n} unused texture file(s), {freed / 1048576:.1f} MB freed")
    return freed
