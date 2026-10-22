"""tex_dump.py - TEMPORARY helper: write every texture the model of one vehicle uses, plus a few named effect textures.

Self-contained on purpose. Nothing imports this module except the one block in wtx/gui.py marked
"TEX_DUMP BUTTON". To remove the feature: delete this file and delete that block.

Output (one folder):
  <texture>.dds   the texture as the game stores it (no camo baked in, no channel changes)
  <texture>.png   the same picture as RGBA, to look at it quickly (decoded from the .dds)
  textures.txt    which material uses which texture in which slot
  fx/             the textures named in EXTRA_TEXTURES (shared effect textures that no model material lists), same .dds + .png
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

# Shared effect textures to extract next to the vehicle's own. Add names here (no extension). They are looked up in the
# game's texture packs by name, whatever vehicle is selected. The afterburner (volumetricAfterBurner in the vehicle's
# .blk) uses the first two: rampTextureOffset / maskTextureOffset are positions inside these pictures.
EXTRA_TEXTURES = ("afterburneratlastex", "afterburnerflamenozzlemask")


def dump_vehicle_textures(lib, ref, out_dir, status, lod: int = 0, extra=EXTRA_TEXTURES) -> int:
    """Write the textures of the vehicle model `ref` (and the `extra` effect textures) into `out_dir`.
    Returns how many textures were written. `lib` is the AssetLibrary of the program (wtx/assets.py); `status` is the log callback."""
    import numpy as np
    from PIL import Image
    from wtx.assets import AssetCacher, DDSx, DDSxTexturePack2, _clean_tex_name, _loadDdsArray, log
    from wtx.skin_blk import clean

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wt_texdump_"))
    report: list[str] = []
    seen: set[str] = set()
    written = 0

    def write_one(mat, name: str, folder: Path) -> bool:
        """Export one texture through `mat` into `folder` as .dds + .png. False when the game has no such texture."""
        mat.exportTexture(name, str(work), True)
        dds = work / "textures" / f"{name}.dds"
        if not dds.exists():
            return False
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dds, folder / f"{name}.dds")
        arr = _loadDdsArray(str(dds), str(work))
        if arr is not None:
            arr = np.ascontiguousarray(np.array(arr, dtype=np.uint8))
            Image.fromarray(arr, "RGBA" if arr.shape[-1] == 4 else "RGB").save(str(folder / f"{name}.png"))
        return True

    try:
        # fixed_skin=False: same camo swaps as the skin template, so the slots are the ones the vehicle really uses
        with lib._vehicle_camo(status, fixed_skin=False):
            mdl = lib._open_model(ref, status, lod, geometry=False)      # materials only, no mesh
            lib._load_texture_packs(mdl, status)
            materials = list(getattr(mdl, "materials", None) or [])
            for m_idx, mat in enumerate(materials):
                m_name = getattr(mat, "name", None) or f"material_{m_idx}"
                report.append(f"[{m_idx}] {m_name}  class={getattr(mat, 'cls', '?')}")
                for s_idx, raw in enumerate(mat.getTextureSlots() or []):
                    name = clean(raw)
                    if not name:
                        continue
                    report.append(f"      slot {s_idx}: {name}")
                    if name in seen:
                        continue
                    seen.add(name)
                    try:
                        if write_one(mat, name, out_dir):
                            written += 1
                        else:
                            status(f"  [texdump] ! {name} was not found in the game files")
                            report.append(f"      ! {name}: not found")
                    except Exception as e:                      # noqa: BLE001 - one bad texture must not stop the rest
                        status(f"  [texdump] ! {name}: {type(e).__name__}: {e}")
                        report.append(f"      ! {name}: {type(e).__name__}: {e}")

            # Shared effect textures: no model material lists them, so their packs are loaded here by name (on top of the
            # packs the model needed, standard packs first and HQ last so the HQ picture wins, like _load_texture_packs).
            extra = [e for e in (extra or ()) if e]
            if extra and not materials:
                status("  [texdump] ! the model has no material to export the effect textures through")
            elif extra:
                packs: dict[str, None] = {}
                for n in extra:
                    where = lib.tex_index.get(_clean_tex_name(n))
                    if not where:
                        status(f"  [texdump] ! {n} is not in any texture pack of the index")
                    for rel in where or ():
                        packs[rel] = None
                for rel in sorted(packs, key=lambda p: 1 if "content.hq" in p.lower() else 0):
                    log.setMuted(True)
                    try:
                        pack = DDSxTexturePack2(str(lib.game_dir / rel))
                        for ddsx in pack.getPackedFiles():
                            AssetCacher.cacheAsset(ddsx)
                    finally:
                        log.setMuted(False)
                    status(f"  texture pack {rel.rsplit('/', 1)[-1]}")
                report.append("")
                report.append("effect textures (fx/):")
                for n in extra:
                    name = _clean_tex_name(n)
                    try:
                        if write_one(materials[0], name, out_dir / "fx"):
                            written += 1
                            report.append(f"      {name}")
                        else:
                            status(f"  [texdump] ! {name} was not found in the game files")
                            report.append(f"      ! {name}: not found")
                    except Exception as e:                      # noqa: BLE001
                        status(f"  [texdump] ! {name}: {type(e).__name__}: {e}")
                        report.append(f"      ! {name}: {type(e).__name__}: {e}")
        (out_dir / "textures.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    finally:
        try:
            AssetCacher.clearCache(DDSx)
        except Exception:                                       # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)
    return written
