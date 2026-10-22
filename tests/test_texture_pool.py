"""Tests for steps 2 + 3 (and, from section 10 on, the Performance settings, profiling and settings line) against the REAL classes (MaterialData, DDSx, TextureBatch, AssetLibrary, phase_timing).
Only the game files are synthetic: tiny DXT1 DDSx entries, and a fake DDSxTexturePack2 for the pack-cache test."""
import os, re, sys, time, struct, tempfile, shutil, threading, importlib
sys.path.insert(0, os.path.dirname(__file__))
import boot  # noqa

import util.log as log
from util.assetcacher import AssetCacher
import parse.material as material
from parse.material import DDSx, MaterialData, TexturePathDict, TextureBatch, textureBatch
import wtx.assets as assets

OK = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    OK.append(bool(cond))

TMP = tempfile.mkdtemp(prefix="wtx_t_")

# ---------------------------------------------------------------- synthetic DDSx (real class, real getDDS, real Pillow convert)
def make_ddsx(name, w=64, h=64, seed=0):
    import random
    rnd = random.Random(seed)
    memSz = max(1, w // 4) * max(1, h // 4) * 8                      # one DXT1 mip
    payload = bytes(rnd.getrandbits(8) for _ in range(memSz))
    f = os.path.join(TMP, f"{name}.ddsx")
    with open(f, "wb") as fh:
        fh.write(b"\0" * 0x40 + payload)
    hdr = DDSx.Header(struct.pack("<4s4sIHHBBHHBBII", b"DDSx", b"DXT1", 0, w, h, 1, 0, 1, 4, 0, 0, memSz, memSz))
    return DDSx(f, name, memSz, 0x40, hdr)

def make_mat(*texs):
    m = MaterialData()
    m.cls = "rendinst_simple"
    m.par = ""
    for i, t in enumerate(texs):
        m.addTexSlot(f"t{i}", t)
    return m

def reset():
    MaterialData._exportedTextures.clear()
    AssetCacher.clearCache()

def tree(d):
    out = {}
    for root, _, files in os.walk(d):
        for fn in files:
            p = os.path.join(root, fn)
            out[os.path.relpath(p, d)] = open(p, "rb").read()
    return out

N = 24
names = [f"tex_{i:02d}_c" for i in range(N)]

def run_export(out, workers, dup=True, paths=None, latency=0.0):
    reset()
    for i, n in enumerate(names):
        AssetCacher.cacheAsset(make_ddsx(n, seed=i))
        if i % 3 == 0:                                                # a second, larger copy of some textures: getBestTex must pick it
            AssetCacher.cacheAsset(make_ddsx(n, w=128, h=128, seed=100 + i))
    mat = make_mat(*[n + "*" for n in names[:3]])
    star = "" if paths is not None else "*"
    orig = DDSx.getDDS
    if latency:
        def slow(self):
            time.sleep(latency)                                       # stands in for the file read + decompression (GIL released)
            return orig(self)
        DDSx.getDDS = slow
    try:
        os.makedirs(out, exist_ok=True)
        TextureBatch.last = {}
        material.TEXTURE_WORKERS = workers
        t0 = time.perf_counter()
        with textureBatch():
            for n in names:
                mat.exportTexture(n + star, out, True, paths)
                if dup:
                    mat.exportTexture(n, out, True, paths)            # the same file asked for again (another slot / material)
        return time.perf_counter() - t0, dict(TextureBatch.last)
    finally:
        DDSx.getDDS = orig

# ---------------------------------------------------------------- 1. same files, sequential vs pool
a = os.path.join(TMP, "seq"); b = os.path.join(TMP, "par")
ts, st_s = run_export(a, 1)
tp, st_p = run_export(b, 4)
ta, tb = tree(a), tree(b)
check(len(ta) == N and set(ta) == set(tb), f"1. {N} distinct texture files, same set with 1 and 4 workers")
check(ta == tb, "1. every file byte-identical, sequential vs 4-worker pool")
check(all(v[:4] == b"DDS " for v in tb.values()), "1. outputs are real DDS files")
big = [k for k in tb if int(k.split('_')[1][:2]) % 3 == 0]
check(all(len(tb[k]) > 8000 for k in big), "1. getBestTex still picks the larger copy (128x128) when two exist")
check(st_p.get("jobs") == N and st_p.get("workers") == 4, f"1. duplicates collapsed in the plan: {st_p.get('jobs')} jobs for {2 * N} requests, {st_p.get('workers')} workers")

# ---------------------------------------------------------------- 2. the pool really overlaps work (latency stands in for decompress/read)
c = os.path.join(TMP, "lat1"); d = os.path.join(TMP, "lat4")
t1, _ = run_export(c, 1, latency=0.05)
t4, s4 = run_export(d, 4, latency=0.05)
check(tree(c) == tree(d), "2. with latency: still identical output")
check(t4 < t1 * 0.5, f"2. 4 workers overlap simulated I/O+decompress: {t1:.2f}s -> {t4:.2f}s (simulated latency, not a real-game measurement)")
check(s4["busy"] > s4["wall"] * 1.8, f"2. pool stats: busy {s4['busy']:.2f}s over wall {s4['wall']:.2f}s")

# ---------------------------------------------------------------- 3. log state: workers cannot corrupt the indent, and stay silent
check(log.curLevel == 0, "3. main-thread log indent untouched (0)")
seen = []
log.setSink(lambda m, t: seen.append((threading.current_thread().name, m)))
run_export(os.path.join(TMP, "logs"), 4)
log.setSink(None)
check(all(not n.startswith("wtx-") for n, _ in seen) and len(seen) >= 1, f"3. no log line came from a worker thread ({len(seen)} lines, all from the calling thread)")
check(any("Wrote" in m for _, m in seen), "3. the 'Wrote N bytes' lines are still logged (by the calling thread)")
box = []
def hammer():
    log.setWorkerThread()
    try:
        for _ in range(2000):
            log.addLevel(); log.addLevel(); log.subLevel(); log.subLevel()
        box.append(log.getLevelStr() == "")
    except Exception as e:
        box.append(repr(e))
ths = [threading.Thread(target=hammer) for _ in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
check(box == [True] * 8 and log.curLevel == 0, "3. 8 threads hammering addLevel/subLevel: no 'Impossible level', indent intact")

# ---------------------------------------------------------------- 4. renamed (masked) textures keep their name, same-file jobs keep request order
reset()
paths = TexturePathDict()
paths._TexturePathDict__d["tex_00_c"] = TexturePathDict.Texture("tex_00_c", material.TEXTURE_MASKED, "x")
e = os.path.join(TMP, "named")
run_export(e, 4, dup=False, paths=paths)
te = tree(e)
check(os.path.join("textures", "tex_00_cm.dds") in te and os.path.join("textures", "tex_00_c.dds") not in te,
      "4. a masked texture is written under its new name (…m), the plan resolved it without converting")

# ---------------------------------------------------------------- 5. errors: the others are still written, the first error is raised
reset()
for i, n in enumerate(names[:6]):
    AssetCacher.cacheAsset(make_ddsx(n, seed=i))
bad = AssetCacher.getCachedAsset(DDSx, names[2])[0]
bad.getDDS = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
mat = make_mat(names[0])
f = os.path.join(TMP, "err"); os.makedirs(f)
material.TEXTURE_WORKERS = 4
raised = None
try:
    with textureBatch():
        for n in names[:6]:
            mat.exportTexture(n, f, True)
except RuntimeError as ex:
    raised = ex
files = os.listdir(os.path.join(f, "textures"))
check(raised is not None and str(raised) == "boom", "5. a failing texture raises its error when the batch ends")
check(len(files) == 5, f"5. the other 5 textures were still written ({len(files)} files)")
reset()
for i, n in enumerate(names[:6]):
    AssetCacher.cacheAsset(make_ddsx(n, seed=i))
bad = AssetCacher.getCachedAsset(DDSx, names[2])[0]
bad.getDDS = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
f2 = os.path.join(TMP, "err2"); os.makedirs(f2)
with textureBatch(raise_errors=False) as bt:
    for n in names[:6]:
        mat.exportTexture(n, f2, True)
check(len(os.listdir(os.path.join(f2, "textures"))) == 5, "5. raise_errors=False (the MTL stage) only warns")
# an exception in the plan loop drops the queued work and clears the batch
reset()
AssetCacher.cacheAsset(make_ddsx(names[0], seed=0))
f3 = os.path.join(TMP, "err3"); os.makedirs(f3)
try:
    with textureBatch():
        mat.exportTexture(names[0], f3, True)
        raise KeyboardInterrupt
except KeyboardInterrupt:
    pass
check(material.activeTextureBatch() is None and not os.path.exists(os.path.join(f3, "textures", names[0] + ".dds")),
      "5. cancel inside the block: queued textures dropped, no batch left active")

# ---------------------------------------------------------------- 6. no batch = the old behaviour (written at once)
reset()
AssetCacher.cacheAsset(make_ddsx(names[0], seed=0))
f4 = os.path.join(TMP, "inline"); os.makedirs(f4)
mat.exportTexture(names[0], f4, True)
check(os.path.exists(os.path.join(f4, "textures", names[0] + ".dds")), "6. exportTexture outside a batch writes the file before it returns")
nested_out = os.path.join(TMP, "nested"); os.makedirs(nested_out)
reset(); AssetCacher.cacheAsset(make_ddsx(names[0], seed=0)); AssetCacher.cacheAsset(make_ddsx(names[1], seed=1))
with textureBatch() as outer:
    with textureBatch() as inner:
        mat.exportTexture(names[0], nested_out, True)
    check(inner is outer and not os.path.exists(os.path.join(nested_out, "textures", names[0] + ".dds")), "6. nested batch joins the outer one (nothing written until the outer flushes)")
    outer.flush()
    check(os.path.exists(os.path.join(nested_out, "textures", names[0] + ".dds")), "6. an explicit flush() writes what is queued so far")

# ---------------------------------------------------------------- 7. MaterialTemplateLibrary stage: the files exist before the normal-map decode
reset()
AssetCacher.cacheAsset(make_ddsx("veh_c", seed=1)); AssetCacher.cacheAsset(make_ddsx("veh_n", seed=2))
m = MaterialData(); m.cls = "rendinst_simple"; m.par = ""; m.addTexSlot("t0", "veh_c*"); m.addTexSlot("t2", "veh_n*")   # slot 2 = the normal map
mtl = material.MaterialTemplateLibrary([m])
seen_at_decode = {}
orig_dec = material.decodeNormalForGltf if hasattr(material, "decodeNormalForGltf") else None
def spy(p):
    seen_at_decode[p] = os.path.exists(p)
material.decodeNormalForGltf = spy; material.decodeNormalForObj = spy; material.decodeNormalForObjSrgb = spy; material.decodeGlossMetalForObj = spy
material.TEXTURE_WORKERS = 4
g = os.path.join(TMP, "mtl"); os.makedirs(g)
mtl.exportTextures(g, True)
check(len(seen_at_decode) >= 1 and all(seen_at_decode.values()) and os.path.exists(os.path.join(g, "textures", "veh_c.dds")),
      f"7. MTL stage: the normal decode ran ({len(seen_at_decode)} call) and its source file already existed (the batch was flushed first)")

# ---------------------------------------------------------------- 8. pack cache (step 3), with a fake pack class that counts parses
class FakePack:
    parses = []
    delay = 0.15
    def __init__(self, fp):
        FakePack.parses.append((os.path.basename(fp), threading.current_thread().name))
        time.sleep(FakePack.delay)                                    # stands in for reading + parsing a big dxp.bin
        self._fp = fp
    def getPackedFiles(self):
        hq = "content.hq" in self._fp.lower()
        return [make_fake_entry("shared_tex", 128 if hq else 128, os.path.basename(self._fp)), make_fake_entry("only_" + os.path.basename(self._fp), 64, "")]

def make_fake_entry(name, px, tag):
    x = make_ddsx.__globals__["DDSx"].__new__(DDSx)
    x.__dict__["_FilePathable__name"] = name
    class E:  # minimal DDSx look-alike for the cache test (name + pixel count + origin tag)
        pass
    e = E(); e.name = name; e.px = px; e.tag = tag
    return e

game = os.path.join(TMP, "game"); os.makedirs(os.path.join(game, "content", "base", "res"))
os.makedirs(os.path.join(game, "content.hq", "res"))
rels = [f"content/base/res/p{i}.dxp.bin" for i in range(4)] + ["content.hq/res/p_hq.dxp.bin"]
for r in rels:
    open(os.path.join(game, r), "wb").write(b"x" * 10)

lib = object.__new__(assets.AssetLibrary)
lib.game_dir = type(assets.Path(game))(game)
lib._pack_cache = {}
lib._pack_cache_lock = threading.Lock()
lib._pack_batch = None
lib.tex_index = {f"tex{i}": [r] for i, r in enumerate(rels)}
class M:                                                              # a material that wants one texture from every pack
    textures = [f"tex{i}" for i in range(len(rels))]
    def getParams(self): return {}
class Mdl: materials = [M()]
assets.DDSxTexturePack2 = FakePack
assets.AssetCacher.cacheAsset = AssetCacher.cacheAsset
msgs = []
AssetCacher.clearCache()
assets.PACK_WORKERS = 4                                               # the default is 1 now (no gain from threads); this test is about the pool itself
t0 = time.perf_counter(); lib._load_texture_packs(Mdl(), msgs.append); cold = time.perf_counter() - t0
check(len(FakePack.parses) == 5, "8. cold: 5 packs parsed")
check(cold < 0.15 * 5 * 0.6, f"8. cold packs parsed in parallel: {cold:.2f}s for 5 x 0.15s (sequential would be {0.75:.2f}s)")
check(all(n.startswith("wtx-pack") for _, n in FakePack.parses), "8. parsing ran on the pack workers")
order = [m for m in msgs if m.strip().startswith("texture pack")]
check(order[-1].strip().endswith("p_hq.dxp.bin"), "8. registered in fixed order, HQ pack last")
FakePack.parses.clear(); AssetCacher.clearCache()
t0 = time.perf_counter(); lib._load_texture_packs(Mdl(), msgs.append); warm = time.perf_counter() - t0
check(not FakePack.parses and warm < 0.05, f"8. second export: 0 packs re-parsed ({warm * 1000:.0f} ms) - was a full re-read before")
reg = AssetCacher.getCachedAsset(DDSx, "shared_tex") if False else None
os.utime(os.path.join(game, rels[0]), None); open(os.path.join(game, rels[0]), "ab").write(b"y")
lib._load_texture_packs(Mdl(), msgs.append)
check(len(FakePack.parses) == 1 and FakePack.parses[0][0] == "p0.dxp.bin", "8. a pack whose file changed on disk is re-parsed, the others are not")
assets.PACK_CACHE_MAX = 3
FakePack.parses.clear(); lib._pack_cache.clear()
lib._load_texture_packs(Mdl(), msgs.append)
check(len(lib._pack_cache) == 3, "8. cache is bounded (PACK_CACHE_MAX)")
# weapon-batch semantics are unchanged
assets.PACK_CACHE_MAX = 32
lib._pack_batch = set(); FakePack.parses.clear(); lib._pack_cache.clear()
lib._load_texture_packs(Mdl(), msgs.append)
msgs.clear(); lib._load_texture_packs(Mdl(), msgs.append)
check(any("already loaded" in m for m in msgs), "8. inside a weapon batch the 'already loaded' shortcut still works")
lib._pack_batch = None

# ---------------------------------------------------------------- 9. phase_timing.install(): every hook it asks for exists now
import phase_timing
phase_timing.ENABLED = True
res = phase_timing.install()
print(res)
check("not found" not in res, "9. phase_timing.install(): no missing hook")

lines = []
with phase_timing.session(lines.append, "export_gltf  selftest"):
    run_export(os.path.join(TMP, "timed"), 4, latency=0.02)
    lib._load_texture_packs(Mdl(), lambda m: None)
print("\n".join(lines))
check(any("texture pack" in l and "workers busy" in l for l in lines) or any("texture pool" in l for l in lines), "9. the report shows the texture pool line")
check(any("textures: waiting for the pool" in l for l in lines), "9. flush() is timed as 'waiting for the pool'")
check(any("texture: whole job" in l for l in lines) and any("worker threads" in l for l in lines), "9. worker-thread jobs are listed separately")

# ---------------------------------------------------------------- 10. wtx/tuning.py (Settings > Performance; everything except the Qt dialog itself)
import ast
import wtx.tuning as tuning

SAVED = (material.TEXTURE_WORKERS, assets.PACK_WORKERS, assets.CLEAR_PACK_CACHE, phase_timing.PROFILE)
ENV_KEYS = ("WTX_TEX_WORKERS", "WTX_PACK_WORKERS", "WTX_PROFILE")
ENV_SAVED = {k: os.environ.get(k) for k in ENV_KEYS}

def set_env(**kw):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(kw)

class FakeQ:                                                          # QSettings look-alike; like the .ini / registry backends it hands strings back
    def __init__(self): self.d = {}
    def value(self, key, default=None): return self.d.get(key, default)
    def setValue(self, key, v): self.d[key] = str(v).lower() if isinstance(v, bool) else str(v)

set_env()
check(tuning.defaults() == {"tex_workers": 4, "pack_workers": 1, "map_workers": 4, "clear_pack_cache": False, "profile": False}, "10. defaults without environment: 4 texture workers, 1 pack worker, 4 map workers, cache kept, no profiling")
set_env(WTX_TEX_WORKERS="7", WTX_PACK_WORKERS="3", WTX_MAP_WORKERS="5", WTX_PROFILE="1")
check(tuning.defaults() == {"tex_workers": 7, "pack_workers": 3, "map_workers": 5, "clear_pack_cache": False, "profile": True}, "10. defaults read WTX_TEX_WORKERS / WTX_PACK_WORKERS / WTX_MAP_WORKERS / WTX_PROFILE")
set_env(WTX_TEX_WORKERS="abc", WTX_PACK_WORKERS="-2", WTX_MAP_WORKERS="x", WTX_PROFILE="maybe")
check(tuning.defaults() == {"tex_workers": 4, "pack_workers": 0, "map_workers": 4, "clear_pack_cache": False, "profile": False}, "10. garbage in the environment falls back / clamps instead of failing")
set_env(WTX_TEX_WORKERS="99", WTX_PACK_WORKERS="99", WTX_MAP_WORKERS="99")
check(tuning.defaults()["tex_workers"] == 16 and tuning.defaults()["pack_workers"] == 8 and tuning.defaults()["map_workers"] == 16, "10. environment values are clamped to the spin box ranges (16 / 8 / 16)")

set_env()
check(tuning.normalize({"tex_workers": 99, "pack_workers": -5, "map_workers": 40, "clear_pack_cache": "false", "profile": "true"}) ==
      {"tex_workers": 16, "pack_workers": 0, "map_workers": 16, "clear_pack_cache": False, "profile": True}, "10. normalize clamps numbers and reads 'true'/'false' strings")
check(tuning.normalize(None) == tuning.defaults() and tuning.normalize({"tex_workers": 2})["pack_workers"] == 1, "10. normalize fills what is missing from the defaults")

q = FakeQ()
set_env(WTX_TEX_WORKERS="6", WTX_PROFILE="1")
check(tuning.load(q) == tuning.defaults(), "10. load with nothing saved = the starting values (environment)")
want = {"tex_workers": 8, "pack_workers": 2, "map_workers": 3, "clear_pack_cache": True, "profile": False}
tuning.save(q, want)
check(tuning.load(q) == want and all(k.startswith("perf_") for k in q.d), "10. save / load round trip (string-typed store), keys are perf_*")
check(tuning.load(q)["profile"] is False, "10. a saved value wins over the environment")
set_env()

import parse.gltf_conv as gltf_conv
SAVED = SAVED + (gltf_conv.MAP_WORKERS,)
got = tuning.apply({"tex_workers": 6, "pack_workers": 2, "map_workers": 7, "clear_pack_cache": True, "profile": True})
check(material.TEXTURE_WORKERS == 6 and assets.PACK_WORKERS == 2 and gltf_conv.MAP_WORKERS == 7 and assets.CLEAR_PACK_CACHE is True and phase_timing.PROFILE is True,
      "10. apply() sets TEXTURE_WORKERS, PACK_WORKERS, MAP_WORKERS, CLEAR_PACK_CACHE and phase_timing.PROFILE")
check(got == {"tex_workers": 6, "pack_workers": 2, "map_workers": 7, "clear_pack_cache": True, "profile": True}, "10. apply() returns the values it applied")
check(TextureBatch().workers == 6, "10. a new TextureBatch picks the new worker count up (no restart)")
tuning.apply({"tex_workers": 0, "pack_workers": 0, "map_workers": 0, "clear_pack_cache": False, "profile": False})
check(TextureBatch().workers == 0 and assets.PACK_WORKERS == 0 and gltf_conv.MAP_WORKERS == 0 and assets.CLEAR_PACK_CACHE is False and phase_timing.PROFILE is False, "10. apply() again: 0 / 0 / 0 / off / off")

phase_timing.PROFILE = True
sys.modules["phase_timing"] = None                                    # as if phase_timing.py had been deleted: `import phase_timing` raises ImportError
try:
    ok = True
    try:
        tuning.apply({"tex_workers": 3, "pack_workers": 1, "map_workers": 2, "clear_pack_cache": False, "profile": False})
    except Exception:
        ok = False
finally:
    sys.modules["phase_timing"] = phase_timing
check(ok and material.TEXTURE_WORKERS == 3 and gltf_conv.MAP_WORKERS == 2 and phase_timing.PROFILE is True, "10. apply() without phase_timing: no error, the other values still apply")

src = open(os.path.join(boot.REPO, "wtx", "settings_dialog.py")).read()
cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")
init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
argn = [a.arg for a in init.args.args]
check(argn == ["self", "parent", "game_dir", "out_dir", "all_armament", "loadout_icons", "tuning"] and "spin_map" in src and len(init.args.defaults) == 1
      and isinstance(init.args.defaults[0], ast.Constant) and init.args.defaults[0].value is None
      and any(isinstance(n, ast.FunctionDef) and n.name == "tuning" for n in cls.body),
      "10. (source check, the dialog is not run) SettingsDialog keeps its old parameters, new optional tuning=None, has tuning()")
gsrc = open(os.path.join(boot.REPO, "wtx", "gui.py")).read()
gtree = ast.parse(gsrc)
def gfn(name): return ast.get_source_segment(gsrc, next(n for n in ast.walk(gtree) if isinstance(n, ast.FunctionDef) and n.name == name))
check("tuning.load(" in gfn("_startup") and "tuning.apply(" in gfn("_startup"), "10. (source check) MainWindow._startup loads and applies the saved tuning")
check("tuning=self.tuning" in gfn("_open_settings") and "tuning.apply(dlg.tuning())" in gfn("_open_settings") and "tuning.save(" in gfn("_open_settings"),
      "10. (source check) _open_settings passes self.tuning in, applies and saves what the dialog returns")

# ---------------------------------------------------------------- 11. CLEAR_PACK_CACHE + AssetLibrary._begin_export
FakePack.delay = 0.0
assets.PACK_WORKERS = 1
assets.CLEAR_PACK_CACHE = False
lib._pack_batch = None; lib._pack_cache.clear(); AssetCacher.clearCache(); FakePack.parses.clear()
lib._load_texture_packs(Mdl(), lambda m: None)
check(len(lib._pack_cache) == 5 and len(FakePack.parses) == 5, "11. setup: 5 packs parsed and cached")
lib._begin_export()
check(len(lib._pack_cache) == 5, "11. CLEAR_PACK_CACHE off: _begin_export keeps the cache")
assets.CLEAR_PACK_CACHE = True
lib._pack_batch = set()
lib._begin_export()
check(len(lib._pack_cache) == 5, "11. CLEAR_PACK_CACHE on, inside a weapon batch: the cache is left alone")
lib._pack_batch = None
lib._begin_export()
check(len(lib._pack_cache) == 0, "11. CLEAR_PACK_CACHE on, a new export: the cache is emptied")
FakePack.parses.clear(); AssetCacher.clearCache()
lib._load_texture_packs(Mdl(), lambda m: None)
check(len(FakePack.parses) == 5, "11. after the clear every pack is parsed again (cold)")
assets.CLEAR_PACK_CACHE = False
lib._begin_export(); FakePack.parses.clear(); AssetCacher.clearCache()
lib._load_texture_packs(Mdl(), lambda m: None)
check(not FakePack.parses, "11. flag off again: the next export re-parses nothing")

asrc = open(os.path.join(boot.REPO, "wtx", "assets.py")).read()
atree = ast.parse(asrc)
def stmt_before_begin(fname):
    f = next(n for n in ast.walk(atree) if isinstance(n, ast.FunctionDef) and n.name == fname)
    for i, st in enumerate(f.body):
        if isinstance(st, ast.Expr) and "self._begin_export()" in ast.get_source_segment(asrc, st):
            return ast.get_source_segment(asrc, f.body[i - 1])
    return None
check(stmt_before_begin("_export_obj_impl") == "log.subLevel(log.curLevel)" and stmt_before_begin("_export_gltf_impl") == "log.subLevel(log.curLevel)",
      "11. (source check) _export_obj_impl / _export_gltf_impl call _begin_export() right after log.subLevel(log.curLevel)")
check((stmt_before_begin("_export_skin_template_impl") or "").startswith("skin_dir.mkdir("), "11. (source check) _export_skin_template_impl calls it right after skin_dir.mkdir(...)")
check(stmt_before_begin("_open_model") is None and asrc.count("self._begin_export()") == 3, "11. (source check) no other call site")

# ---------------------------------------------------------------- 12. PACK_WORKERS = 1 (the default) is the sequential path
def load_with(workers):
    assets.PACK_WORKERS = workers
    FakePack.parses.clear(); lib._pack_cache.clear(); AssetCacher.clearCache()
    FakePack.delay = 0.02
    m = []
    lib._load_texture_packs(Mdl(), m.append)
    return m, list(FakePack.parses)
m1, p1 = load_with(1)
m0, p0 = load_with(0)
m4, p4 = load_with(4)
FakePack.delay = 0.0
check(len(p1) == 5 and not any(n.startswith("wtx-pack") for _, n in p1 + p0), "12. PACK_WORKERS 1 / 0: 5 packs parsed on the calling thread, no pack pool")
check(all(n.startswith("wtx-pack") for _, n in p4), "12. PACK_WORKERS 4 still uses the pool")
check(m1 == m0 == m4 and [x for x in m1 if x.strip().startswith("texture pack")][-1].strip().endswith("p_hq.dxp.bin"),
      "12. same messages and registration order (HQ pack last) for 0, 1 and 4 workers")
assets.PACK_WORKERS = 1

# ---------------------------------------------------------------- 13. profiling in phase_timing (PROFILE)
def _burn_helper_xyz():
    return sum(range(20000))
_ns = {}
exec("\n".join(f"def many_{i}(): return sum(range({5000 + i}))" for i in range(20)), _ns)

@phase_timing.phase("model: getModel (mesh build)")
def p_mesh():
    _burn_helper_xyz()
    for i in range(20): _ns[f"many_{i}"]()

@phase_timing.phase("animations")
def p_anim_outer():
    _burn_helper_xyz()
    p_mesh()                                                           # a profiled phase inside a profiled phase

@phase_timing.phase("weapons: placement + merge", weapons=True)
def p_weapons():
    p_mesh()                                                           # labelled "weapons > model: getModel (mesh build)"

@phase_timing.phase("camo bake")                                       # not in PROFILED
def p_other():
    _burn_helper_xyz()

@phase_timing.phase("animations")
def p_boom():
    _burn_helper_xyz()
    raise RuntimeError("boom")

def report_of(fn):
    out = []
    with phase_timing.session(out.append, "export_gltf  selftest"):
        fn()
    return out

phase_timing.PROFILE = False
off = report_of(lambda: (p_mesh(), p_anim_outer()))
check(not any("profile" in l for l in off) and not phase_timing._profiles, "13. PROFILE off: no profile output, nothing collected")

phase_timing.PROFILE = True
rep = report_of(lambda: (p_mesh(), p_mesh(), p_anim_outer(), p_weapons(), p_other()))
heads = [l for l in rep if l.startswith("[timing] --- ")]
check(any("model: getModel (mesh build):" in h for h in heads) and any("--- animations:" in h for h in heads) and any("weapons > model: getModel (mesh build):" in h for h in heads),
      "13. one profile per full label; the weapons > prefix keeps its own")
check(len(heads) == 3 and not any("camo bake" in h for h in heads), "13. only the PROFILED phases get a profile (3 labels here, 'camo bake' none)")
anim_i = next(i for i, l in enumerate(rep) if "--- animations:" in l)
anim_block = rep[anim_i + 1: next((i for i, l in enumerate(rep) if i > anim_i and l.startswith("[timing] --- ")), len(rep))]
check(any("_burn_helper_xyz" in l for l in anim_block) and any(".py" in l and "p_mesh" in l for l in anim_block),
      "13. nested: the inner profiled phase is not profiled on its own, but its functions are inside the outer profile")
mesh_i = next(i for i, l in enumerate(rep) if l.startswith("[timing] --- model: getModel (mesh build):"))
mesh_block = [l for l in rep[mesh_i + 1:] if not l.startswith("[timing] ---")][:40]
helper_rows = [l for l in mesh_block if "_burn_helper_xyz" in l]
check(helper_rows and helper_rows[0].split()[1] == "2", f"13. calls of one label accumulate (the helper ran twice: {helper_rows[0].split()[1] if helper_rows else '-'})")
rows_mesh = []
for l in rep[mesh_i + 1:]:
    if l.startswith("[timing] ---") or l.startswith("[timing] note"):
        break
    if re.match(r"\[timing\]\s+\d+(/\d+)?\s+\d+\.\d+\s", l):         # a pstats row: ncalls tottime ... (not the "N function calls" summary line)
        rows_mesh.append(l)
check(len(rows_mesh) == phase_timing.PROFILE_TOP, f"13. top {phase_timing.PROFILE_TOP} functions listed ({len(rows_mesh)} rows)")
check(any("inflated by the profiling overhead" in l for l in rep), "13. warning line: profiled phases are inflated by the profiling overhead")
check(rep.index(next(l for l in rep if l.startswith("[timing] ===== export_gltf"))) < rep.index(next(l for l in rep if "profile: top functions" in l)),
      "13. the profile comes after the timing table")
check(phase_timing._tls.__dict__.get("profiling", False) is False and sys.getprofile() is None, "13. nothing left switched on after the export")

rep2 = report_of(lambda: p_other())
check(any("nothing was profiled" in l for l in rep2) and not any("--- animations:" in l for l in rep2), "13. session() clears the profiles: nothing carried over from the previous export")

try:
    report_of(p_boom)
except RuntimeError:
    pass
check(phase_timing._tls.__dict__.get("profiling", False) is False and sys.getprofile() is None, "13. an exception inside a profiled phase still switches the profiler off")
rep3 = report_of(lambda: p_mesh())
check(any("--- model: getModel (mesh build):" in l for l in rep3), "13. profiling works again after that")

if sys.version_info >= (3, 12):
    import cProfile
    other = cProfile.Profile()
    other.enable()                                                     # somebody else (a debugger, cProfile -m ...) holds the one profiler slot
    try:
        err = None
        try:
            rep4 = report_of(lambda: p_anim_outer())
        except Exception as e:
            err = e
    finally:
        other.disable()
    check(err is None, f"13. another profiler already active: the export is not broken ({type(err).__name__ if err else 'no error'})")
    check(any("nothing was profiled" in l for l in rep4) and any(re.match(r"\[timing\]\s+animations\s", l) for l in rep4),
          "13. ... the phase is still timed in the table, the profile says nothing was collected")

    # a profiled phase on another thread while this one is profiling: enable() fails there, must be skipped quietly
    box = {}
    def other_thread():
        try:
            p_mesh()
            box["ok"] = True
        except Exception as e:
            box["ok"] = repr(e)
    @phase_timing.phase("animations")
    def p_with_thread():
        t = threading.Thread(target=other_thread, name="wtx-sidecar")
        t.start(); t.join()
        _burn_helper_xyz()
    rep5 = report_of(p_with_thread)
    check(box.get("ok") is True and any("--- animations:" in l for l in rep5), "13. a second thread entering a profiled phase meanwhile does not fail or disturb the profile")
else:
    check(True, "13. (skipped on Python < 3.12: only one-profiler-at-a-time semantics are tested there)")
phase_timing.PROFILE = False

# ---------------------------------------------------------------- 14. the [timing] settings line
material.TEXTURE_WORKERS, assets.PACK_WORKERS, assets.CLEAR_PACK_CACHE, phase_timing.PROFILE = 3, 2, True, False
gltf_conv.MAP_WORKERS = 6
r = report_of(lambda: p_other())
check(r[0] == "[timing] settings: texture workers 3, pack workers 2, glTF map workers 6, clear pack cache per export yes, profiling no", f"14. first report line: {r[0]}")
check(r[1].startswith("[timing] ===== export_gltf"), "14. the title line follows")
material.TEXTURE_WORKERS, assets.PACK_WORKERS, assets.CLEAR_PACK_CACHE, phase_timing.PROFILE = 0, 1, False, True
gltf_conv.MAP_WORKERS = 1
r = report_of(lambda: p_other())
check(r[0] == "[timing] settings: texture workers 0, pack workers 1, glTF map workers 1, clear pack cache per export no, profiling yes", f"14. built from the live values: {r[0]}")
tuning.apply({"tex_workers": 5, "pack_workers": 4, "map_workers": 2, "clear_pack_cache": True, "profile": False})
r = report_of(lambda: p_other())
check(r[0] == "[timing] settings: texture workers 5, pack workers 4, glTF map workers 2, clear pack cache per export yes, profiling no", "14. a tuning.apply() shows up in the very next report")
nested = []
with phase_timing.session(nested.append, "outer"):
    with phase_timing.session(nested.append, "inner (weapon model)"):
        p_other()
check(sum(1 for l in nested if l.startswith("[timing] settings:")) == 1, "14. a nested session (weapon models) prints no second settings line")

# (section 15 below restores the settings at the very end)
for k, v in ENV_SAVED.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v


# ---------------------------------------------------------------- 15. glTF map pre-pass (parse.gltf_conv.precompute_gltf_maps)
import hashlib, contextlib, io

def make_dds32(path, w, h, seed):
    """An uncompressed 32-bit BGRA DDS (the kind readDdsRgba reads, and the kind the texture stage writes)."""
    import random
    rnd = random.Random(seed)
    head = bytearray(128)
    head[0:4] = b"DDS "
    struct.pack_into("<I", head, 4, 124)
    struct.pack_into("<I", head, 8, 0x1007)
    struct.pack_into("<II", head, 12, h, w)
    struct.pack_into("<I", head, 76, 32)
    struct.pack_into("<I", head, 80, 0x41)
    struct.pack_into("<I", head, 88, 32)
    struct.pack_into("<IIII", head, 92, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    with open(path, "wb") as f:
        f.write(bytes(head) + bytes(rnd.getrandbits(8) for _ in range(w * h * 4)))

class FM:                                                             # a material, as far as the glTF loop looks at it
    def __init__(self, normal, cls="rendinst_simple"):
        self.normal, self.cls, self.textures = normal, cls, [None, None, normal]

def map_fixture(root):
    d = os.path.join(root, "textures"); os.makedirs(d)
    for i in range(7):
        make_dds32(os.path.join(d, f"part{i}_n.dds"), 96 + 8 * i, 64, seed=i)
    make_dds32(os.path.join(d, "glassy_n.dds"), 64, 64, seed=50)
    make_dds32(os.path.join(d, "flat_normal.dds"), 64, 64, seed=51)       # a normal map whose name does not end in _n: no metal-roughness picture
    open(os.path.join(d, "broken_n.dds"), "wb").write(b"not a dds at all")
    mats = [FM(f"part{i}_n") for i in range(7)] + [FM("part3_n"), FM("glassy_n", cls="glass_tex"), FM("flat_normal"), FM("broken_n"), FM(None)]
    return d, mats

class Mdl2:
    def __init__(self, mats): self.materials = mats

def loop_like(mats, d, tex_index):
    """What the material loop does with the maps (same helpers, same functions)."""
    for m in mats:
        stem = gltf_conv._normal_stem_of(m, gltf_conv._slot_list(m))
        if not stem:
            continue
        if gltf_conv.find_texture(tex_index, [stem + "_gltf"]) is None:
            src = gltf_conv._resolve_normal_src(tex_index, stem)
            if src:
                gltf_conv.make_gltf_normal(gltf_conv.Path(d) / src)
        raw_n = gltf_conv._mr_source(m, stem, tex_index)
        if raw_n:
            gltf_conv.make_gltf_mr(gltf_conv.Path(d) / raw_n)

def pngs(d):
    return {f: hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest() for f in sorted(os.listdir(d)) if f.endswith(".png")}

seen = []
real_normal, real_mr = gltf_conv.make_gltf_normal, gltf_conv.make_gltf_mr
def spy_n(p): seen.append(threading.current_thread().name); return real_normal(p)
def spy_m(p): seen.append(threading.current_thread().name); return real_mr(p)
gltf_conv.make_gltf_normal, gltf_conv.make_gltf_mr = spy_n, spy_m

# reference: no pool, then the loop alone
gltf_conv.MAP_WORKERS = 1
dA, matsA = map_fixture(os.path.join(TMP, "mapsA"))
idxA = gltf_conv.index_texture_dir(gltf_conv.Path(dA))
seen.clear()
n0 = gltf_conv.precompute_gltf_maps(Mdl2(matsA), gltf_conv.Path(dA), idxA)
check(n0 == 0 and not seen and not pngs(dA), "15. MAP_WORKERS 1: the pre-pass does nothing (old behaviour: no pool, no files)")
gltf_conv.MAP_WORKERS = 0
check(gltf_conv.precompute_gltf_maps(Mdl2(matsA), gltf_conv.Path(dA), idxA) == 0 and not seen, "15. MAP_WORKERS 0: same")
with contextlib.redirect_stdout(io.StringIO()):
    loop_like(matsA, dA, idxA)
ref = pngs(dA)
check(len(ref) == 9 + 7 and "part0_n_gltf.png" in ref and "part0_n_mr.png" in ref and "glassy_n_mr.png" not in ref and "flat_normal_mr.png" not in ref and "glassy_n_gltf.png" in ref,
      f"15. reference (sequential loop alone): {len(ref)} pictures; glass and non-_n materials get no metal-roughness picture")

# pool
gltf_conv.MAP_WORKERS = 4
dB, matsB = map_fixture(os.path.join(TMP, "mapsB"))
idxB = gltf_conv.index_texture_dir(gltf_conv.Path(dB))
seen.clear()
out = io.StringIO()
with contextlib.redirect_stdout(out):
    nsub = gltf_conv.precompute_gltf_maps(Mdl2(matsB), gltf_conv.Path(dB), idxB)
names_used = set(seen)
check(nsub == 7 + 1 + 1 + 1 + 7 + 1 and all(n.startswith("wtx-map") for n in names_used) and len(names_used) > 1,
      f"15. pool: {nsub} conversions submitted (shared normal map counted once), run on {len(names_used)} wtx-map threads")
got = pngs(dB)
check(all(f in got for f in ref), "15. after the pre-pass every picture the loop needs already exists")
check(got == ref,
      "15. the pictures written by the pool are byte-identical to the sequential ones (sha256)")
check("WARNING" not in out.getvalue(), "15. pool threads are muted: the failure of the broken texture printed nothing from a worker")
check("broken_n_mr.png" not in got and "broken_n_gltf.png" not in got, "15. the broken texture produced no picture (left to the loop)")
seen.clear()
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    loop_like(matsB, dB, idxB)
check(pngs(dB) == got and "gloss / metal map of broken_n.dds" in out2.getvalue(), "15. the loop afterwards changes no picture and still reports the broken one itself (main thread)")
check(set(seen) == {threading.current_thread().name}, "15. the loop's own calls run on the calling thread")

mt = {f: os.path.getmtime(os.path.join(dB, f)) for f in got}
seen.clear()
with contextlib.redirect_stdout(io.StringIO()):
    again = gltf_conv.precompute_gltf_maps(Mdl2(matsB), gltf_conv.Path(dB), gltf_conv.index_texture_dir(gltf_conv.Path(dB)))
check({f: os.path.getmtime(os.path.join(dB, f)) for f in got} == mt, f"15. a repeat run rewrites nothing ({again} cheap calls, all kept)")

# a model without anything to convert / one conversion only: no pool
gltf_conv.MAP_WORKERS = 4
seen.clear()
check(gltf_conv.precompute_gltf_maps(Mdl2([FM(None)]), gltf_conv.Path(dB), idxB) == 0 and gltf_conv.precompute_gltf_maps(Mdl2([]), gltf_conv.Path(dB), idxB) == 0 and not seen,
      "15. nothing to do: no pool")
gltf_conv.make_gltf_normal, gltf_conv.make_gltf_mr = real_normal, real_mr

# phase_timing: the pre-pass is timed on the main thread, the conversions on the workers
dC, matsC = map_fixture(os.path.join(TMP, "mapsC"))
idxC = gltf_conv.index_texture_dir(gltf_conv.Path(dC))
lines = []
with phase_timing.session(lines.append, "export_gltf  maps"):
    with contextlib.redirect_stdout(io.StringIO()):
        gltf_conv.precompute_gltf_maps(Mdl2(matsC), gltf_conv.Path(dC), idxC)
check(any(re.match(r"\[timing\]\s+glTF: maps \(plan \+ wait for pool\)\s", l) for l in lines)
      and any("glTF: normal maps" in l for l in lines[lines.index(next(l for l in lines if "on worker threads" in l)):]),
      "15. report: 'glTF: maps (plan + wait for pool)' on the main thread, the normal / metal-roughness work under worker threads")

# source checks on the real material loop
gsrc2 = open(os.path.join(boot.REPO, "dae_core", "parse", "gltf_conv.py")).read()
fn_src = ast.get_source_segment(gsrc2, next(n for n in ast.parse(gsrc2).body if isinstance(n, ast.FunctionDef) and n.name == "export_model_to_gltf"))
check(fn_src.index("precompute_gltf_maps(") < fn_src.index("for idx, mat in enumerate(") and all(h in fn_src for h in ("_normal_stem_of(", "_resolve_normal_src(", "_mr_source(")),
      "15. (source check) export_model_to_gltf runs the pre-pass before the material loop, and the loop uses the same helpers")

material.TEXTURE_WORKERS, assets.PACK_WORKERS, assets.CLEAR_PACK_CACHE, phase_timing.PROFILE, gltf_conv.MAP_WORKERS = SAVED
for k, v in ENV_SAVED.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{sum(OK)}/{len(OK)} checks passed")
sys.exit(0 if all(OK) else 1)
