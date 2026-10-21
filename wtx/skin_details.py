"""skin_details.py - "Details" layers for custom-skin painting: the panel lines, rivets and weld seams of a vehicle, as pictures
that can be pasted over a flat paint job. One file per game texture, same name as the skin texture, all TGA:

  Details/<c>.png   grey layer with an alpha (PNG, so Photoshop shows it as transparent): panel lines and details only (grooves dark, rivet edges light), flat areas transparent;
                    the curvature of the _n of the same material, same size as the _c. Nothing from the game's paint (no markings, no camo). Materials without
                    a normal map get no file. Put it above your paint in Overlay / Soft Light
  Details/<n>.tga   the normal map in the game's own layout (X in alpha, Y in green, red and blue 0): paste only its alpha and
                    green channels over your own normal map

Nothing here has a hand-set strength for the detail itself: the grey map is scaled by the strongest 0.5 % of its own
values, so every texture uses its full range. DETAIL_CONTRAST only says how much of the grey range that is.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

DETAILS_DIR = "Details"
C_DETAIL_FORMAT = "png"      # "png" or "tga" for the _c layers. PNG: Photoshop opens its alpha as real transparency (it showed a TGA's alpha as an opaque layer)
DETAIL_CONTRAST = 0.8     # 1.0 = the strongest edges reach pure black / white, 0.8 leaves some room
FLIP_Y = False            # True if grooves and rivets come out the wrong way round (the game's Y channel pointing the other way)


def _norm(values: np.ndarray) -> np.ndarray:
    """float array (signed) -> float32 in [-1, 1], scaled by the 99.5th percentile of |values| (sampled every 4th pixel)."""
    sample = np.abs(values[::4, ::4])
    scale = float(np.percentile(sample, 99.5)) if sample.size else 0.0
    if scale < 1e-9:
        return np.zeros(values.shape, np.float32)
    return np.clip(values / scale, -1.0, 1.0).astype(np.float32)


def _to_grey(norm: np.ndarray) -> np.ndarray:
    """[-1, 1] -> uint8 grey, 0 = 128."""
    return ((np.clip(norm, -1.0, 1.0) * (127.5 * DETAIL_CONTRAST) + 127.5) + 0.5).astype(np.uint8)


def normal_game(arr: np.ndarray) -> np.ndarray:
    """RGBA in the game's layout: alpha = X, green = Y, red and blue 0 (no gloss, no metal)."""
    out = np.zeros(arr.shape[:2] + (4,), np.uint8)
    out[..., 1] = arr[..., 1]
    out[..., 3] = arr[..., 3]
    return out


def cavity(arr: np.ndarray) -> np.ndarray:
    """Signed map ([-1, 1]) of the normal map's curvature: the divergence of its X / Y slopes. Convex (rivet heads, raised seams) light,
    concave (grooves, panel gaps) dark, flat 128."""
    nx = arr[..., 3].astype(np.float32) * (2.0 / 255.0) - 1.0
    ny = arr[..., 1].astype(np.float32) * (2.0 / 255.0) - 1.0
    # image rows run top to bottom, the normal's +Y points up the picture: d(ny)/d(up) = -d(ny)/d(row)
    dny = np.gradient(ny, axis=0)
    div = np.gradient(nx, axis=1) + (dny if FLIP_Y else -dny)
    return _norm(div)


def lines_from_colour(rgb: np.ndarray) -> np.ndarray:
    """Signed map ([-1, 1]) of the fine detail of a colour texture: luminance minus its Gaussian blur (radius = width / 512)."""
    lum = Image.fromarray(np.ascontiguousarray(rgb[..., :3]), "RGB").convert("L")
    radius = max(2.0, lum.size[0] / 512.0)
    blurred = lum.filter(ImageFilter.GaussianBlur(radius))
    diff = np.asarray(lum, dtype=np.float32) - np.asarray(blurred, dtype=np.float32)
    return _norm(diff)


def _resized(norm: np.ndarray, size) -> np.ndarray:
    if norm.shape[1] == size[0] and norm.shape[0] == size[1]:
        return norm
    return np.asarray(Image.fromarray(norm, "F").resize(tuple(size), Image.BILINEAR), dtype=np.float32)


def write_normal_details(out_dir: Path, name: str, arr: np.ndarray) -> int:
    """<n>.tga in the game's layout. Returns the number of files written (1)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(normal_game(arr)), "RGBA").save(str(out_dir / f"{name}.tga"), format="TGA")
    return 1


def write_colour_details(out_dir: Path, name: str, rgb: np.ndarray, normal_arr: np.ndarray | None) -> int:
    """<c>.png (or .tga, see C_DETAIL_FORMAT): the curvature of the material's normal map as a grey layer (grooves dark, rivet edges light)
    whose alpha is the strength of the detail: flat areas have alpha 0 (no data), only the panel lines and details are visible.
    The colour texture's own detail (lines_from_colour) is NOT used: it also picks up everything painted on it (markings, text,
    camo edges, the border of each UV island). A material without a normal map gets no file. Returns the number of files written."""
    if normal_arr is None:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    size = (rgb.shape[1], rgb.shape[0])
    detail = _resized(cavity(normal_arr), size)
    out = np.empty(detail.shape + (4,), np.uint8)
    out[..., :3] = _to_grey(detail)[..., None]
    out[..., 3] = (np.clip(np.abs(detail), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    img = Image.fromarray(np.ascontiguousarray(out), "RGBA")
    if C_DETAIL_FORMAT == "png":
        img.save(str(out_dir / f"{name}.png"), compress_level=1)
    else:
        img.save(str(out_dir / f"{name}.tga"), format="TGA")
    return 1
