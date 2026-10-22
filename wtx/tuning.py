"""tuning.py - the run-time performance settings (Settings > Performance) and how they reach the backend.

Four values, kept in a plain dict so they can be compared, stored and tested without Qt:

    tex_workers       threads that convert + write textures at the end of an export stage   (0 or 1 = no pool)
    pack_workers      threads that parse texture packs that are not in the pack cache        (0 or 1 = one after the other)
    map_workers       threads that build the glTF normal / metal-roughness pictures ahead of the material loop (0 or 1 = no pool)
    clear_pack_cache  every export starts with an empty texture-pack cache (cold packs each time; for fair comparisons)
    profile           cProfile the slow phases and print the top functions after the [timing] table

Starting values come from the environment (WTX_TEX_WORKERS, WTX_PACK_WORKERS, WTX_PROFILE); a value the user saved wins over them.
apply() needs no restart: parse.material.TEXTURE_WORKERS is read whenever a TextureBatch is created, wtx.assets.PACK_WORKERS
whenever texture packs are loaded, parse.gltf_conv.MAP_WORKERS before the glTF material loop, wtx.assets.CLEAR_PACK_CACHE at the start of
every export.
No Qt import here: `qsettings` is anything with value(key, default) / setValue(key, value).
"""
from __future__ import annotations

import os
import sys

TEX_WORKERS_MAX = 16
PACK_WORKERS_MAX = 8
PREFIX = "perf_"            # QSettings keys: perf_tex_workers, perf_pack_workers, perf_map_workers, perf_clear_pack_cache, perf_profile
MAP_WORKERS_MAX = 16
KEYS = ("tex_workers", "pack_workers", "map_workers", "clear_pack_cache", "profile")


def _to_int(value, default: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(hi, n))


def _to_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def defaults() -> dict:
    """The starting values: environment variables if set, else the built-in defaults."""
    return {
        "tex_workers": _to_int(os.environ.get("WTX_TEX_WORKERS"), 4, TEX_WORKERS_MAX),
        "pack_workers": _to_int(os.environ.get("WTX_PACK_WORKERS"), 1, PACK_WORKERS_MAX),
        "map_workers": _to_int(os.environ.get("WTX_MAP_WORKERS"), 4, MAP_WORKERS_MAX),
        "clear_pack_cache": False,
        "profile": _to_bool(os.environ.get("WTX_PROFILE")),
    }


def normalize(values: dict | None) -> dict:
    """A complete, clamped copy of `values`: missing or invalid entries fall back to defaults()."""
    base = defaults()
    values = values or {}
    return {
        "tex_workers": _to_int(values.get("tex_workers"), base["tex_workers"], TEX_WORKERS_MAX),
        "pack_workers": _to_int(values.get("pack_workers"), base["pack_workers"], PACK_WORKERS_MAX),
        "map_workers": _to_int(values.get("map_workers"), base["map_workers"], MAP_WORKERS_MAX),
        "clear_pack_cache": _to_bool(values.get("clear_pack_cache"), base["clear_pack_cache"]),
        "profile": _to_bool(values.get("profile"), base["profile"]),
    }


def load(qsettings) -> dict:
    """The saved values; anything not saved yet starts from defaults()."""
    d = defaults()
    return normalize({k: qsettings.value(PREFIX + k, d[k]) for k in KEYS})


def save(qsettings, values: dict) -> None:
    v = normalize(values)
    for k in KEYS:
        qsettings.setValue(PREFIX + k, v[k])


def apply(values: dict) -> dict:
    """Put the values into the backend modules now. Returns the normalized values that were applied."""
    v = normalize(values)

    import parse.material as material
    import wtx.assets as assets
    material.TEXTURE_WORKERS = v["tex_workers"]
    assets.PACK_WORKERS = v["pack_workers"]
    try:
        import parse.gltf_conv as gltf_conv           # needs pygltflib; without it there is no glTF export to tune
        gltf_conv.MAP_WORKERS = v["map_workers"]
    except Exception:
        pass
    assets.CLEAR_PACK_CACHE = v["clear_pack_cache"]

    # phase_timing.py is a removable module: without it the profiling flag simply has nothing to switch
    pt = sys.modules.get("phase_timing")
    if pt is None:
        try:
            import phase_timing as pt
        except Exception:
            pt = None
    if pt is not None:
        pt.PROFILE = v["profile"]
    return v
