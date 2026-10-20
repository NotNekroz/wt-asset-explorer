"""uv_guide.py - UV island outlines of a model's textures, for painting custom skins.

One PNG per texture: the outline of every UV island of the meshes that use the texture (no inner triangle edges),
drawn at the texture's own size, white lines with a dark edge on a transparent background. Put it as a layer over the texture while painting.

Two steps, so the model is walked once:
  collect()     model -> {texture name: [triangle UVs]}
  draw_guide()  triangle UVs -> PNG

UV orientation: the game's V runs bottom to top (the glTF exporter flips it the same way), an image's rows run top to
bottom, so y = (1 - v) * height.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

GUIDE_DIR = "uv_guides_ROUGH"   # name of the folder (inside the skin folder) the guide PNGs go into
MAX_SIZE = 4096          # longest side of a guide; bigger textures are drawn scaled down
FALLBACK_SIZE = (2048, 2048)
OVERLAP_LIMIT = 3         # an island lying on top of this many other islands is not drawn
OUTLINE_LIMIT = 600_000  # above this many edges the dark outline pass is skipped (speed)


def texture_size(dds_path) -> tuple[int, int] | None:
    """(width, height) from a .dds header, None when it cannot be read."""
    try:
        with open(dds_path, "rb") as f:
            head = f.read(20)
        if head[:4] == b"DDS ":
            h, w = struct.unpack("<II", head[12:20])
            if w > 0 and h > 0:
                return w, h
    except OSError:
        pass
    return None


def collect(mdl, materials, textures_of):
    """Walk a built Model.

    mdl: the Model (iterating gives objects with .name, .faces, .materials {start face: name}, .material_ids
         {start face: index into `materials`}; mdl.getUV(vertex id) gives the UV).
    materials: the model's material list (indices match material_ids).
    textures_of(material): the texture names of that material the guide is wanted for.
    Returns tris_by_tex: tris_by_tex[tex] is a list of (n, 3, 2) float32 arrays, one per mesh part."""
    mat_tex = []
    for m in materials or ():
        try:
            mat_tex.append(list(textures_of(m)))
        except Exception:                       # noqa: BLE001 - one odd material must not lose the rest
            mat_tex.append([])

    tris_by_tex: dict[str, list] = {}
    for obj in mdl:
        faces = obj.faces
        n = len(faces)
        if not n:
            continue
        starts = sorted(s for s in (set(obj.materials) | set(obj.material_ids)) if 0 <= s < n)
        for i, s in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else n
            idx = obj.material_ids.get(s)
            if idx is None or not (0 <= idx < len(mat_tex)) or not mat_tex[idx] or end <= s:
                continue
            arr = np.asarray(faces[s:end], dtype=np.int64).reshape(-1, 3)
            uniq, inv = np.unique(arr, return_inverse=True)
            uv = np.array([mdl.getUV(int(v))[:2] for v in uniq], dtype=np.float32)
            tri = uv[inv.reshape(arr.shape)]                    # (n, 3, 2)
            for tex in mat_tex[idx]:
                tris_by_tex.setdefault(tex, []).append(tri)
    return tris_by_tex


def _island_labels(vid: np.ndarray, n_vert: int) -> np.ndarray:
    """Connected components of triangles that share UV vertices. vid: (n, 3) vertex ids -> (n,) island id."""
    lab = np.arange(n_vert, dtype=np.int64)
    while True:
        m = lab[vid].min(axis=1)
        new = lab.copy()
        for i in range(3):
            np.minimum.at(new, vid[:, i], m)
        while True:                                            # pointer jumping: follow the labels to their root
            jumped = new[new]
            if np.array_equal(jumped, new):
                break
            new = jumped
        if np.array_equal(new, lab):
            break
        lab = new
    return np.unique(lab[vid[:, 0]], return_inverse=True)[1].astype(np.int64)


def _covering_islands(T, isl, n_isl, limit):
    """Ids of the islands that lie on top of at least `limit` other islands.

    T: (N, 3, 2) triangles in UV, isl: (N,) island id of each. An island B counts as covered by A when a point
    inside B (the middle of its biggest triangle) falls inside A's real shape, so a ring or a frame does not cover what
    sits in its hole."""
    from PIL import Image, ImageDraw

    area = np.abs((T[:, 1, 0] - T[:, 0, 0]) * (T[:, 2, 1] - T[:, 0, 1]) - (T[:, 1, 1] - T[:, 0, 1]) * (T[:, 2, 0] - T[:, 0, 0]))
    order = np.lexsort((-area, isl))                           # by island, biggest triangle first
    first = order[np.r_[0, np.flatnonzero(np.diff(isl[order])) + 1]]
    pts = T[first].mean(axis=1)                                # (n_isl, 2) one point inside every island
    tmin, tmax = T.min(axis=1), T.max(axis=1)
    by_isl = np.argsort(isl, kind="stable")
    cuts = np.r_[0, np.flatnonzero(np.diff(isl[by_isl])) + 1]
    bmin = np.minimum.reduceat(tmin[by_isl], cuts, axis=0)
    bmax = np.maximum.reduceat(tmax[by_isl], cuts, axis=0)

    candidates = []
    for s in range(0, n_isl, 256):                             # islands whose box holds `limit`+ other islands' points
        sl = slice(s, min(n_isl, s + 256))
        inside = ((pts[None, :, 0] >= bmin[sl, None, 0]) & (pts[None, :, 0] <= bmax[sl, None, 0]) &
                  (pts[None, :, 1] >= bmin[sl, None, 1]) & (pts[None, :, 1] <= bmax[sl, None, 1]))
        inside[np.arange(sl.stop - s), np.arange(s, sl.stop)] = False
        for row in np.flatnonzero(inside.sum(axis=1) >= limit):
            candidates.append((s + int(row), np.flatnonzero(inside[row])))

    R = 1024
    covering = set()
    for A, others in candidates:
        mask = Image.new("L", (R, R), 0)
        d = ImageDraw.Draw(mask)
        sel = by_isl[cuts[A]: (cuts[A + 1] if A + 1 < n_isl else len(by_isl))]
        px = np.empty((len(sel), 3, 2), dtype=np.float64)
        px[..., 0] = T[sel][..., 0] * (R - 1)
        px[..., 1] = (1.0 - T[sel][..., 1]) * (R - 1)
        for tri in px.tolist():
            d.polygon([tuple(p) for p in tri], fill=255)
        m = np.asarray(mask)
        xs = np.clip(np.rint(pts[others, 0] * (R - 1)).astype(int), 0, R - 1)
        ys = np.clip(np.rint((1.0 - pts[others, 1]) * (R - 1)).astype(int), 0, R - 1)
        if int((m[ys, xs] > 0).sum()) >= limit:
            covering.add(A)
    return covering


def draw_guide(tris_list, size, out_path, on_skip=None) -> bool:
    """Draw the UV island outlines of the given triangle arrays to out_path (PNG). Returns False when nothing was drawable.

    An island that lies on top of OVERLAP_LIMIT or more other islands is left out (on_skip(count) is told how many)."""
    from PIL import Image, ImageDraw

    if not tris_list:
        return False
    w, h = size or FALLBACK_SIZE
    scale = min(1.0, MAX_SIZE / max(w, h))
    w, h = max(16, int(w * scale)), max(16, int(h * scale))

    # Only the outline of each UV island is drawn. An edge is a border when the triangles touching it are not
    # balanced on both sides of it: an inner edge has a triangle on each side, a border (seam / island edge) has them
    # on one side only. Each mesh part is handled on its own, islands are the triangles of a part that share vertices.
    q = 100_000                                                # UVs compared at 1e-5, absorbs float noise
    edge_chunks, owner_chunks, tri_chunks, isl_chunks = [], [], [], []
    n_isl = 0
    for tris in tris_list:
        tris = tris[np.isfinite(tris).all(axis=(1, 2))]
        if not len(tris):
            continue
        # Tiled UVs (a part mapped wholly into tile 1..2 or -1..0): move that triangle back into 0..1. A triangle
        # that touches 0..1 stays where it is, so islands hugging the texture border (UV a hair below 0 or above 1)
        # are not thrown to the opposite side of the picture.
        lo, hi = tris.min(axis=1, keepdims=True), tris.max(axis=1, keepdims=True)
        outside = (lo >= 1.0 - 1e-4) | (hi <= 1e-4)
        tris = tris - np.where(outside, np.floor(tris.mean(axis=1, keepdims=True)), 0.0).astype(np.float32)
        k = np.rint(tris.astype(np.float64) * q).astype(np.int64)
        a, b, c = k[:, 0], k[:, 1], k[:, 2]
        area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        keep = np.abs(area) > 20                               # (UV area < 1e-9) slivers have no inside, drop them
        k = k[keep]
        if not len(k):
            continue
        a, b, c = k[:, 0], k[:, 1], k[:, 2]
        n = len(k)
        pts_u, vid = np.unique(k.reshape(-1, 2), axis=0, return_inverse=True)
        lab = _island_labels(vid.reshape(n, 3), len(pts_u)) + n_isl
        n_isl = int(lab.max()) + 1
        e0 = np.concatenate([a, b, c])
        e1 = np.concatenate([b, c, a])
        third = np.concatenate([c, a, b])
        tri_of = np.tile(np.arange(n), 3)
        swap = (e0[:, 0] > e1[:, 0]) | ((e0[:, 0] == e1[:, 0]) & (e0[:, 1] > e1[:, 1]))
        lo = np.where(swap[:, None], e1, e0)
        hi = np.where(swap[:, None], e0, e1)
        side = np.sign((hi[:, 0] - lo[:, 0]) * (third[:, 1] - lo[:, 1])
                       - (hi[:, 1] - lo[:, 1]) * (third[:, 0] - lo[:, 0]))
        edges = np.concatenate([lo, hi], axis=1)               # (m, 4)
        uniq, first, inv = np.unique(edges, axis=0, return_index=True, return_inverse=True)
        balance = np.bincount(inv.reshape(-1), weights=side, minlength=len(uniq))
        border = balance != 0
        edge_chunks.append(uniq[border])
        owner_chunks.append(lab[tri_of[first[border]]])
        tri_chunks.append(k.astype(np.float64) / q)
        isl_chunks.append(lab)
    if not edge_chunks:
        return False
    E = np.concatenate(edge_chunks)
    owner = np.concatenate(owner_chunks)

    skipped = _covering_islands(np.concatenate(tri_chunks), np.concatenate(isl_chunks), n_isl, OVERLAP_LIMIT)
    if skipped:
        E = E[~np.isin(owner, list(skipped))]
        if on_skip:
            on_skip(len(skipped))

    e = np.unique(E, axis=0).astype(np.float64) / q            # (m, 4): u0 v0 u1 v1
    e = e[(e[:, 0] != e[:, 2]) | (e[:, 1] != e[:, 3])]
    # An edge longer than a whole texture is a tiling seam; drawing it would only put a diagonal across the picture.
    e = e[(np.abs(e[:, 0] - e[:, 2]) <= 1.001) & (np.abs(e[:, 1] - e[:, 3]) <= 1.001)]
    if not len(e):
        return False
    px = np.empty_like(e)
    px[:, [0, 2]] = e[:, [0, 2]] * (w - 1)
    px[:, [1, 3]] = (1.0 - e[:, [1, 3]]) * (h - 1)
    segs = np.rint(px).astype(np.int32).tolist()

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if len(segs) <= OUTLINE_LIMIT:
        for x0, y0, x1, y1 in segs:
            draw.line((x0, y0, x1, y1), fill=(0, 0, 0, 170), width=3)
    for x0, y0, x1, y1 in segs:
        draw.line((x0, y0, x1, y1), fill=(255, 255, 255, 255), width=1)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), format="PNG", compress_level=3)
    return True
