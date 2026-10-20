"""
obj_merge.py - hang weapon models on a vehicle .obj (the OBJ twin of glb_merge.GlbMerger).

The OBJ writer (realres.Model.getOBJ) bakes every rigid object's node matrix (node.wtm) into its vertices and then
negates z, so a vehicle .obj is in "OBJ space" = diag(1, 1, -1) applied to the game's model space, with no extra scale.
A weapon model exported on its own is in the same space. To put it on an emitter node whose world matrix is E (game
model space) the weapon's vertices are transformed by  C = S @ E @ S  with S = diag(1, 1, -1, 1).

* the merged .obj only ever grows at its end: new v / vt / vn lines, then the new faces, so every existing index stays valid
* a transform with a negative determinant (a mirrored emitter) reverses the winding of the faces; normals follow
* the weapon's materials are appended to the vehicle's .mtl (renamed on a name clash with different content);
  texture files are copied next to the vehicle's (renamed on a clash with a different file)
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

Say = Callable[[str], None]
_TEX_KEYS = ("bump", "disp", "decal", "refl")
S_OBJ = np.diag([1.0, 1.0, -1.0, 1.0])


def to_obj_space(E: np.ndarray) -> np.ndarray:
    """A game-model-space world matrix -> the same placement in OBJ space."""
    return S_OBJ @ np.asarray(E, float) @ S_OBJ


def _fmt(x: float) -> str:
    s = f"{x:.4f}"
    return "0.0000" if s == "-0.0000" else s


def parse_face(line: str, nv: int, nvt: int, nvn: int) -> list[tuple]:
    """'f 1/1/1 2/2/2 3/3/3' -> [(v, vt, vn), ...] with absolute 1-based indices (None where absent)."""
    def idx(s: str, n: int):
        if not s:
            return None
        i = int(s)
        return i if i > 0 else n + i + 1
    out = []
    for tok in line.split()[1:]:
        p = tok.split("/")
        out.append((idx(p[0], nv), idx(p[1], nvt) if len(p) > 1 else None, idx(p[2], nvn) if len(p) > 2 else None))
    return out


def face_text(tris: list[tuple]) -> str:
    def one(t):
        v, vt, vn = t
        if vt is None and vn is None:
            return str(v)
        if vn is None:
            return f"{v}/{vt}"
        return f"{v}/{'' if vt is None else vt}/{vn}"
    return "f " + " ".join(one(t) for t in tris)


def _read_lines(path: Path) -> tuple[list[str], str]:
    raw = Path(path).read_bytes()
    return raw.decode("utf-8", "replace").splitlines(), ("\r\n" if b"\r\n" in raw else "\n")


def _parse_mtl(path: Path) -> dict[str, list[str]]:
    """newmtl name -> its lines (the newmtl line first)."""
    blocks: dict[str, list[str]] = {}
    cur = None
    try:
        lines, _ = _read_lines(path)
    except OSError:
        return blocks
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("newmtl"):
            parts = s.split(None, 1)
            cur = parts[1].strip() if len(parts) > 1 else ""
            blocks[cur] = [f"newmtl {cur}"]
        elif cur is not None and s:
            blocks[cur].append(s)
    return blocks


@dataclass
class ObjModel:
    name: str
    verts: np.ndarray                       # (n, 3)
    uvs: list[str]                          # raw "vt ..." lines
    normals: np.ndarray                     # (m, 3)
    body: list                              # ("g", name) | ("usemtl", name) | ("f", [(v, vt, vn), ...])
    materials: dict[str, list[str]]
    mtl_dir: Path
    _mat_map: Optional[dict[str, str]] = field(default=None, repr=False)


def load_obj_model(path, name: str = "") -> Optional[ObjModel]:
    """Parse an .obj written by the exporter (and its .mtl). None when it cannot be read."""
    path = Path(path)
    try:
        lines, _ = _read_lines(path)
    except OSError:
        return None
    verts, uvs, norms, body = [], [], [], []
    mtl_names = []
    for ln in lines:
        if not ln or ln[0] == "#":
            continue
        key = ln.split(None, 1)[0]
        if key == "v":
            p = ln.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif key == "vt":
            uvs.append(ln.strip())
        elif key == "vn":
            p = ln.split()
            norms.append((float(p[1]), float(p[2]), float(p[3])))
        elif key == "f":
            body.append(("f", parse_face(ln, len(verts), len(uvs), len(norms))))
        elif key in ("g", "o"):
            parts = ln.split(None, 1)
            arg = parts[1].strip().replace(" ", "_") if len(parts) > 1 else ""
            body.append(("g", arg))
        elif key == "usemtl":
            parts = ln.split(None, 1)
            body.append(("usemtl", parts[1].strip() if len(parts) > 1 else ""))
        elif key == "mtllib":
            mtl_names.append(ln.split(None, 1)[1].strip())
    if not verts or not any(b[0] == "f" for b in body):
        return None
    materials: dict[str, list[str]] = {}
    for m in mtl_names:
        materials.update(_parse_mtl(path.parent / m))
    return ObjModel(name or path.stem, np.array(verts, float), uvs,
                    np.array(norms, float).reshape(-1, 3), body, materials, path.parent)


class ObjMerger:
    """Append transformed copies of weapon models to a vehicle .obj (see the module docstring)."""

    def __init__(self, obj_path, say: Say = print):
        self.path = Path(obj_path)
        self.say = say
        self.lines, self.nl = _read_lines(self.path)
        self.nv = sum(1 for ln in self.lines if ln.startswith("v "))
        self.nvt = sum(1 for ln in self.lines if ln.startswith("vt "))
        self.nvn = sum(1 for ln in self.lines if ln.startswith("vn "))
        self.mtl_path: Optional[Path] = None
        for ln in self.lines[:20]:
            if ln.startswith("mtllib"):
                self.mtl_path = self.path.parent / ln.split(None, 1)[1].strip()
                break
        self.mtl_blocks = _parse_mtl(self.mtl_path) if self.mtl_path else {}
        self.out: list[str] = []
        self.new_mtl: list[list[str]] = []
        self.added = 0

    # ------------------------------------------------------------------ materials / textures
    def _copy_texture(self, model: ObjModel, rel: str, cache: dict) -> str:
        if rel in cache:
            return cache[rel]
        src = model.mtl_dir / rel
        new = rel
        if src.is_file():
            dst = self.path.parent / rel
            if dst.exists() and dst.stat().st_size != src.stat().st_size:
                new = str(Path(rel).with_name(f"wpn_{model.name}_{Path(rel).name}")).replace("\\", "/")
                dst = self.path.parent / new
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        cache[rel] = new
        return new

    def _materials_for(self, model: ObjModel) -> dict[str, str]:
        if model._mat_map is not None:
            return model._mat_map
        rename: dict[str, str] = {}
        tex_cache: dict[str, str] = {}
        for name, block in model.materials.items():
            body = []
            for ln in block[1:]:
                tok = ln.split()
                if tok and (tok[0].lower().startswith("map_") or tok[0].lower() in _TEX_KEYS) and len(tok) > 1:
                    tok[-1] = self._copy_texture(model, tok[-1], tex_cache)
                    ln = " ".join(tok)
                body.append(ln)
            existing = self.mtl_blocks.get(name)
            final = name
            if existing is not None:
                if existing[1:] == body:
                    rename[name] = name
                    continue
                final = f"{name}__{model.name}"
                n = 2
                while final in self.mtl_blocks:
                    final, n = f"{name}__{model.name}_{n}", n + 1
                rename[name] = final
            self.mtl_blocks[final] = [f"newmtl {final}"] + body
            self.new_mtl.append(self.mtl_blocks[final])
        model._mat_map = rename
        return rename

    # ------------------------------------------------------------------ placing
    def instance(self, model: ObjModel, C: np.ndarray, label: str) -> None:
        """Append one copy of `model` transformed by the 4x4 matrix C (already in OBJ space)."""
        label = label.replace(" ", "_")
        A, t = C[:3, :3], C[:3, 3]
        det = float(np.linalg.det(A))
        V = model.verts @ A.T + t
        if len(model.normals):
            Ainv = np.linalg.inv(A) if abs(det) > 1e-12 else np.eye(3)
            N = model.normals @ Ainv
            ln = np.linalg.norm(N, axis=1)
            N = N / np.where(ln > 1e-12, ln, 1.0)[:, None]
        else:
            N = model.normals
        bv, bvt, bvn = self.nv, self.nvt, self.nvn
        mats = self._materials_for(model)
        out = self.out
        out.append(f"# weapon model {label}")
        out.extend(f"v {_fmt(a)} {_fmt(b)} {_fmt(c)}" for a, b, c in V)
        out.extend(model.uvs)
        out.extend(f"vn {_fmt(a)} {_fmt(b)} {_fmt(c)}" for a, b, c in N)
        self.nv += len(V)
        self.nvt += len(model.uvs)
        self.nvn += len(N)
        flip = det < 0
        opened = False
        for kind, val in model.body:
            if kind == "g":
                out.append(f"g {label}/{val}" if val else f"g {label}")
                opened = True
            elif kind == "usemtl":
                out.append(f"usemtl {mats.get(val, val)}")
            else:
                if not opened:
                    out.append(f"g {label}")
                    opened = True
                tris = [(None if v is None else v + bv, None if vt is None else vt + bvt,
                         None if vn is None else vn + bvn) for v, vt, vn in val]
                if flip:
                    tris = [tris[0]] + tris[:0:-1]
                out.append(face_text(tris))
        self.added += 1

    def save(self) -> None:
        if not self.added:
            return
        lines = list(self.lines)
        if self.new_mtl:
            if self.mtl_path is None:                       # the vehicle had no materials of its own
                self.mtl_path = self.path.with_suffix(".mtl")
                lines.insert(0, f"mtllib {self.mtl_path.name}")
            text = "".join(self.nl.join(b) + self.nl for b in self.new_mtl)
            fresh = True
            if self.mtl_path.is_file() and self.mtl_path.stat().st_size:
                with open(self.mtl_path, "rb") as f:
                    f.seek(-1, 2)
                    fresh = f.read(1) in (b"\n", b"\r")
            with open(self.mtl_path, "ab") as f:
                f.write((("" if fresh else self.nl) + text).encode("utf-8"))
        self.path.write_bytes((self.nl.join(lines + self.out) + self.nl).encode("utf-8"))
