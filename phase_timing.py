"""Per-phase timing of an export.  TEMPORARY / REMOVABLE: delete this file and the block marked
`PHASE TIMING` in main.py and nothing else changes.

What it does: install() wraps ~30 existing functions (model build, texture packs, texture export, glTF writing, weapons ...)
at run time with a stopwatch. When an export finishes (export_gltf / export_obj / export_skin_template), a table is written
to the log through the export's own status callback:

    [timing] ===== export_gltf  f_16c_block_50  -  38.4 s =====
    [timing]   phase                                          calls   self time   share
    [timing]   textures: waiting for the pool                     1     12.31 s     32%
    ...

"self time" excludes the phases nested inside (a phase called from another one is not counted twice), so the shares add up
to the wall time. Everything done while a loadout's weapon models are exported is listed with the prefix "weapons >".
Time spent on worker threads overlaps the wall clock, so it is listed separately and never added to the shares.

The first line of every report shows the live settings (texture, pack and glTF map workers, pack cache cleared per export, profiling), so two
reports can be compared honestly.

Profiling: set WTX_PROFILE=1 (or phase_timing.PROFILE = True at run time, which Settings > Performance does). The phases in PROFILED are
then run under cProfile, one Profile per full label ("weapons > ..." kept apart), accumulated over all calls of the export. The profiler is
switched on by the thread that runs the phase; a phase inside a profiled phase, or one that starts while another profiler is active
(Python 3.12 allows only one), is skipped. Caveat measured on Python 3.12: cProfile there also records the other threads that run while it
is switched on (worker pools, Python code of the GUI thread), not only the calling one. After the table the top functions by own time are
listed. Profiled phases are slower than they really are.

Set WTX_TIMING=0 to turn it off without removing it. Nothing here changes what an export produces.
"""
from __future__ import annotations

import contextlib
import cProfile
import functools
import inspect
import io
import os
import pstats
import sys
import threading
import time

ENABLED = os.environ.get("WTX_TIMING", "1") != "0"
PROFILE = os.environ.get("WTX_PROFILE", "").strip().lower() in ("1", "true", "yes", "on")     # settable at run time
PROFILED = ("model: getModel (mesh build)", "animations", "glTF: normal maps", "glTF: metal / roughness maps")   # base labels (no "weapons > ")
PROFILE_TOP = 12
_perf = time.perf_counter

_tls = threading.local()          # per thread: the stack of open phases, the label prefix
_lock = threading.Lock()
_stats: dict[tuple[str, bool], list] = {}     # (label, ran on a worker thread) -> [calls, self seconds]
_pool = {"jobs": 0, "workers": 0, "wall": 0.0, "busy": 0.0}
_session = {"active": 0}
_profiles: dict[str, cProfile.Profile] = {}   # full label (with its "weapons > " prefix) -> Profile, accumulated over the calls of one export


class _Frame:
    __slots__ = ("label", "t0", "child")

    def __init__(self, label: str):
        self.label, self.t0, self.child = label, _perf(), 0.0


def _stack() -> list:
    s = getattr(_tls, "stack", None)
    if s is None:
        s = _tls.stack = []
    return s


def _record(label: str, seconds: float, background: bool) -> None:
    with _lock:
        row = _stats.setdefault((label, background), [0, 0.0])
        row[0] += 1
        row[1] += seconds


def _profile_start(base: str, full: str):
    """Switch the profiler of this phase on. Returns the Profile, or None when nothing is being profiled: profiling is off, the phase is not
    one of PROFILED, a profiled phase is already open on this thread (nested: the outer profile covers it), or another profiler is active
    (Python 3.12 allows only one: enable() raises ValueError)."""
    if not PROFILE or base not in PROFILED or getattr(_tls, "profiling", False):
        return None
    if threading.current_thread().name.startswith("wtx-"):     # pool workers are never profiled (Python 3.12 allows one profiler at a time)
        return None
    with _lock:
        prof = _profiles.get(full) or cProfile.Profile()
    try:
        prof.enable()
    except ValueError:
        return None
    with _lock:
        _profiles[full] = prof
    _tls.profiling = True
    return prof


def _profile_stop(prof) -> None:
    if prof is not None:
        prof.disable()
        _tls.profiling = False


def phase(label: str, weapons: bool = False):
    """Decorator: time the function under `label` (self time; see module doc). weapons=True labels everything called inside as weapons."""
    def deco(fn):
        if getattr(fn, "_wtx_timed", False):
            return fn

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _session["active"]:
                return fn(*args, **kwargs)

            prefix = getattr(_tls, "prefix", "")
            frame = _Frame(prefix + label)
            stack = _stack()
            stack.append(frame)
            if weapons:
                _tls.prefix = prefix + "weapons > "
            prof = _profile_start(label, frame.label)
            try:
                return fn(*args, **kwargs)
            finally:
                _profile_stop(prof)
                total = _perf() - frame.t0
                stack.pop()
                if weapons:
                    _tls.prefix = prefix
                if stack:
                    stack[-1].child += total
                _record(frame.label, total - frame.child, threading.current_thread().name.startswith("wtx-"))

        wrapper._wtx_timed = True
        return wrapper
    return deco


# ------------------------------------------------------------------------------------------------------------ the report
def _fmt_row(label: str, calls, seconds: float, wall: float) -> str:
    share = f"{100.0 * seconds / wall:5.0f}%" if wall > 0 else "     "
    return f"[timing]   {label:<52s} {calls!s:>5s} {seconds:9.2f} s {share}"


def _report(say, title: str, wall: float) -> None:
    with _lock:
        rows = {k: list(v) for k, v in _stats.items()}
        pool = dict(_pool)

    main = sorted(((lab, c, t) for (lab, bg), (c, t) in rows.items() if not bg), key=lambda r: -r[2])
    side = sorted(((lab, c, t) for (lab, bg), (c, t) in rows.items() if bg), key=lambda r: -r[2])
    accounted = sum(t for _, _, t in main)

    out = [_settings_line(),
           f"[timing] ===== {title}  -  {wall:.2f} s =====",
           f"[timing]   {'phase (self time)':<52s} {'calls':>5s} {'time':>11s} {'share':>6s}"]
    out += [_fmt_row(lab, c, t, wall) for lab, c, t in main]
    out.append(_fmt_row("(not inside any timed phase)", "", max(0.0, wall - accounted), wall))

    if side:
        out.append("[timing]   on worker threads (overlaps the wall clock, summed over threads):")
        out += [f"[timing]     {lab:<50s} {c!s:>5s} {t:9.2f} s" for lab, c, t in side]

    if pool["jobs"]:
        possible = pool["wall"] * pool["workers"]
        use = 100.0 * pool["busy"] / possible if possible > 0 else 0.0
        out.append(f"[timing]   texture pool: {pool['jobs']} textures, up to {pool['workers']} workers, pool open {pool['wall']:.2f} s, "
                   f"workers busy {pool['busy']:.2f} s of {possible:.2f} s possible ({use:.0f}% used)")

    out += _profile_lines()

    for line in out:
        try:
            say(line)
        except Exception:
            print(line)


def _settings_line() -> str:
    """The values this export ran with, read from the live modules (not from the settings dialog)."""
    mat, ast, gl = sys.modules.get("parse.material"), sys.modules.get("wtx.assets"), sys.modules.get("parse.gltf_conv")
    tex = getattr(mat, "TEXTURE_WORKERS", "?")
    packs = getattr(ast, "PACK_WORKERS", "?")
    maps = getattr(gl, "MAP_WORKERS", "?")
    clear = getattr(ast, "CLEAR_PACK_CACHE", None)
    yn = lambda v: "?" if v is None else ("yes" if v else "no")
    return (f"[timing] settings: texture workers {tex}, pack workers {packs}, glTF map workers {maps}, clear pack cache per export {yn(clear)}, "
            f"profiling {yn(PROFILE)}")


def _profile_lines() -> list[str]:
    """The cProfile report: per profiled phase, the PROFILE_TOP functions with the most own time."""
    with _lock:
        profs = dict(_profiles)
    if not profs:
        return ["[timing] profile: nothing was profiled (no profiled phase ran, or another profiler was already active)"] if PROFILE else []

    out = ["[timing] ===== profile: top functions by own time (own time = without the functions they call) ====="]
    blocks = []
    for label, prof in profs.items():
        try:
            buf = io.StringIO()
            st = pstats.Stats(prof, stream=buf)
            st.strip_dirs().sort_stats("tottime").print_stats(PROFILE_TOP)
            body = [ln.rstrip() for ln in buf.getvalue().splitlines() if ln.strip()]
            blocks.append((st.total_tt, label, body))
        except Exception as e:
            blocks.append((0.0, label, [f"(could not read the profile: {type(e).__name__}: {e})"]))
    for total, label, body in sorted(blocks, key=lambda b: -b[0]):
        out.append(f"[timing] --- {label}: {total:.2f} s profiled")
        out += [f"[timing]   {ln}" for ln in body]
    out.append("[timing] note: profiled phases are inflated by the profiling overhead - compare the functions with each other, "
               "not these times with a run that was not profiled")
    if sys.version_info >= (3, 12):
        out.append("[timing] note: on Python 3.12 the profile also counts other threads that ran during the phase, not only the export thread")
    return out


@contextlib.contextmanager
def session(say, title: str):
    """One timed export. Nested sessions (the weapon models are exported through export_gltf too) only add to the outer one."""
    if not ENABLED or _session["active"]:
        yield
        return

    with _lock:
        _stats.clear()
        _profiles.clear()
        _pool.update(jobs=0, workers=0, wall=0.0, busy=0.0)

    _session["active"] = 1
    t0 = _perf()
    try:
        yield
    finally:
        wall = _perf() - t0
        _session["active"] = 0
        try:
            _report(say, title, wall)
        except Exception as e:            # a timing problem must never break an export
            print(f"[timing] report failed: {type(e).__name__}: {e}")


def _find_say(args, kwargs):
    say = kwargs.get("status")
    if callable(say):
        return say
    for a in args[1:]:
        if callable(a) and not isinstance(a, type):
            return a
    return print


def entry(kind: str):
    """Decorator for export_gltf / export_obj / export_skin_template: report when the (outermost) call ends."""
    def deco(fn):
        if getattr(fn, "_wtx_timed", False):
            return fn

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not ENABLED:
                return fn(*args, **kwargs)
            ref = args[1] if len(args) > 1 else None
            title = f"{kind}  {getattr(ref, 'name', '') or ''}".rstrip()
            with session(_find_say(args, kwargs), title):
                return fn(*args, **kwargs)

        wrapper._wtx_timed = True
        return wrapper
    return deco


# ------------------------------------------------------------------------------------------------------------- hooking
def _replace_everywhere(name: str, original, replacement, prefixes=("parse", "wtx", "util")) -> int:
    """Rebind a module-level function in every module that imported it by name (gltf_conv binds bakeCamoPairs at import)."""
    n = 0
    for modname, mod in list(sys.modules.items()):
        if mod is None or not modname.split(".")[0] in prefixes:
            continue
        if getattr(mod, name, None) is original:
            setattr(mod, name, replacement)
            n += 1
    return n


def _hook_function(module: str, name: str, decorator) -> str | None:
    mod = sys.modules.get(module)
    orig = getattr(mod, name, None) if mod else None
    if orig is None or not callable(orig):
        return f"{module}.{name}"
    _replace_everywhere(name, orig, decorator(orig))
    return None


def _hook_method(cls, name: str, decorator) -> str | None:
    raw = inspect.getattr_static(cls, name, None)
    if raw is None:
        return f"{cls.__name__}.{name}"
    if isinstance(raw, staticmethod):
        setattr(cls, name, staticmethod(decorator(raw.__func__)))
    elif inspect.isfunction(raw):
        setattr(cls, name, decorator(raw))
    else:
        return f"{cls.__name__}.{name}"
    return None


_installed = False


def install() -> str:
    """Wrap the functions listed below. Safe to call twice. Returns a one-line summary (also printed)."""
    global _installed
    if not ENABLED or _installed:
        return "timing off" if not ENABLED else "already installed"

    import wtx.assets as assets
    import parse.material as material
    import parse.realres as realres
    for extra in ("parse.gltf_conv", "parse.fx_strip", "parse.loadout_filter", "parse.glb_merge", "parse.tex_prune"):
        try:
            __import__(extra)
        except Exception:
            pass

    missing: list[str] = []
    hooked = 0

    def method(cls, name, label, **kw):
        nonlocal hooked
        bad = _hook_method(cls, name, phase(label, **kw))
        missing.append(bad) if bad else None
        hooked += 0 if bad else 1

    def func(module, name, label, **kw):
        nonlocal hooked
        bad = _hook_function(module, name, phase(label, **kw))
        missing.append(bad) if bad else None
        hooked += 0 if bad else 1

    L = assets.AssetLibrary
    method(L, "_ensure_descs", "game descriptions (.desc)")
    method(L, "_preload_companions", "skeleton / collision preload")
    method(L, "_load_texture_packs", "texture packs: choose + load")
    method(L, "_pack_entries", "texture packs: parse (cache misses)")
    method(L, "_prune_unused_textures", "prune unused textures")
    method(L, "_add_animations", "animations")
    method(L, "_open_model", "open model (skin template)")
    method(L, "attach_mounts", "weapons: placement + merge", weapons=True)
    method(L, "attach_mounts_obj", "weapons: placement + merge", weapons=True)

    for cls in (realres.DynModel, realres.RendInst):
        method(cls, "computeData", "model: computeData")
        method(cls, "getModel", "model: getModel (mesh build)")
    method(realres.Model, "exportTextures", "textures: plan + export")
    method(realres.Model, "exportObj", "OBJ: mtl + obj writing")

    method(material.TextureBatch, "flush", "textures: waiting for the pool")
    method(material.MaterialTemplateLibrary, "exportTextures", "textures: material stage")
    method(material.DDSx, "getDDS", "texture: read + decompress")
    method(material.MaterialData, "convertTex", "texture: Pillow convert")
    method(material.MaterialData, "_runTextureJob", "texture: whole job")
    func("parse.material", "bakeCamoPairs", "camo bake")
    for fn in ("decodeNormalForGltf", "decodeNormalForObj", "decodeNormalForObjSrgb", "decodeGlossMetalForObj"):
        func("parse.material", fn, "normal / gloss conversions")
    func("parse.gltf_conv", "make_gltf_normal", "glTF: normal maps")
    func("parse.gltf_conv", "make_gltf_mr", "glTF: metal / roughness maps")
    func("parse.gltf_conv", "precompute_gltf_maps", "glTF: maps (plan + wait for pool)")
    func("parse.gltf_conv", "export_model_to_gltf", "glTF: build + write")
    for fn in ("strip_glb", "strip_obj"):
        func("parse.fx_strip", fn, "effect strip")
    for fn in ("hide_nodes_glb", "hide_nodes_obj", "mirror_nodes_glb", "mirror_nodes_obj"):
        func("parse.loadout_filter", fn, "loadout filter / mirror")
    if "parse.glb_merge" in sys.modules:
        method(sys.modules["parse.glb_merge"].GlbMerger, "load_model", "weapons: GLB load")
        method(sys.modules["parse.glb_merge"].GlbMerger, "save", "weapons: GLB save")

    for name, kind in (("export_gltf", "export_gltf"), ("export_obj", "export_obj"), ("export_skin_template", "skin template")):
        bad = _hook_method(L, name, entry(kind))
        missing.append(bad) if bad else None
        hooked += 0 if bad else 1

    # the pool statistics (jobs, workers, busy time) are kept by TextureBatch itself: pick them up after every flush
    batch_cls = material.TextureBatch
    flush = batch_cls.flush

    @functools.wraps(flush)
    def flush_with_stats(self):
        try:
            return flush(self)
        finally:
            last = getattr(batch_cls, "last", None) or {}
            if _session["active"] and last:
                with _lock:
                    _pool["jobs"] += last.get("jobs", 0)
                    _pool["workers"] = max(_pool["workers"], last.get("workers", 0))
                    _pool["wall"] += last.get("wall", 0.0)
                    _pool["busy"] += last.get("busy", 0.0)
                batch_cls.last = {}
    flush_with_stats._wtx_timed = True
    batch_cls.flush = flush_with_stats

    _installed = True
    msg = f"[timing] installed {hooked} hooks" + (f"; not found (skipped): {', '.join(missing)}" if missing else "")
    return msg
