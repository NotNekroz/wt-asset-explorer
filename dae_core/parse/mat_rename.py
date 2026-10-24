"""
mat_rename.py - name every material after its diffuse texture, so a material in Blender / Substance Painter can be
matched to its texture file at a glance.

The exporter names a material from its texture slots and shader class ("f_16c_block_52_aesa_aircraft_normal_detail_lo");
this renames it to the stem of the picture it shows ("f_16c_block_52_aesa_c", or "<diffuse>__<camo>" for a camo-baked
one). Runs as the LAST step of an export, after the effect strip, loadout filter, weapon mounts and texture prune,
which still look materials up by their original names.

OBJ: the base colour is the .mtl block's map_Kd. 'newmtl' lines in the .mtl(s) and 'usemtl' lines in the .obj change.
GLB: the base colour is pbrMetallicRoughness.baseColorTexture -> texture -> image uri. Only material 'name' changes
     (primitives refer to materials by index).

Materials without a diffuse texture (glass, untextured parts) keep their name. Two materials that show the same
texture get 'name', 'name_2', 'name_3' ... so every name stays unique. Every rename is reported through `say`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

try:
    from parse.a2d_anim import read_glb, write_glb
except ImportError:                      # running outside the app (tests)
    from a2d_anim import read_glb, write_glb

Say = Callable[[str], None]


def _stem(ref: str) -> str:
    """'textures/f_16c_c.dds' / 'textures\\f_16c_c.png' / 'textures/a%20b.png' -> 'f_16c_c' (no folder, no extension)."""
    name = unquote(ref.strip().strip('"')).replace("\\", "/").split("/")[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return re.sub(r"\s+", "_", stem.strip())


def _unique(base: str, taken: set) -> str:
    """base, or base_2, base_3 ... whichever is not in `taken`; the result is added to `taken`."""
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def _report(say: Say, kind: str, pairs: list, renumbered: int) -> None:
    shown = ", ".join(f"{a} -> {b}" for a, b in pairs[:12]) + (" ..." if len(pairs) > 12 else "")
    extra = f" ({renumbered} numbered because several materials share one texture)" if renumbered else ""
    say(f"  named {len(pairs)} material(s) after their texture in the {kind}{extra}: {shown}")


# ------------------------------------------------------------------------------------------------------ OBJ / MTL
def _mtl_diffuse(line: str):
    """Texture file of a 'map_Kd [options] file' line (the last token), else None."""
    tok = line.split()
    if len(tok) > 1 and tok[0].lower() == "map_kd":
        return tok[-1]
    return None


def rename_obj_materials(path, say: Say = print) -> int:
    """Rename the materials of a .obj (and its .mtl file(s)) after their diffuse texture. Returns how many changed."""
    p = Path(path)
    obj_raw = p.read_bytes()
    obj_lines = obj_raw.decode("utf-8", "replace").splitlines(keepends=True)

    mtl_files: list[Path] = []
    for ln in obj_lines:
        tag = ln.split(None, 1)
        if tag and tag[0] == "mtllib" and len(tag) > 1:
            mtl_files.append(p.parent / tag[1].strip())
    mtl_files = [m for m in dict.fromkeys(mtl_files) if m.is_file()]
    if not mtl_files:
        say("  material names: no .mtl found - names unchanged")
        return 0

    # 1. read every material: name -> diffuse stem (None = no diffuse texture)
    blocks: dict[Path, list[str]] = {}
    diffuse: dict[str, str | None] = {}
    dup: set[str] = set()
    for m in mtl_files:
        lines = m.read_bytes().decode("utf-8", "replace").splitlines(keepends=True)
        blocks[m] = lines
        cur = None
        for ln in lines:
            s = ln.strip()
            if s.lower().startswith("newmtl"):
                parts = s.split(None, 1)
                cur = parts[1].strip() if len(parts) > 1 else ""
                if cur in diffuse:
                    dup.add(cur)                    # two blocks with one name: usemtl cannot tell them apart
                diffuse.setdefault(cur, None)
            elif cur is not None and diffuse[cur] is None:
                ref = _mtl_diffuse(s)
                if ref:
                    diffuse[cur] = _stem(ref)

    # 2. old name -> new name. Materials that keep their name reserve it first, so a new name can never collide.
    taken = {n for n, d in diffuse.items() if not d or n in dup}
    mapping: dict[str, str] = {}
    renumbered = 0
    for name, stem in diffuse.items():
        if not stem or name in dup:
            continue
        new = _unique(stem, taken)
        renumbered += new != stem
        if new != name:
            mapping[name] = new
    if not mapping:
        say("  material names: already named after their textures (or none has a diffuse texture)")
        return 0

    # 3. write the .mtl files, then the .obj (only the lines that change are touched; line endings stay)
    def tail(ln: str) -> str:
        return ln[len(ln.rstrip("\r\n")):]

    for m, lines in blocks.items():
        out = []
        for ln in lines:
            s = ln.strip()
            if s.lower().startswith("newmtl"):
                parts = s.split(None, 1)
                old = parts[1].strip() if len(parts) > 1 else ""
                if old in mapping:
                    ln = f"{parts[0]} {mapping[old]}" + tail(ln)
            out.append(ln)
        m.write_bytes("".join(out).encode("utf-8"))

    out, changed = [], 0
    for ln in obj_lines:
        if ln.startswith("usemtl"):
            parts = ln.split(None, 1)
            old = parts[1].strip() if len(parts) > 1 else ""
            if old in mapping:
                ln = f"usemtl {mapping[old]}" + tail(ln)
                changed += 1
        out.append(ln)
    p.write_bytes("".join(out).encode("utf-8"))
    _report(say, "OBJ", list(mapping.items()), renumbered)
    return len(mapping)


# ------------------------------------------------------------------------------------------------------ GLB
def _image_stem(js: dict, tex_index) -> str | None:
    if not isinstance(tex_index, int):
        return None
    textures, images = js.get("textures") or [], js.get("images") or []
    if not 0 <= tex_index < len(textures):
        return None
    src = textures[tex_index].get("source")
    if not isinstance(src, int):
        for ext in (textures[tex_index].get("extensions") or {}).values():      # e.g. a DDS / KTX2 / WebP extension
            if isinstance(ext, dict) and isinstance(ext.get("source"), int):
                src = ext["source"]
                break
    if not isinstance(src, int) or not 0 <= src < len(images):
        return None
    uri, nm = images[src].get("uri"), images[src].get("name")
    if uri and not str(uri).startswith("data:"):
        return _stem(str(uri))
    return _stem(nm) if nm else None                # embedded image: its name, if it has one


def rename_glb_materials(path, say: Say = print) -> int:
    """Rename the materials of a .glb after their base colour texture. Returns how many changed."""
    js, binb = read_glb(path)
    mats = js.get("materials") or []
    if not mats:
        say("  material names: the GLB has no materials")
        return 0

    stems: list[str | None] = []
    for m in mats:
        bct = (m.get("pbrMetallicRoughness") or {}).get("baseColorTexture") or {}
        stems.append(_image_stem(js, bct.get("index")))

    taken = {m.get("name") for m, s in zip(mats, stems) if not s}
    renames: list[tuple[str, str]] = []
    renumbered = 0
    for i, (m, stem) in enumerate(zip(mats, stems)):
        if not stem:
            continue
        new = _unique(stem, taken)
        renumbered += new != stem
        old = m.get("name") or f"material{i}"
        if new != m.get("name"):
            m["name"] = new
            renames.append((old, new))
    if not renames:
        say("  material names: already named after their textures (or none has a base colour texture)")
        return 0
    write_glb(path, js, binb)
    _report(say, "GLB", renames, renumbered)
    return len(renames)
