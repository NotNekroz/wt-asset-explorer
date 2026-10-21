"""skin_blk.py - build the .blk of a War Thunder custom skin (UserSkins/<skin>/<vehicle id>.blk).

Format (official wiki, https://wiki.warthunder.com/cdk/creation_camo, chapter "Work with blk"):

    name:t="user"                       // has to be the first line of the file
    replace_tex {                       // one block per texture
        from:t="f_16c_block_52_aesa_c*" // the game's texture name + '*'
        to:t="f_16c_block_52_aesa_c.tga"// the file in the skin folder: name AND extension must match exactly
    }

The skin textures are TGA files: the game ignored the DDS files this tool used to write (tested in game), while TGA is read.

The game reads the .blk, and wherever the vehicle's model asks for `from` it loads the file `to` from the skin folder.
So a skin = the .blk + the (edited) texture files it names. Nothing else is needed.

Which block: the wiki describes two kinds. `replace_tex` swaps a texture 1:1 (what the original file looks like is what you
edit). `set_tex` + `param:t="camo_skin_tex"` is the "fixed camouflage" kind that also lets the game's in-game decals
show on top. It also needs the alpha channel prepared a certain way, so this tool writes plain `replace_tex` for
everything (it works for aircraft, normal maps, weapons, pylons and interiors alike). Set ALBEDO_BLOCK = "set_tex" to
write the albedo (_c) textures the fixed-camo way instead.
"""
from __future__ import annotations

from pathlib import Path

ALBEDO_BLOCK = "replace_tex"      # "replace_tex" or "set_tex" (the latter adds param:t="camo_skin_tex")
SKIN_SUFFIXES = ("_c", "_n")      # albedo and normal map: the textures a skin is made of
SKIN_EXT = ".tga"                 # texture format of the skin folder (the game does not pick up the exported .dds)
SKIP_PARTS = ("detail", "camo", "flame", "fire", "smoke", "glow", "burner")   # shared / effect textures, left alone


def clean(raw) -> str:
    """'f_16c_block_52_aesa_c*' / 'dir/name.dds' -> 'f_16c_block_52_aesa_c' (the game's texture name)."""
    if not raw:
        return ""
    name = str(raw).strip().replace("\\", "/").split("*")[0].split("/")[-1]
    for ext in (".dds", ".tga"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name


def skin_textures(slots) -> list[str]:
    """Texture names of one material that a skin can replace: the _c / _n ones, minus shared detail and effect maps."""
    out = []
    for raw in slots or ():
        name = clean(raw)
        low = name.lower()
        if not name or not low.endswith(SKIN_SUFFIXES):
            continue
        if any(p in low for p in SKIP_PARTS):
            continue
        if name not in out:
            out.append(name)
    return out


def blk_text(names, ext: str = SKIN_EXT) -> str:
    lines = [          # nothing may come before name:t="user" (not even comments)
        'name:t="user"',
        "",
    ]
    for n in names:
        block = ALBEDO_BLOCK if n.lower().endswith("_c") else "replace_tex"
        lines.append(block + " {")
        lines.append(f'  from:t="{n}*"')
        lines.append(f'  to:t="{n}{ext}"')
        if block == "set_tex":
            lines.append('  param:t="camo_skin_tex"')
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def write_blk(folder: Path, vehicle_id: str, names, ext: str = SKIN_EXT) -> tuple[Path, bool]:
    """Write <folder>/<vehicle id>.blk. An existing .blk is never overwritten (you may have edited it): the new text goes
    to <vehicle id>.blk.new instead. Returns (path written, True when it is the real .blk)."""
    folder = Path(folder)
    target = folder / f"{vehicle_id}.blk"
    text = blk_text(names, ext)
    if target.exists():
        target = folder / f"{vehicle_id}.blk.new"
        target.write_text(text, encoding="utf-8", newline="\n")
        return target, False
    target.write_text(text, encoding="utf-8", newline="\n")
    return target, True


_IMAGE_EXTS = (".tga", ".png", ".dds", ".jpg", ".jpeg", ".bmp")


def read_skin_folder(folder) -> dict[str, Path]:
    """The textures of a custom-skin folder: {game texture name (lower case): image file}. See read_skin_folder_report."""
    return read_skin_folder_report(folder)[0]


def read_skin_folder_report(folder) -> tuple[dict[str, Path], list[str]]:
    """({game texture name (lower case): image file}, notes for the log).

    1. The .blk files of the folder say which game texture ('from') each file ('to') replaces, whatever the file names are
       (leclerc_s2_body_c -> leclerc_s2_custom_body.tga). Blocks are read whatever their order, comments are skipped. The game
       needs the 'to' name AND extension exactly, but the preview is lenient: when the folder has no file of that exact
       name it takes the file with the same name and another image extension, and says so in the notes.
    2. Image files no .blk block claimed are taken by their own name (the default name a generated .blk would use), so a
       folder without a .blk, or a half-finished one, still works for the preview. A texture the .blk does not mention
       and the folder has no default-named file for is simply not replaced.
    A 'to' with no matching file at all is left out and reported."""
    import re

    folder = Path(folder)
    notes: list[str] = []
    if not folder.is_dir():
        return {}, notes
    files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    by_stem: dict[str, list[Path]] = {}
    for p in files.values():
        if p.suffix.lower() in _IMAGE_EXTS:
            by_stem.setdefault(p.stem.lower(), []).append(p)
    for lst in by_stem.values():
        lst.sort(key=lambda p: _IMAGE_EXTS.index(p.suffix.lower()))
    out: dict[str, Path] = {}
    claimed: set[Path] = set()
    for blk in sorted(p for n, p in files.items() if n.endswith(".blk")):
        try:
            text = blk.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        for body in re.findall(r"\{([^{}]*)\}", text):
            src = re.search(r'\bfrom\s*:\s*t\s*=\s*"([^"]*)"', body)
            dst = re.search(r'\bto\s*:\s*t\s*=\s*"([^"]*)"', body)
            if not src or not dst:
                continue
            name = clean(src.group(1)).lower()
            to_name = dst.group(1).strip().replace("\\", "/").split("/")[-1]
            if not name or not to_name:
                continue
            target = files.get(to_name.lower())
            if target is not None and target.suffix.lower() not in _IMAGE_EXTS:
                target = None
            if target is None:
                near = by_stem.get(Path(to_name).stem.lower())
                if near:
                    target = near[0]
                    notes.append(f"{blk.name}: '{name}' -> {to_name} is not in the folder, using {target.name} (the game needs the exact name)")
            if target is None:
                notes.append(f"{blk.name}: '{name}' -> {to_name}: no such file in the folder; the default name '{name}' is tried instead")
                continue
            out[name] = target
            claimed.add(target)
    for stem, lst in by_stem.items():                 # images no .blk block names: the default name
        for p in lst:
            if p not in claimed:
                out.setdefault(stem, p)
                break
    return out, notes
