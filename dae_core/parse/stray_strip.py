"""
stray_strip.py - remove tiny dummy meshes ("dots") from an exported .obj or .glb.

Game models carry a few-triangle dummy mesh that is only a point in 3D (a centimetre or less across) but still has
UVs: the F-16C's 'nozzle1_91' (5 triangles at the nose, UV island over a tall strip of the sheet) and 'optic1_gun' /
'optic1_turret' (2 triangles each, the targeting-pod gimbal markers). They render as nothing in game, yet in
Substance Painter / Blender their UV islands lie on top of real parts (nose, pods, cockpit interior), so painting
one paints the other.

A face group (object + material) is dropped when it has at most MAX_FACES faces AND its biggest 3D extent is below
MAX_SIZE model units (metres). On the F-16C the smallest real part is 5 cm, the dots are 0.6 - 1.1 cm, so 2 cm
separates them cleanly. Every removal is reported.
OBJ: face lines are removed, vertex lines stay so the remaining face indices stay valid.
GLB: the dummy's mesh is detached from its node, the node itself stays (same technique as fx_strip.strip_glb), so
     the node tree, names and animation channels are untouched. The size is measured in world space (node
     transforms applied), so it is the same figure the OBJ export shows.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Callable

Say = Callable[[str], None]

MAX_FACES = 12
MAX_SIZE = 0.02


def _idx(tok: str):
    a = tok.split("/")
    v = int(a[0])
    vt = int(a[1]) if len(a) > 1 and a[1] else 0
    return v, vt


def strip_stray_obj(path, say: Say = print, max_faces: int = MAX_FACES, max_size: float = MAX_SIZE) -> int:
    """Remove tiny dummy meshes from a .obj in place. Returns the number of face lines dropped."""
    p = Path(path)
    raw = p.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8", "replace").splitlines()

    V, VT = [], []
    blocks: dict = {}                       # (object, material) -> [line numbers of its faces]
    obj = mat = ""
    for n, line in enumerate(lines):
        if line.startswith("v "):
            x = line.split()
            V.append((float(x[1]), float(x[2]), float(x[3])))
        elif line.startswith("vt "):
            x = line.split()
            VT.append((float(x[1]), float(x[2])))
        elif line.startswith("f "):
            blocks.setdefault((obj, mat), []).append(n)
        elif line.startswith(("g ", "o ")):
            obj = line[2:].strip()
        elif line.startswith("usemtl"):
            mat = line[6:].strip()

    drop, names = set(), []
    for (o, m), nums in blocks.items():
        if len(nums) > max_faces:
            continue
        pts, ok = [], True
        for n in nums:
            for tok in lines[n].split()[1:]:
                try:
                    v, vt = _idx(tok)
                except ValueError:
                    ok = False
                    break
                if v <= 0 or v > len(V):
                    ok = False                  # relative indices: leave the group alone
                    break
                pts.append(V[v - 1])
            if not ok:
                break
        if not ok or not pts:
            continue
        size = max(max(c) - min(c) for c in zip(*pts))
        if size < max_size:
            drop.update(nums)
            names.append(f"{o or m} ({len(nums)} tris, {size * 100:.1f} cm)")

    if drop:
        out = [l for n, l in enumerate(lines) if n not in drop]
        p.write_bytes((nl.join(out) + nl).encode("utf-8"))
        say(f"  removed {len(names)} tiny dummy mesh(es): " + ", ".join(names[:10]))
    else:
        say("  no tiny dummy meshes found in the OBJ")
    return len(drop)


# ----------------------------------------------------------------------------------------------------------------
# GLB
# ----------------------------------------------------------------------------------------------------------------
_GLB_MAGIC = 0x46546C67                 # 'glTF'
_CHUNK_JSON = 0x4E4F534A                # 'JSON'
_CHUNK_BIN = 0x004E4942                 # 'BIN\0'
_COMP = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _read_glb(raw: bytes):
    """-> (gltf dict, bin bytes or None, [(chunk type, bytes), ...] in file order)."""
    if len(raw) < 20:
        raise ValueError("not a GLB file (too short)")
    magic, _version, _length = struct.unpack_from("<III", raw, 0)
    if magic != _GLB_MAGIC:
        raise ValueError("not a GLB file")
    chunks, off = [], 12
    while off + 8 <= len(raw):
        clen, ctype = struct.unpack_from("<II", raw, off)
        chunks.append((ctype, raw[off + 8: off + 8 + clen]))
        off += 8 + clen
    if not chunks or chunks[0][0] != _CHUNK_JSON:
        raise ValueError("GLB has no JSON chunk")
    gltf = json.loads(chunks[0][1].decode("utf-8"))
    bin_data = next((c for t, c in chunks[1:] if t == _CHUNK_BIN), None)
    return gltf, bin_data, chunks


def _write_glb(path: Path, gltf: dict, chunks) -> None:
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)         # JSON chunk is padded with spaces to a 4-byte boundary
    body = struct.pack("<II", len(js), _CHUNK_JSON) + js
    for ctype, data in chunks[1:]:      # every other chunk (BIN) goes back untouched
        body += struct.pack("<II", len(data), ctype) + data
    path.write_bytes(struct.pack("<III", _GLB_MAGIC, 2, 12 + len(body)) + body)


def _mat_local(node: dict):
    """4x4 row-major matrix of a node's local transform."""
    m = node.get("matrix")
    if m and len(m) == 16:              # glTF stores matrices column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    xx, yy, zz, xy, xz, yz, wx, wy, wz = x * x, y * y, z * z, x * y, x * z, y * z, w * x, w * y, w * z
    return [[(1 - 2 * (yy + zz)) * sx, 2 * (xy - wz) * sy, 2 * (xz + wy) * sz, tx],
            [2 * (xy + wz) * sx, (1 - 2 * (xx + zz)) * sy, 2 * (yz - wx) * sz, ty],
            [2 * (xz - wy) * sx, 2 * (yz + wx) * sy, (1 - 2 * (xx + yy)) * sz, tz],
            [0.0, 0.0, 0.0, 1.0]]


def _mat_mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _world_matrices(gltf: dict):
    nodes = gltf.get("nodes", [])
    parent = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i
    cache: dict = {}

    def world(i, depth=0):
        if i in cache:
            return cache[i]
        m = _mat_local(nodes[i])
        if i in parent and depth < 1000:        # depth guard: a malformed file must not recurse forever
            m = _mat_mul(world(parent[i], depth + 1), m)
        cache[i] = m
        return m

    return world


def _read_ints(gltf, bin_data, acc_idx, limit):
    """Index accessor -> list of ints, or None when it is longer than limit (a real mesh, no need to read it)."""
    acc = gltf["accessors"][acc_idx]
    if acc["count"] > limit:
        return None
    if "sparse" in acc or acc.get("type") != "SCALAR" or acc["componentType"] not in (5121, 5123, 5125):
        raise ValueError("unsupported index accessor")
    bv = gltf["bufferViews"][acc["bufferView"]]
    fmt, size = _COMP[acc["componentType"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or size
    return [struct.unpack_from("<" + fmt, bin_data, base + i * stride)[0] for i in range(acc["count"])]


def _read_vec3(gltf, bin_data, acc_idx, indices):
    """POSITION accessor (float VEC3 only) -> the points at the given vertex indices."""
    acc = gltf["accessors"][acc_idx]
    if "sparse" in acc or acc.get("type") != "VEC3" or acc["componentType"] != 5126 or acc.get("normalized"):
        raise ValueError("unsupported POSITION accessor")
    bv = gltf["bufferViews"][acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or 12
    out = []
    for i in indices:
        if i < 0 or i >= acc["count"]:
            raise ValueError("vertex index out of range")
        out.append(struct.unpack_from("<3f", bin_data, base + i * stride))
    return out


def _mesh_world_size(gltf, bin_data, node, world_m, max_faces):
    """(triangle count, biggest world-space extent) of a node's mesh, or None if it is not a small plain mesh."""
    if "skin" in node:
        return None                                     # skinned vertices are not in node space: leave alone
    buf = gltf.get("buffers", [])
    if not buf or "uri" in buf[0]:
        return None                                     # data is not inside this GLB
    prims = gltf["meshes"][node["mesh"]].get("primitives", [])
    if not prims:
        return None
    tris, pts = 0, []
    for prim in prims:
        if prim.get("mode", 4) != 4 or "extensions" in prim:        # triangles only, no draco / meshopt
            return None
        pos = prim["attributes"]["POSITION"]
        if "indices" in prim:
            idx = _read_ints(gltf, bin_data, prim["indices"], max_faces * 3)
        else:
            n = gltf["accessors"][pos]["count"]
            idx = list(range(n)) if n <= max_faces * 3 else None
        if not idx:
            return None                                 # too big (or empty): a real mesh
        tris += len(idx) // 3
        if tris > max_faces:
            return None
        pts += _read_vec3(gltf, bin_data, pos, idx)
    if not pts:
        return None
    wp = [tuple(sum(world_m[r][k] * (p[k] if k < 3 else 1.0) for k in range(4)) for r in range(3)) for p in pts]
    return tris, max(max(c) - min(c) for c in zip(*wp))


def strip_stray_glb(path, say: Say = print, max_faces: int = MAX_FACES, max_size: float = MAX_SIZE) -> int:
    """Detach tiny dummy meshes from a .glb in place (the nodes stay). Returns the number of nodes cleaned."""
    p = Path(path)
    gltf, bin_data, chunks = _read_glb(p.read_bytes())
    nodes = gltf.get("nodes", [])
    if bin_data is None or not nodes or not gltf.get("meshes"):
        say("  no tiny dummy meshes found in the GLB")
        return 0

    world = _world_matrices(gltf)
    names = []
    for i, node in enumerate(nodes):
        if "mesh" not in node:
            continue
        try:
            res = _mesh_world_size(gltf, bin_data, node, world(i), max_faces)
        except (ValueError, KeyError, IndexError, TypeError, struct.error):
            continue                                    # anything unusual: keep the mesh, never guess
        if res is None or res[1] >= max_size:
            continue
        tris, size = res
        del node["mesh"]
        node.pop("weights", None)                       # morph weights are meaningless without a mesh
        names.append(f"{node.get('name') or 'node ' + str(i)} ({tris} tris, {size * 100:.1f} cm)")

    if names:
        _write_glb(p, gltf, chunks)
        say(f"  removed {len(names)} tiny dummy mesh(es): " + ", ".join(names[:10]))
    else:
        say("  no tiny dummy meshes found in the GLB")
    return len(names)
