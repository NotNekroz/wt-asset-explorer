"""
Differential test for the WTX_FAST_MATH paths (parse/mesh.py vertex + index decoding, parse/realres.py vertex scaling and face
copy, parse/a2d_anim.py track maths and single ACL decode).

Every fast path is run next to the original loop it replaces, on the same input, and the results are compared with repr()
(so -0.0, NaN, int-vs-float and the type and text of an exception all count). It also checks that the fast path is really
taken (and not silently falling back) wherever it should be.

Run from the repo folder:   uv run tests/test_fast_decode.py        (or: python tests/test_fast_decode.py)
It needs no game files: the inputs are synthetic. It does NOT test real exports; see the end of the output.
"""
import os, sys, types, struct, random, tempfile, json, math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "dae_core"))

try:
    import zstandard  # noqa: F401
except ImportError:                      # only needed so the repo's modules import
    _z = types.ModuleType("zstandard"); _z.ZstdDecompressor = object; sys.modules["zstandard"] = _z

import warnings
warnings.simplefilter("ignore", RuntimeWarning)   # the original loops overflow / divide by zero on the edge patterns on purpose
import numpy as np
import util.log as log
log.setMuted(True)
import parse.mesh as mesh
import parse.realres as rr
import parse.a2d_anim as aa
from util.fileread import BinFile

VD = mesh.MatVData.VertexData
rng = random.Random(1234)
nprng = np.random.default_rng(1234)

passed = failed = 0
def check(ok, what):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print("FAIL:", what)

def both(fn, may_raise=False, what=""):
    """run fn() with the fast paths off and on; (old, new) as repr text. An exception is a result too, but an unexpected one fails."""
    out = []
    for mode in (False, True):
        mesh.FAST_DECODE = mode
        aa.FAST_MATH = mode
        try:
            out.append(repr(fn()))
        except Exception as e:
            if not may_raise:
                check(False, f"{what}: unexpected exception with fast={mode}: {type(e).__name__}: {e}")
            out.append(f"EXC {type(e).__name__}: {e}")
    mesh.FAST_DECODE = True
    aa.FAST_MATH = True
    return out

# ------------------------------------------------------------------------------------------------ vertices / faces
class GV:
    def __init__(self, fmt, stride, vcnt, psz=0, isz=0):
        self.idx, self.f, self.s, self.n, self.p, self.i = 0, fmt, stride, vcnt, psz, isz
    def getStorageFormat(self): return self.f
    def getVertexStride(self): return self.s
    def getVertexCnt(self): return self.n
    def getVertexBlockSz(self): return self.n * self.s
    def getPackedIndicesSz(self): return self.p
    def getIndicesSz(self): return self.i

def new_vd(gv):
    vd = object.__new__(VD)
    vd._VertexData__gvData = gv
    vd._VertexData__unimplemented = False
    vd._VertexData__autoRaw = None
    vd._VertexData__faces = None
    return vd

def block(data):
    return BinFile(bytes(data)).readBlock(len(data))

def row_size(parser):
    sz = {VD.SHORT_VERTEX: 6, VD.FLOAT_VERTEX: 12, VD.NO_UV: 0, VD.SHORT_UV: 4, VD.FLOAT_UV: 8, VD.DUAL_SHORT_UV: 8, VD.HALFFLOAT_UV: 4}
    return sum(c.sz if isinstance(c, VD.PADDING) else sz[type(c)] for c in parser)

def vertex_bytes(kind, n):
    if kind == "random":
        return bytes(rng.randrange(256) for _ in range(n))
    if kind == "zeros":
        return bytes(n)
    if kind == "ones":
        return b"\xff" * n
    if kind == "edge":      # int16 / float32 edge patterns: -32768, 32767, 0, -0.0, nan, inf, tiny
        pats = [b"\x00\x80", b"\xff\x7f", b"\x00\x00", b"\x00\x00\x00\x80", b"\x00\x00\xc0\x7f", b"\x00\x00\x80\x7f", b"\x01\x00\x00\x00",
                b"\x00\x00\x80\xff", b"\x00\x00\x80\x3f", b"\xff\xff", b"\x01\x80"]
        out = bytearray()
        while len(out) < n:
            out += rng.choice(pats)
        return bytes(out[:n])

print("vertex layouts ...")
layouts = 0
for fmt, strides in VD.FORMATS.items():
    for stride, parser in strides.items():
        row = row_size(parser)
        check(row == stride, f"layout {fmt}/{stride}: field sizes add up to {row}, not the stride")
        for vcnt in (1, 2, 3, 17, 500):
            for kind in ("random", "zeros", "ones", "edge"):
                data = vertex_bytes(kind, vcnt * stride)
                def run():
                    vd = new_vd(GV(fmt, stride, vcnt)); vd.__processVertices__(block(data))
                    return vd.getVertices(), vd.getUVs(), vd._VertexData__UVs1
                old, new = both(run, what=f"vertices {fmt}/{stride} n={vcnt} {kind}")
                check(old == new, f"vertices {fmt}/{stride} n={vcnt} {kind} differ")
                vd = new_vd(GV(fmt, stride, vcnt))
                mesh.FAST_DECODE = True
                check(vd.__fastVertices__(block(data), parser, vcnt) is not None, f"vertices {fmt}/{stride}: fast path not taken")
                layouts += 1
        # short blocks: both must fail the same way
        for cut in (1, stride, stride * 3 - 1):
            data = vertex_bytes("random", stride * 3)[:cut]
            def run():
                vd = new_vd(GV(fmt, stride, 3)); vd.__processVertices__(block(data)); return vd.getVertices()
            old, new = both(run, may_raise=True)
            check(old == new, f"short vertex block {fmt}/{stride} cut={cut}: {old} / {new}")
print(f"  {layouts} layout/size/pattern combinations")

# unknown layout and terminate flag: must fall back
def run():
    vd = new_vd(GV(99, 12, 2)); vd.__processVertices__(block(bytes(24))); return 1
old, new = both(run, may_raise=True)
check(old == new and old.startswith("EXC"), "unknown storage format")

def leb(v):
    out = bytearray()
    while True:
        b = v & 0x7f; v >>= 7
        if v: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def faces_of(data_vertex_free, psz, isz):
    def run():
        vd = new_vd(GV(1, 12, 0, psz, isz)); vd.__processFaces__(block(data_vertex_free)); return vd.getFaces()
    return run

print("unpacked faces ...")
for count in (1, 2, 3, 10, 1000, 4096):
    sz = count * 6 + (count % 7 == 0) * 0
    # the loop uses count = sz // 6 - sz % 6: pick sz so that it gives `count` triangles
    sz = count * 6
    data = bytes(rng.randrange(256) for _ in range(sz + 12))
    old, new = both(faces_of(data, 0, sz), what=f"unpacked {count}")
    check(old == new, f"unpacked faces count={count} differ")
for sz in (0, 5, 6, 7, 11, 12, 13, 35, 36, 37):
    data = bytes(rng.randrange(256) for _ in range(sz))
    old, new = both(faces_of(data, 0, sz), may_raise=True)
    check(old == new, f"unpacked faces sz={sz}: {old[:60]} / {new[:60]}")
# short data: asks for more triangles than the block holds
old, new = both(faces_of(bytes(30), 0, 60), may_raise=True)
check(old == new and old.startswith("EXC"), "unpacked faces, short block")
vd = new_vd(GV(1, 12, 0)); check(vd.__fastUnpackedFaces__(block(bytes(60)), 10) is not None, "unpacked fast path not taken")

print("packed faces ...")
def packed_run(encoded, tail=b"", psz_delta=0):
    data = b"\x00" + encoded + tail
    def run():
        vd = new_vd(GV(1, 12, 0, len(encoded) + 5 + psz_delta, 0)); vd.__processFaces__(block(data)); return vd.getFaces()
    return run

def make_numbers(n, bits):
    return [rng.randrange(1 << bits) for _ in range(n)]

cases = 0
for n in (0, 1, 2, 3, 4, 5, 6, 7, 30, 31, 32, 300, 3001, 60000):
    for bits in (5, 7, 8, 14, 21, 28, 30, 35):
        enc = b"".join(leb(v) for v in make_numbers(n, bits))
        old, new = both(packed_run(enc, b"\x01\x02"), may_raise=True, what=f"packed n={n} bits={bits}")
        check(old == new, f"packed n={n} bits={bits}: {old[:70]} / {new[:70]}")
        cases += 1
        if n >= 3 and enc:
            vd = new_vd(GV(1, 12, 0))
            check(vd.__fastPackedFaces__(enc, len(enc)) is not None, f"packed fast path not taken n={n} bits={bits}")
# mixed widths, junk, truncated, runs longer than 5 bytes, unfinished last number
for _ in range(400):
    n = rng.randrange(0, 200)
    enc = b"".join(leb(rng.randrange(1 << rng.choice((3, 7, 12, 20, 30, 35)))) for _ in range(n))
    if rng.random() < 0.3 and enc: enc = enc[:rng.randrange(len(enc))]
    if rng.random() < 0.2: enc += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 9)))
    old, new = both(packed_run(enc), may_raise=True)
    check(old == new, f"packed mixed/junk: {old[:70]} / {new[:70]}")
    cases += 1
for pat in (b"\x80", b"\xff" * 8, b"\x80\x80\x80\x80\x80\x80\x01", b"\x81\x01" * 7, b"\x81\x80\x80\x80\x80\x01" * 3, b"\x05\x81\x80\x80\x80\x80\x01\x07\x08\x09" * 2, b"\x00", b"\x00\x00\x00", b"\xff\xff\xff\xff\x7f" * 3, b"\x01\x00\x00\x00\x00"):
    old, new = both(packed_run(pat), may_raise=True)
    check(old == new, f"packed pattern {pat!r}: {old[:70]} / {new[:70]}")
    cases += 1
for delta in (-3, -1, 1, 3):          # packed size field disagrees with the data that is there
    enc = b"".join(leb(v) for v in make_numbers(30, 14))
    old, new = both(packed_run(enc, b"\x00" * 3, delta), may_raise=True)
    check(old == new, f"packed size delta {delta}: {old[:70]} / {new[:70]}")
    cases += 1
print(f"  {cases} packed streams")

# autoLayout path (format 17, stride 32): the check reads the faces and the raw bytes, both modes must end in the same vertices
print("format 17/32 auto layout ...")
for vcnt in (40, 300):
    raw = bytearray()
    for i in range(vcnt):
        raw += bytes(4) + bytes(4) + struct.pack("<hhh", int(3000 * math.sin(i / 7)), int(3000 * math.cos(i / 9)), i * 20) + bytes(14) + struct.pack("<hh", i * 10, i * 5)
    raw = bytes(raw)[:vcnt * 32].ljust(vcnt * 32, b"\0")
    tris = [(i, i + 1, i + 2) for i in range(vcnt - 2)]
    ib = b"".join(struct.pack("<HHH", *t) for t in tris)
    data = raw + ib
    def run():
        blk = block(data); vb = blk.readBlock(vcnt * 32)
        vd = new_vd(GV(17, 32, vcnt, 0, len(ib)))
        vd.__processVertices__(vb); vd.__processFaces__(blk)
        return vd.getVertices(), vd.getUVs(), vd.getFaces()
    old, new = both(run, may_raise=True)
    check(old == new and not old.startswith("EXC"), f"format 17/32 vcnt={vcnt}: {old[:80]} / {new[:80]}")
    check(new_vd(GV(17, 32, vcnt)).__fastVertices__(block(data).readBlock(vcnt * 32), VD.FORMATS[17][32], vcnt) is not None, "17/32 fast path not taken")

# ------------------------------------------------------------------------------------------------ realres
print("vertex scaling ...")
def rand_verts(n):
    out = []
    for _ in range(n):
        out.append([rng.choice((rng.uniform(-3, 3), 0.0, -0.0, 1e-310, 1e300, float("nan"), float("inf"), -1.0)) for _ in range(3)])
    return out
f32 = np.float32
scales = {"float": ((1.5, -0.25, 3.0), (0.1, 0.2, 0.3, 0.0)), "default": (rr.Model.DEFAULT_SCALE, rr.Model.DEFAULT_OFFSET),
          "int": ((2, 3, 4), (1, 2, 3, 0)), "mixed": ((2, 0.5, -1), (0, 0.25, 7, 0)),
          "float32 scale": ((f32(1.5), f32(2.0), f32(0.1)), (0.1, 0.2, 0.3, 0.0)),
          "float32 offset": ((1.5, 2.0, 0.1), (f32(0.1), f32(0.2), f32(0.3), f32(0.0))),
          "tiny": ((1e-5, 1e-5, 1e-5), (1e-3, 0, -1e-3, 0))}
for name, (s, o) in scales.items():
    for n in (1, 5, 1000):
        verts = rand_verts(n)
        uvs = [(0.0, 1.0)] * n
        def run():
            m = rr.Model("t", vertScale=s, vertOffet=o)
            m.appendVerts([list(v) for v in verts], uvs)
            return m._Model__vertices, m._Model__uvs
        old, new = both(run, what=f"scale {name}")
        check(old == new, f"appendVerts {name} n={n} differ")
    m = rr.Model("t", vertScale=s, vertOffet=o)
    taken = m._Model__fastScale([[0.5, 0.5, 0.5]]) is not None
    want = name not in ("float32 scale", "float32 offset")
    check(taken == want, f"scale '{name}': fast path taken={taken}, expected {want}")
m = rr.Model("t", vertScale=(1.0, 1.0, 1.0), vertOffet=(0.0, 0.0, 0.0, 0.0))
for bad in ([], tuple([[1.0, 2.0, 3.0]]), [[1.0, 2.0]], [None]):
    check(m._Model__fastScale(bad) is None, f"fastScale accepted {bad!r}")
def run():
    mm = rr.Model("t"); mm.appendVerts([None], [(0, 0)]); return 1
old, new = both(run, may_raise=True)
check(old == new, "appendVerts([None]) must fail the same way")
# unscaled path untouched
def run():
    mm = rr.Model("t", vertScale=(2.0, 2.0, 2.0)); mm.appendVerts([(1.0, 1.0, 1.0)], [(0, 0)], scale=False); return mm._Model__vertices
old, new = both(run); check(old == new, "scale=False")

print("face copy in _computeMvdShaderMesh ...")
class FakeVertexData:
    def __init__(self, nv, faces): self.v = [[float(i), 0.0, 0.0] for i in range(nv)]; self.u = [(0.0, 0.0)] * nv; self.f = faces
    def getVertices(self): return self.v
    def getUVs(self): return self.u
    def getFaces(self): return self.f
class FakeMvd:
    def __init__(self, vds): self.vds = vds
    def getVertexData(self, i): return self.vds[i]
Elem = lambda **k: types.SimpleNamespace(**k)
me = types.SimpleNamespace(shouldTerminate=False)
def build_case(overrun=False, order=False):
    vds = [FakeVertexData(50, tuple((rng.randrange(50), rng.randrange(50), rng.randrange(50)) for _ in range(200))) for _ in range(3)]
    elems = []
    for _ in range(12):
        vdi = rng.randrange(3); start = rng.randrange(0, 150) * 3; num = rng.randrange(0, 40)
        if overrun and _ == 7: num = 400
        elems.append(Elem(vdOrderIndex=rng.randrange(3) if order else 0, vData=vdi, baseVertex=rng.randrange(0, 100), startI=start, numFace=num, mat=rng.randrange(4)))
    return vds, elems
for case in range(40):
    state = rng.getstate()
    overrun, order = case % 5 == 4, case % 2 == 1
    def run():
        rng.setstate(state)
        vds, elems = build_case(overrun, order)
        mdl = rr.Model("t"); obj = mdl.newObject("o")
        vdata = [None] * 3
        ret = rr.RendInst._computeMvdShaderMesh(me, FakeMvd(vds), elems, mdl, obj, vdata, {})
        return ret, obj.faces, obj.materials, obj.material_ids
    def run_safe():
        rng.setstate(state)
        vds, elems = build_case(overrun, order)
        mdl = rr.Model("t"); obj = mdl.newObject("o"); vdata = [None] * 3
        try:
            rr.RendInst._computeMvdShaderMesh(me, FakeMvd(vds), elems, mdl, obj, vdata, {})
            r = "ok"
        except Exception as e:
            r = f"EXC {type(e).__name__}"
        return r, obj.faces, obj.materials
    old, new = both(run_safe, may_raise=True)
    check(old == new, f"face copy case {case} (overrun={overrun}, order={order}) differ")
    if overrun: check("EXC IndexError" in old, "overrun case did not raise IndexError in the original")
rng.setstate(random.Random(99).getstate())

# ------------------------------------------------------------------------------------------------ animation
print("animation maths ...")
def rand_quats(n, kind):
    if kind == "walk":
        q = nprng.normal(size=4); out = []
        for _ in range(n):
            q = q + nprng.normal(size=4) * 0.15; q /= np.linalg.norm(q)
            out.append(q * (-1 if nprng.random() < 0.3 else 1))     # sign flips like real keys
        return np.array(out, np.float32)
    if kind == "random":
        q = nprng.normal(size=(n, 4)); return (q / np.linalg.norm(q, axis=1)[:, None]).astype(np.float32)
    if kind == "near180":
        ax = nprng.normal(size=(n, 3)); ax /= np.linalg.norm(ax, axis=1)[:, None]
        ang = np.pi - np.abs(nprng.normal(size=n)) * 1e-3
        return np.concatenate([ax * np.sin(ang / 2)[:, None], np.cos(ang / 2)[:, None]], 1).astype(np.float32)
    if kind == "axes":      # exact 90 / 180 degree turns about the axes
        base = [(0, 0, 0, 1), (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (.7071068, 0, 0, .7071068), (0, .7071068, 0, .7071068)]
        return np.array([base[rng.randrange(len(base))] for _ in range(n)], np.float32)
    if kind == "ident":
        return np.tile(np.array([0, 0, 0, 1], np.float32), (n, 1))
def rand_scales(n, kind):
    if kind == "one": return np.ones((n, 3), np.float32)
    if kind == "mirror": return np.tile(np.array([1, 1, -1], np.float32), (n, 1))
    if kind == "mixed": return (nprng.normal(size=(n, 3)) * 2).astype(np.float32)
    if kind == "zero": return np.zeros((n, 3), np.float32)
    if kind == "tiny": return np.full((n, 3), 1e-7, np.float32)
    if kind == "anis": return np.abs(nprng.normal(size=(n, 3)) * np.array([1, 5, .01])).astype(np.float32)

worst64 = 0.0; ndiff32 = 0; nvals = 0; tracks = 0; fast_taken = 0
for qk in ("walk", "random", "near180", "axes", "ident"):
    for sk in ("one", "mirror", "mixed", "zero", "tiny", "anis"):
        for n in (1, 2, 3, 40, 300):
            Q, S = rand_quats(n, qk), rand_scales(n, sk)
            aa.FAST_MATH = False; q0, s0 = aa._track_trs(Q, S, n)
            aa.FAST_MATH = True; q1, s1 = aa._track_trs(Q, S, n)
            fb = aa._one_hemisphere(aa._decompose_batch(Q, S)[0]) is None     # True when the zero-dot fallback was used
            fast_taken += not fb; tracks += 1
            check(q0.shape == q1.shape and s0.shape == s1.shape and q0.dtype == q1.dtype and s0.dtype == s1.dtype, f"anim shapes/dtypes {qk}/{sk}/{n}")
            for a, b in ((q0, q1), (s0, s1)):
                with np.errstate(all="ignore"):
                    finite = np.isfinite(a) & np.isfinite(b)
                    worst64 = max(worst64, float(np.abs(a[finite] - b[finite]).max(initial=0.0)))
                    d32 = (a.astype("<f4").tobytes() != b.astype("<f4").tobytes())
                if d32:
                    ndiff32 += int((a.astype("<f4") != b.astype("<f4")).sum())
                    check(False, f"anim values differ after float32 rounding: {qk}/{sk}/{n}")
                nvals += a.size
check(fast_taken > 0.8 * tracks, f"fast animation path taken only {fast_taken}/{tracks} times")
# neighbours exactly perpendicular: the hemisphere shortcut must step aside, and the answer must stay the old one
Q = np.array([(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)] * 5, np.float32); S = np.ones((20, 3), np.float32)
check(aa._one_hemisphere(aa._decompose_batch(Q, S)[0]) is None, "zero-dot case should fall back")
aa.FAST_MATH = False; a = aa._track_trs(Q, S, 20); aa.FAST_MATH = True; b = aa._track_trs(Q, S, 20)
check(np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1]), "zero-dot fallback result differs")
# cache
cache = {}; aa.FAST_MATH = True
r1 = aa._track_trs(rand_quats(10, "walk"), rand_scales(10, "mixed"), 10, cache, (3, 0, 9)); r2 = aa._track_trs(None, None, 10, cache, (3, 0, 9))
check(r1 is r2, "cache not used")
print(f"  {tracks} tracks, {nvals} values; fast path taken {fast_taken}x; values differing after float32 rounding: {ndiff32}; "
      f"largest float64 gap {worst64:.2e}")

print("whole add_a2d_to_glb with a fake ACL decoder ...")
NB, NF = 14, 60
names = [f"bone{i}" for i in range(NB)] + ["gear_l1", "gear_r1", "flap_l", "hook_catch", "not_in_glb"]
_FAKE = None
def fake_decode_factory(counter):
    global _FAKE
    if _FAKE is None:                   # built once, so both runs decode exactly the same data
        r = np.random.default_rng(7)
        nt = len(names)
        rot = np.stack([rand_quats(NF, "walk") for _ in range(nt)], 1)
        tr = (r.normal(size=(NF, nt, 3)) * 0.1).astype(np.float32)
        sc = np.ones((NF, nt, 3), np.float32); sc[:, 3] = (1, 1, -1)
        for t in (2, 5, 9):             # some tracks do not move at all
            rot[:, t] = rot[0, t]; tr[:, t] = tr[0, t]
        sc[:, 6] *= np.linspace(1, 1.5, NF, dtype=np.float32)[:, None]
        _FAKE = (rot, tr, sc)
    def fake(blob):
        counter[0] += 1
        rot, tr, sc = (a.copy() for a in _FAKE)
        return {"rot": rot, "trans": tr, "scale": sc, "default": np.zeros((NF, len(names), 3), bool), "rate": 30.0, "num_tracks": len(names), "num_samples": NF}
    return fake
graph = {"bindings": [{"param": "p_gear_1", "nodeRE": "^gear_[lr]\\d*$"}, {"param": "p_flaps_rel", "nodeRE": "^flap"},
                      {"param": "p_hook", "node": "hook_catch"}, {"param": "p_bone", "nodeRE": "^bone[0-4]$"}]}
def run_glb(mode, with_graph, nres):
    aa.FAST_MATH = mode
    counter = [0]
    saved = aa.acl_decode, aa.unwrap, aa.parse_container
    aa.acl_decode = fake_decode_factory(counter)
    aa.unwrap = lambda raw: raw
    aa.parse_container = lambda raw: aa.A2d(3 << 8, list(names), [], b"")
    try:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.glb")
            js = {"asset": {"version": "2.0"}, "nodes": [{"name": n} for n in names[:-1]], "buffers": [{"byteLength": 0}]}
            aa.write_glb(p, js, bytearray())
            n = aa.add_a2d_to_glb(p, [(f"anim{i}", b"x") for i in range(nres)], lambda s: None, graph=graph if with_graph else None)
            return n, open(p, "rb").read(), counter[0]
    finally:
        aa.acl_decode, aa.unwrap, aa.parse_container = saved
        aa.FAST_MATH = True
for with_graph in (False, True):
    for nres in (1, 2):
        n0, b0, c0 = run_glb(False, with_graph, nres); n1, b1, c1 = run_glb(True, with_graph, nres)
        check(n0 == n1 and n0 > 0, f"glb animations added {n0} vs {n1}")
        same = b0 == b1
        if not same:        # byte differences are allowed only at float32-rounding level; report them instead of hiding them
            js0 = json.loads(b0[20:20 + struct.unpack_from('<I', b0, 12)[0]]); js1 = json.loads(b1[20:20 + struct.unpack_from('<I', b1, 12)[0]])
            check(js0 == js1, "glb JSON differs")
            o0 = 20 + struct.unpack_from('<I', b0, 12)[0] + 8; o1 = 20 + struct.unpack_from('<I', b1, 12)[0] + 8
            x0 = np.frombuffer(b0[o0:], "<f4"); x1 = np.frombuffer(b1[o1:], "<f4")
            print(f"  note: glb binary chunks differ in {int((x0 != x1).sum())} of {x0.size} float32 values (graph={with_graph}, a2d count={nres})")
            check(False, "glb bytes not identical")
        want_c1 = nres
        check(c1 == want_c1, f"ACL decode ran {c1} times with fast math, expected {want_c1}")
        check(c0 == (2 * nres if with_graph and aa.KEEP_FULL_CLIP else nres), f"original ACL decode count {c0}")
        print(f"  graph={with_graph!s:5} a2d={nres}: animations {n1}, identical bytes: {same}, ACL decodes {c0} -> {c1}")

# ------------------------------------------------------------------------------------------------ OBJ writer
print("OBJ writer (Model.getOBJ) ...")
class FakeNode:
    def __init__(self, wtm): self.wtm = wtm
class FakeSkeleton:
    def __init__(self, nodes): self.nodes = nodes
    def getNodeByName(self, name): return self.nodes.get(name)

def rand_wtm(kind):
    if kind == "identity": return [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    if kind == "int": return [[1, 0, 0, 5], [0, 0, 1, -2], [0, 1, 0, 0], [0, 0, 0, 1]]
    if kind == "negzero": return [[-0.0, 1.0, 0.0, -0.0], [1.0, -0.0, 0.0, 0.0], [0.0, 0.0, -1.0, -0.0], [0, 0, 0, 1.0]]
    if kind == "huge": return [[1e300, 1e-300, 0.0, 1e308], [0.0, 1e200, 1e200, 0.0], [3.0, 1e308, 1e308, 1e308], [0, 0, 0, 1.0]]
    return [[rng.uniform(-3, 3) for _ in range(4)] for _ in range(3)] + [[0.0, 0.0, 0.0, 1.0]]

def build_obj_model(seed, nverts, nobjs, with_skel=True, edge=False, wtm_kinds=("random", "identity", "int", "negzero", "huge")):
    r = random.Random(seed)
    verts = [[r.uniform(-5, 5) for _ in range(3)] for _ in range(nverts)]
    if edge:
        for v in verts:
            for k in range(3):
                if r.random() < 0.15: v[k] = r.choice((0.0, -0.0, 1e300, -1e300, 1e-310, float("nan"), float("inf")))
    uvs = [(r.random(), r.uniform(-2, 3)) for _ in range(nverts)]
    nodes = {}
    m = rr.Model("t", skeleton=FakeSkeleton(nodes) if with_skel else None)
    m.appendVerts(verts, uvs, scale=False)
    for k in range(nobjs):
        name = f"obj{k}"
        o = m.newObject(name, r.random() < 0.3)
        nf = r.choice([0, 1, 2, 5, 40, 200])
        o.setFaces([tuple(r.randrange(nverts) for _ in range(3)) for _ in range(nf)])      # repeated ids inside a face happen
        for _ in range(r.randrange(0, 4)):
            o.appendMaterial(f"mat{r.randrange(5)}", startFaceIdx=r.randrange(0, nf + 3))   # also keys past the last face
        if r.random() < 0.7:
            nodes[name] = FakeNode(rand_wtm(r.choice(wtm_kinds)))
    return m

obj_cases = 0
for seed in range(60):
    for edge in (False, True):
        m = build_obj_model(seed, rng.choice((4, 30, 400)), rng.choice((1, 2, 5, 9)), with_skel=seed % 5 != 0, edge=edge)
        old, new = both(lambda: m.getOBJ(), what=f"OBJ seed={seed} edge={edge}")
        check(old == new, f"getOBJ differs (seed={seed}, edge={edge})")
        obj_cases += 1
print(f"  {obj_cases} models, each text compared whole")

m = build_obj_model(7, 400, 5)
check(m._Model__fastObj() is not None, "OBJ fast path not taken on an ordinary model")
# unusual input: the fast path must step aside (None) and the loop must give the same answer, errors included
def odd(make, what):
    m = make()
    check(m._Model__fastObj() is None, f"OBJ fast path should step aside: {what}")
    old, new = both(lambda: m.getOBJ(), may_raise=True)
    check(old == new, f"OBJ {what}: {old[:60]} / {new[:60]}")
def m_bad_id():
    m = build_obj_model(3, 30, 2); m.newObject("bad").setFaces([(0, 1, 9999)]); return m
def m_neg_id():
    m = build_obj_model(3, 30, 2); m.newObject("neg").setFaces([(0, 1, -1)]); return m
def m_float_faces():
    m = build_obj_model(3, 30, 1); m.newObject("fl").setFaces([(0.0, 1.0, 2.0)]); return m
def m_f32_matrix():
    m = build_obj_model(3, 30, 2); m.newObject("f32").setFaces([(0, 1, 2)])
    m.skeleton.nodes["f32"] = FakeNode(np.eye(4, dtype=np.float32)); return m
def m_no_objects():
    return rr.Model("t")
def m_short_uvs():
    m = rr.Model("t"); m.appendVerts([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], [(0.0, 0.0)], scale=False)
    m.newObject("o").setFaces([(0, 1, 2)]); return m
for fn, what in ((m_bad_id, "id past the vertices"), (m_neg_id, "negative id"), (m_float_faces, "float ids"),
                 (m_f32_matrix, "float32 matrix"), (m_no_objects, "no objects"), (m_short_uvs, "fewer UVs than vertices")):
    odd(fn, what)
# lists instead of tuples, as other loaders may produce
m = build_obj_model(11, 50, 3)
for o in m:
    o.setFaces([list(f) for f in o.faces])
old, new = both(lambda: m.getOBJ(), what="OBJ list faces"); check(old == new, "OBJ faces as lists"); check(m._Model__fastObj() is not None, "OBJ list faces fast path")

print()
print(f"{passed} checks passed, {failed} failed")
print("Synthetic inputs only. Not covered by this file: real War Thunder data, the GUI, speed.")
sys.exit(1 if failed else 0)
