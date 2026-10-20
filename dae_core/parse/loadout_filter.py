"""
loadout_filter.py - remove the pylon / launcher / pod geometry that the chosen loadout does not show.

The game's model contains every pylon, rail and pod of every possible loadout at once; the vehicle BLK says which
nodes are visible for a given loadout (see vehicle_core/Loadout.py). This module applies that list to an exported file:

GLB: a node whose MESH name (or node name) is in the hide set loses its mesh. The node itself stays, so animation
     channels that target it and the hierarchy below it are untouched. Same technique as fx_strip.
OBJ: faces under a `g <name>` / `o <name>` group in the hide set are removed. Vertex lines stay, so face indices stay valid.

Names are compared case-insensitively. A mesh node the GLB writer had to rename ("<name>_mesh") still matches through
its mesh name, which is always the game object's name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

try:
    from parse.a2d_anim import read_glb, write_glb
except ImportError:                      # running outside the app (tests)
    from a2d_anim import read_glb, write_glb

Say = Callable[[str], None]


def _lower(names: Iterable[str]) -> set[str]:
    return {n.lower() for n in names if n}


def hide_nodes_glb(path, hide: Iterable[str], say: Say = print) -> int:
    """Detach the meshes of the hidden nodes from a .glb in place. Returns how many nodes lost their mesh."""
    hide = _lower(hide)
    if not hide:
        return 0
    js, binb = read_glb(path)
    meshes = js.get("meshes", [])
    removed: list[str] = []
    for n in js.get("nodes", []):
        mi = n.get("mesh")
        if mi is None:
            continue
        node_name = (n.get("name") or "").lower()
        mesh_name = (meshes[mi].get("name") or "").lower() if 0 <= mi < len(meshes) else ""
        if mesh_name in hide or node_name in hide:
            del n["mesh"]
            n.pop("skin", None)
            removed.append(mesh_name or node_name or f"mesh{mi}")
    if removed:
        write_glb(path, js, binb)
    say(f"  loadout: hid {len(removed)} pylon/launcher/pod mesh(es) not used by this loadout"
        + (": " + ", ".join(sorted(set(removed))[:30]) + (" ..." if len(set(removed)) > 30 else "") if removed else ""))
    return len(removed)


# ---------------------------------------------------------------------------------------------------- mirroring
# Some pods exist in the vehicle model only ONCE, on one side of the jet; the game shows them on the other side
# (F-16: the AN/AAQ-33 Sniper / LANTIRN pod sits on the same side as the AN/ASQ-213 HARM pod in the file, but opposite
# it in the game). The vehicle's lateral symmetry plane is z = 0, x is forward, y is up (checked on the F-16: wing
# tanks at z = +-1.845, centre tank at z = 0). A node is mirrored by M @ world with M = diag(1, 1, -1).
LATERAL_AXIS = 2


def _node_local(n):
    import numpy as np
    if "matrix" in n:
        return np.array(n["matrix"], float).reshape(4, 4).T
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = R @ np.diag(n.get("scale", [1, 1, 1]))
    M[:3, 3] = n.get("translation", [0, 0, 0])
    return M


def _quat(R):
    import numpy as np
    t = R[0, 0] + R[1, 1] + R[2, 2]
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        q = [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, 0.25 * s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s, (R[2, 1] - R[1, 2]) / s]
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s, (R[0, 2] - R[2, 0]) / s]
    else:
        s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s, (R[1, 0] - R[0, 1]) / s]
    q = np.array(q) / np.linalg.norm(q)
    return [float(v) for v in q]


def mirror_nodes_js(js: dict, names: Iterable[str]) -> list[str]:
    """Reflect the named nodes (and everything below them) across the lateral symmetry plane. Returns the names done.

    The new world matrix is M @ old world, so a node with a negative determinant ends up with a positive one and
    glTF viewers flip the triangle winding on their own; the mesh data itself is never touched."""
    import numpy as np
    want = _lower(names)
    nodes = js.get("nodes", [])
    if not want or not nodes:
        return []
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    local = [_node_local(n) for n in nodes]
    cache: dict[int, object] = {}

    def world(i):
        if i not in cache:
            cache[i] = (world(parent[i]) @ local[i]) if i in parent else local[i]
        return cache[i]

    def lowered(i):
        return (nodes[i].get("name") or "").lower()

    def mesh_name(i):
        mi = nodes[i].get("mesh")
        meshes = js.get("meshes", [])
        return (meshes[mi].get("name") or "").lower() if mi is not None and 0 <= mi < len(meshes) else ""

    hit = [i for i in range(len(nodes)) if lowered(i) in want or mesh_name(i) in want]
    in_closure: set[int] = set()

    def mark(i):
        in_closure.add(i)
        for c in nodes[i].get("children", []):
            mark(c)

    for i in hit:
        mark(i)
    S = np.diag([1.0, 1.0, 1.0, 1.0])
    S[LATERAL_AXIS, LATERAL_AXIS] = -1.0
    done = []
    for i in hit:
        if i in parent and parent[i] in in_closure:
            continue                                  # follows its mirrored parent: its local matrix stays
        Wp = world(parent[i]) if i in parent else np.eye(4)
        L = np.linalg.inv(Wp) @ S @ world(i)
        A = L[:3, :3]
        scale = [float(np.linalg.norm(A[:, k])) for k in range(3)]
        R = np.column_stack([A[:, k] / scale[k] if scale[k] > 1e-9 else A[:, k] for k in range(3)])
        if np.linalg.det(R) < 0:
            R[:, 0] *= -1
            scale[0] *= -1
        n = nodes[i]
        n.pop("matrix", None)
        n["translation"] = [float(v) for v in L[:3, 3]]
        n["rotation"] = _quat(R)
        n["scale"] = scale
        done.append(n.get("name") or mesh_name(i) or f"node{i}")
    return done


def mirror_nodes_glb(path, names: Iterable[str], say: Say = print) -> int:
    """Mirror the named vehicle nodes of a .glb in place (see mirror_nodes_js). Returns how many nodes were moved."""
    names = _lower(names)
    if not names:
        return 0
    js, binb = read_glb(path)
    done = mirror_nodes_js(js, names)
    if done:
        write_glb(path, js, binb)
    say(f"  loadout: mirrored {len(done)} targeting-pod node(s) to the other side"
        + (": " + ", ".join(sorted(set(done))) if done else ""))
    return len(done)


def hide_nodes_obj(path, hide: Iterable[str], say: Say = print) -> int:
    """Remove the faces of hidden groups from a .obj in place. Returns the number of face lines dropped."""
    hide = _lower(hide)
    if not hide:
        return 0
    p = Path(path)
    raw = p.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    out, dropped, names = [], 0, set()
    group = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        tag = line.split(None, 1)
        key = tag[0] if tag else ""
        arg = tag[1].strip() if len(tag) > 1 else ""
        if key in ("o", "g"):
            group = arg.lower()
        elif key in ("f", "l", "p") and group in hide:
            dropped += 1
            names.add(group)
            continue
        out.append(line)
    if dropped:
        p.write_bytes((nl.join(out) + nl).encode("utf-8"))
    say(f"  loadout: removed {dropped} face(s) of {len(names)} pylon/launcher/pod object(s) not used by this loadout"
        + (": " + ", ".join(sorted(names)[:30]) if names else ""))
    return dropped


def mirror_nodes_obj(path, names: Iterable[str], say: Say = print) -> int:
    """Mirror the faces of the named groups of a .obj across the lateral plane, in place. Returns the groups moved.

    The OBJ writer bakes node matrices into the vertices (and negates z), so the lateral plane is z=0 here too.
    The mirrored copies of the used v / vn lines are appended at the end of the file, together with the re-written
    faces of those groups (winding reversed, so they still face outwards); the original faces of the groups are dropped.
    Every other index in the file stays valid."""
    want = _lower(names)
    if not want:
        return 0
    try:
        from parse.obj_merge import parse_face, face_text
    except ImportError:                      # running outside the app (tests)
        from obj_merge import parse_face, face_text
    p = Path(path)
    raw = p.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8", "replace").splitlines()
    v_lines = [ln for ln in lines if ln.startswith("v ")]
    vn_lines = [ln for ln in lines if ln.startswith("vn ")]
    nv, nvt, nvn = 0, 0, 0
    kept: list[str] = []
    moved: list[tuple[str, str, list]] = []          # (group, usemtl, face)
    group, mtl = "", ""
    for ln in lines:
        tag = ln.split(None, 1)
        key = tag[0] if tag else ""
        arg = tag[1].strip() if len(tag) > 1 else ""
        if key == "v":
            nv += 1
        elif key == "vt":
            nvt += 1
        elif key == "vn":
            nvn += 1
        elif key in ("o", "g"):
            group = arg.split()[0] if arg else ""
        elif key == "usemtl":
            mtl = arg
        elif key == "f" and group.lower() in want:
            moved.append((group, mtl, parse_face(ln, nv, nvt, nvn)))
            continue
        kept.append(ln)
    if not moved:
        say("  loadout: no targeting-pod group found in the OBJ to mirror")
        return 0

    def flipped(line: str) -> str:
        k, x, y, z = line.split()[:4]
        return f"{k} {x} {y} {_neg(z)}"

    def _neg(s: str) -> str:
        s = s.strip()
        if s in ("0", "0.0", "0.0000", "-0.0000", "-0.0", "-0"):
            return "0.0000"
        return s[1:] if s.startswith("-") else "-" + s

    new_v: dict[int, int] = {}
    new_vn: dict[int, int] = {}
    add_v: list[str] = []
    add_vn: list[str] = []
    faces_out: list[str] = []
    last_g, last_m = None, None
    for g, m, face in moved:
        tris = []
        for v, vt, vn in face:
            if v not in new_v:
                add_v.append(flipped(v_lines[v - 1]))
                new_v[v] = nv + len(add_v)
            if vn is not None and vn not in new_vn:
                add_vn.append(flipped(vn_lines[vn - 1]))
                new_vn[vn] = nvn + len(add_vn)
            tris.append((new_v[v], vt, None if vn is None else new_vn[vn]))
        tris = [tris[0]] + tris[:0:-1]
        if g != last_g:
            faces_out.append(f"g {g}")
            last_g, last_m = g, None
        if m != last_m:
            if m:
                faces_out.append(f"usemtl {m}")
            last_m = m
        faces_out.append(face_text(tris))
    out = kept + ["# mirrored targeting-pod groups"] + add_v + add_vn + faces_out
    p.write_bytes((nl.join(out) + nl).encode("utf-8"))
    groups = sorted({g for g, _m, _f in moved})
    say(f"  loadout: mirrored {len(groups)} targeting-pod group(s) to the other side: " + ", ".join(groups))
    return len(groups)
