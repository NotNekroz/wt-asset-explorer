from __future__ import annotations

"""glTF 2.0 / GLB converter for War Thunder models."""

import os
import numpy as np
from itertools import chain
from pathlib import Path
from urllib.parse import quote
from pygltflib import (
    GLTF2, Scene, Node, Mesh, Primitive, Attributes, Buffer, BufferView, Accessor,
    Texture, Image, Material, PbrMetallicRoughness, TextureInfo, NormalMaterialTexture,
    FLOAT, UNSIGNED_INT, ELEMENT_ARRAY_BUFFER, ARRAY_BUFFER
)

def _say(msg: str) -> None:
    """Console line in the app's own log format when available, plain print otherwise."""
    try:
        import util.log as _log
        _log.log(msg)
    except Exception:
        print(msg)


# Use EXACTLY the same diffuse/camo pairing + bake as the OBJ/MTL exporter so both outputs match.
# material.py lives in the 'parse' package (parse.material); a bare `import material` only works if the parse
# folder itself is on sys.path, which it is not in the app. That silent ImportError is what skipped the bake.
getCamoPair = bakeCamoPairs = readDdsRgba = skinOverride = glossMetalMaps = None
_material_import_error = None
for _modname in ("parse.material", "material"):
    try:
        _mod = __import__(_modname, fromlist=["getCamoPair", "bakedDiffuseName", "bakeCamoPairs", "readDdsRgba"])
        getCamoPair = _mod.getCamoPair
        bakedDiffuseName = _mod.bakedDiffuseName
        bakeCamoPairs = _mod.bakeCamoPairs
        readDdsRgba = _mod.readDdsRgba
        skinOverride = getattr(_mod, "skinOverride", None)
        glossMetalMaps = getattr(_mod, "glossMetalMaps", None)
        _material_import_error = None
        break
    except Exception as _e:  # keep the LAST error so it can be reported
        _material_import_error = f"{_modname}: {type(_e).__name__}: {_e}"

if getCamoPair is None:  # standalone fallback (no camo bake possible)
    def bakedDiffuseName(diffuse, camo=None):
        return f"{diffuse}__{camo}" if camo else diffuse

# Skinned meshes whose vertices are already stored in model-root space.
# They must NOT be multiplied by any bone matrix and must NOT hang under a bone.
ROOT_SPACE_SKINNED_MESHES = ("track_l", "track_r")


# ---------------------------------------------------------------------------
# Texture lookup configuration
# ---------------------------------------------------------------------------
IMAGE_EXTS = (".dds", ".png", ".tga")   # DDS first: it is the baked file the OBJ uses; stray .png left by the camo bake must not win
NORMAL_FLIP_G = False                    # set True if bumps look inverted in your viewer (DirectX vs OpenGL green channel)
MIRROR_Z_ROOT = True                    # convert the game's left-handed space to glTF's right-handed one (False = old, mirrored output)
BASE_COLOR_SUFFIXES = ("", "_c", "_d", "_albedo")
NORMAL_SUFFIXES = ("_n", "_normal")
KNOWN_SUFFIXES = ("_albedo", "_normal", "_c", "_d", "_n", "_ao")


def normalize_tex_name(raw) -> str:
    """'camo/tank_tex.tga' -> 'tank_tex'; also drops WT's trailing '*' marker."""
    name = str(raw).strip().replace("\\", "/").rstrip("*")
    name = name.split("/")[-1]
    stem, ext = os.path.splitext(name)
    return stem if ext.lower() in IMAGE_EXTS + (".jpg", ".jpeg", ".bmp") else name


def strip_known_suffix(stem: str) -> str:
    low = stem.lower()
    for suf in KNOWN_SUFFIXES:
        if low.endswith(suf):
            return stem[: -len(suf)]
    return stem


def make_gltf_normal(src: Path) -> str | None:
    """War Thunder packs tangent normals as DXT5nm-style: X in alpha, Y in green, red = other data.
    glTF needs a real RGB normal map, so rebuild it (X=A, Y=G, Z reconstructed) and write <n>_gltf.png."""
    try:
        from PIL import Image as PILImage
        dst = src.with_name(src.stem + "_gltf.png")
        if dst.exists():
            return dst.name
        arr = readDdsRgba(str(src)) if readDdsRgba is not None else None
        if arr is None:
            arr = np.asarray(PILImage.open(src).convert("RGBA"))
        nx = arr[..., 3].astype(np.float32)
        ny = arr[..., 1].astype(np.float32)
        nx *= 2.0 / 255.0; nx -= 1.0
        ny *= 2.0 / 255.0; ny -= 1.0
        if NORMAL_FLIP_G:
            np.negative(ny, out=ny)
        nz = 1.0 - nx * nx - ny * ny
        np.clip(nz, 0.0, 1.0, out=nz)
        np.sqrt(nz, out=nz)
        out = np.empty(arr.shape[:2] + (3,), dtype=np.uint8)
        out[..., 0] = nx * 127.5 + 128.0
        out[..., 1] = ny * 127.5 + 128.0
        out[..., 2] = nz * 127.5 + 128.0
        PILImage.fromarray(out, "RGB").save(dst, compress_level=1)
        return dst.name
    except Exception:
        return None


def make_gltf_mr(src: Path) -> str | None:
    """Gloss and metal of a War Thunder _n texture as a glTF metallic-roughness picture <n>_mr.png.
    Red of the _n is the smoothness (bright = glossy) and blue the metalness (War Thunder wiki: PBR texture compliance); glTF
    wants roughness in green and metalness in blue. Built for the game's own normal maps and a custom skin's alike. A picture
    that is already there and not older than its source is kept (a skin's stale one is deleted when the skin is written).
    Returns the file name, or None when the picture cannot be read."""
    try:
        from PIL import Image as PILImage
        dst = src.with_name(src.stem + "_mr.png")
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst.name
        arr = readDdsRgba(str(src)) if readDdsRgba is not None else None
        if arr is None:
            arr = np.asarray(PILImage.open(src).convert("RGBA"))
        out = np.empty(arr.shape[:2] + (3,), dtype=np.uint8)
        out[..., 0] = 255                       # occlusion slot, unused
        if glossMetalMaps is not None:          # same mapping (and log line) as the OBJ's map_Pr / map_Pm
            out[..., 1], out[..., 2] = glossMetalMaps(arr, src.stem)
        else:
            out[..., 1] = 255 - arr[..., 0]     # roughness = 1 - smoothness
            out[..., 2] = arr[..., 2]           # metalness
        PILImage.fromarray(out, "RGB").save(dst, compress_level=1)
        return dst.name
    except Exception as e:
        _say(f"[skin] WARNING gloss / metal map of {src.name} not built: {type(e).__name__}: {e}")
        return None


def index_texture_dir(tex_dir: Path) -> dict:
    """{(lowercase stem, lowercase ext): real file name} for every image in textures/."""
    index = {}
    if tex_dir.is_dir():
        for p in tex_dir.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                index[(p.stem.lower(), p.suffix.lower())] = p.name
    return index


def find_texture(index: dict, stems) -> str | None:
    """First existing file for the given candidate stems, honouring candidate then extension priority."""
    for stem in stems:
        for ext in IMAGE_EXTS:
            hit = index.get((stem.lower(), ext))
            if hit:
                return hit
    return None


def matrix_to_quaternion(R: np.ndarray) -> list[float]:
    """Converts a 3x3 rotation matrix to a glTF-compatible [x, y, z, w] quaternion using pure numpy."""
    trace = np.trace(R)
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    
    quat = np.array([x, y, z, w], dtype=float)
    quat /= np.linalg.norm(quat)
    return quat.tolist()


def _slot_list(mat) -> list:
    """Texture slots by REAL slot index (t0..t10), same source the OBJ/MTL exporter uses."""
    getter = getattr(mat, "getTextureSlots", None)
    if callable(getter):
        return list(getter())
    return list(getattr(mat, "textures", None) or [])


def _diffuse_camo(mat):
    """(diffuse, camo) as clean names. Identical to the OBJ exporter whenever material.py is importable."""
    if getCamoPair is not None:
        try:
            d, c = getCamoPair(mat)
            return (normalize_tex_name(d) if d else None), (normalize_tex_name(c) if c else None)
        except Exception:
            pass
    slots = _slot_list(mat)
    diffuse = normalize_tex_name(slots[0]) if slots and slots[0] else None
    camo = next((normalize_tex_name(t) for t in slots[1:] if t and "camo" in str(t).lower()), None)
    return diffuse, camo


# Glass (dynamic_glass_chrome). The game colours it with a view-angle lookup strip (<x>_rainbow_coating, slot t3) that glTF
# cannot express, and building a texture for it is not worth the risk, so the strip is ignored and every glass material
# gets the same plain, shiny, see-through look:
#   no diff   -> GLASS_TINT
#   diff set  -> that colour (red / green nav-light lenses keep their colour)
# alpha is the material's opacity, but never below GLASS_MIN_ALPHA so very clear glass (canopy: 0.17) is still visible.
GLASS_TINT = (0.75, 0.85, 0.90)
GLASS_MIN_ALPHA = 0.15
GLASS_CLEAR_ALPHA = 0.025      # alpha of "clear" glass (canopy, light colour): the value that looks right in Blender
GLASS_CLEAR_MIN = 0.55         # glass counts as clear when its darkest colour channel is at least this; coloured lenses keep their opacity
GLASS_ROUGHNESS = 0.05


def glass_look(mat):
    """(r, g, b, a) for a glass material."""
    try:
        opacity = float(mat.getParams().get("opacity", 0.5))
    except Exception:
        opacity = 0.5
    diff = tuple(getattr(mat, "diff", None) or (0, 0, 0, 1))
    if sum(diff[:3]) > 1e-6:                       # an all-zero diff is the loader's default, i.e. 'not set'
        colour = (float(diff[0]), float(diff[1]), float(diff[2]))
    else:
        colour = tuple(GLASS_TINT)
    alpha = GLASS_CLEAR_ALPHA if min(colour) >= GLASS_CLEAR_MIN else max(opacity, GLASS_MIN_ALPHA)
    return (*colour, alpha)


def material_ranges(starts, n_faces, resolve):
    """Split one object's faces into [(first_face, end_face, material)] runs.

    starts  : start face of every material group (the keys of obj.materials)
    resolve : start face -> glTF material index or None
    Adjacent groups that resolve to the same material are merged; empty groups are dropped."""
    runs = []
    order = sorted(starts)
    for i, s in enumerate(order):
        e = order[i + 1] if i + 1 < len(order) else n_faces
        e = min(e, n_faces)
        if e <= s:
            continue
        m = resolve(s)
        if runs and runs[-1][2] == m and runs[-1][1] == s:
            runs[-1] = (runs[-1][0], e, m)
        else:
            runs.append((s, e, m))
    return runs


def export_model_to_gltf(mdl, out_dir: str | Path, export_textures: bool = True) -> Path:
    """Converts a loaded Model instance into a binary .glb file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / f"{mdl.exportName}.glb"
    tex_dir = out_dir / "textures"

    gltf = GLTF2(
        scene=0, scenes=[Scene(nodes=[])], nodes=[], meshes=[], materials=[],
        textures=[], images=[], buffers=[Buffer(byteLength=0)], bufferViews=[], accessors=[],
    )

    # Binary blob built from chunks and joined once (no repeated bytearray growth / per-vertex struct.pack).
    chunks: list[bytes] = []
    blob_len = 0

    def add_view(arr: np.ndarray, target: int) -> int:
        nonlocal blob_len
        data = arr.tobytes()
        pad = (-blob_len) % 4
        if pad:
            chunks.append(b"\x00" * pad)
            blob_len += pad
        gltf.bufferViews.append(BufferView(buffer=0, byteOffset=blob_len, byteLength=len(data), target=target))
        chunks.append(data)
        blob_len += len(data)
        return len(gltf.bufferViews) - 1

    def add_accessor(arr, target, comp, typ, with_bounds=True) -> int:
        view = add_view(arr, target)
        acc = Accessor(bufferView=view, componentType=comp, count=len(arr), type=typ)
        if with_bounds:
            acc.min = np.atleast_1d(arr.min(axis=0)).tolist()
            acc.max = np.atleast_1d(arr.max(axis=0)).tolist()
        gltf.accessors.append(acc)
        return len(gltf.accessors) - 1

    # =========================================================================
    # 1. SKELETON & NODE HIERARCHY
    # =========================================================================
    skeleton = mdl.skeleton
    node_map = {}
    node_matrices = {}   # LOCAL matrices (relative to parent bone)
    node_world = {}      # WORLD matrices (parent chain applied)

    if skeleton is not None:
        nodes_list = skeleton.getNodes()

        for node in nodes_list:
            tm = np.array([[float(node.tm[r][c]) for c in range(4)] for r in range(4)], dtype=float)
            node_matrices[node.name] = tm

            r0, r1, r2 = tm[:3, 0].copy(), tm[:3, 1].copy(), tm[:3, 2].copy()
            scale = [float(np.linalg.norm(r0)), float(np.linalg.norm(r1)), float(np.linalg.norm(r2))]
            if scale[0] > 1e-6: r0 /= scale[0]
            if scale[1] > 1e-6: r1 /= scale[1]
            if scale[2] > 1e-6: r2 /= scale[2]

            rot = np.column_stack((r0, r1, r2))
            if np.linalg.det(rot) < 0:
                rot[:, 0] *= -1
                scale[0] *= -1

            node_map[node.name] = len(gltf.nodes)
            gltf.nodes.append(Node(
                name=node.name, translation=tm[:3, 3].tolist(),
                rotation=matrix_to_quaternion(rot), scale=scale,
            ))

        # [REMOVED in cleanup] parent-chain world-matrix builder  (gltf_conv.py lines 251-275 of the pre-cleanup file,
        #   section "1. SKELETON & NODE HIERARCHY", right after the node-creation loop).
        #   What it was: a `by_name` dict {node name -> skeleton node} plus a recursive helper `get_world(name, _stack)`
        #   that filled `node_world[name]` with parent_world @ node_matrices[name] (tm is relative to the parent bone;
        #   `_stack` guarded against parent cycles). A second loop then OVERWROTE each entry with the node's own
        #   world matrix `wtm` (read inside try/except, falling back to the chain result on any error).
        #   Worked with: `node_world` (read by the mesh re-basing in section 3: inv(node_world[target]) is applied to
        #   model-space vertices) and `node_matrices` (the local tm, still used above for the node TRS).
        #   Why removed: every GeomNodeTree.Node carries `wtm` (realres.py ~line 1362, same 4x4 layout as `tm`) and the
        #   OBJ writer uses exactly that matrix for rigid meshes, so the chain result was always overwritten.
        #   Bring it back only if a skeleton type WITHOUT `wtm` ever has to be exported.
        for n in nodes_list:
            node_world[n.name] = np.array(n.wtm, dtype=float)

        for node in nodes_list:
            if node.parent is not None and node.parent.name in node_map:
                parent = gltf.nodes[node_map[node.parent.name]]
                if parent.children is None:
                    parent.children = []
                parent.children.append(node_map[node.name])
            else:
                gltf.scenes[0].nodes.append(node_map[node.name])
    else:
        gltf.nodes.append(Node(name="Root"))
        gltf.scenes[0].nodes.append(0)
        node_map["Root"] = 0
        node_world["Root"] = np.eye(4)

    # =========================================================================
    # 2. MATERIALS
    # =========================================================================
    mat_index_map = {}        # exact material name -> glTF material index
    mat_index_map_ci = {}     # lowercase fallback
    model_mat_to_gltf = {}    # index in mdl.materials -> glTF material index (names are not unique)
    if export_textures and (bakeCamoPairs is None or getCamoPair is None):
        _say(f"[WARNING] glTF: camo bake unavailable (could not import material.py -> {_material_import_error}); "
             "the GLB will use the raw, unpainted diffuse")
    elif export_textures:
        # The glTF path never goes through MaterialTemplateLibrary.exportTextures, so the camo bake has to
        # happen here. Same pairs, same function, same result as the OBJ's map_Kd texture.
        pairs = {}
        for mat in (getattr(mdl, "materials", None) or []):
            try:
                d, c = getCamoPair(mat)
            except Exception:
                continue
            if d and c:
                baked = tex_dir / (bakedDiffuseName(d, c) + ".dds")
                src_times = [(tex_dir / (n + ".dds")).stat().st_mtime
                             for n in (d, c) if (tex_dir / (n + ".dds")).exists()]
                if len(src_times) == 2 and (not baked.exists() or baked.stat().st_mtime < max(src_times)):
                    pairs[bakedDiffuseName(d, c)] = (d, c)
        _say(f"glTF: {len(pairs)} camo combination(s) to bake")
        if pairs:
            try:
                bakeCamoPairs(str(tex_dir), pairs)
            except Exception as e:
                _say(f"[WARNING] glTF: camo bake failed: {type(e).__name__}: {e}")

    tex_index = index_texture_dir(tex_dir) if export_textures else {}
    image_cache = {}          # file name -> glTF texture index (one Image/Texture per file)

    def get_texture_index(file_name: str) -> int:
        idx = image_cache.get(file_name)
        if idx is None:
            gltf.images.append(Image(uri="textures/" + quote(file_name)))
            gltf.textures.append(Texture(source=len(gltf.images) - 1))
            idx = image_cache[file_name] = len(gltf.textures) - 1
        return idx

    default_mat_idx = None

    def get_default_material() -> int:
        nonlocal default_mat_idx
        if default_mat_idx is None:
            default_mat_idx = len(gltf.materials)
            gltf.materials.append(Material(
                name="default_mat",
                pbrMetallicRoughness=PbrMetallicRoughness(
                    baseColorFactor=[0.8, 0.8, 0.8, 1.0], metallicFactor=0.0, roughnessFactor=0.9),
            ))
        return default_mat_idx

    for idx, mat in enumerate(getattr(mdl, "materials", None) or []):
        mat_name = mat.getName() if hasattr(mat, "getName") else f"mat_{idx}"
        pbr = PbrMetallicRoughness(baseColorFactor=[1.0, 1.0, 1.0, 1.0], metallicFactor=0.0, roughnessFactor=0.9)
        gltf_mat = Material(name=mat_name, pbrMetallicRoughness=pbr)

        if tex_index:
            slots = _slot_list(mat)
            diffuse, camo = _diffuse_camo(mat)

            # Base colour: the baked <diffuse>__<camo> file first (exactly what the OBJ's map_Kd uses),
            # then the plain diffuse with its suffix variants.
            color_cands = []
            if diffuse:
                if camo:
                    color_cands.append(bakedDiffuseName(diffuse, camo))
                root = strip_known_suffix(diffuse)
                for base in dict.fromkeys((diffuse, root)):
                    color_cands += [base + suf for suf in BASE_COLOR_SUFFIXES]
            color_file = find_texture(tex_index, color_cands)

            # Normal map: slot t2 (same as the OBJ's map_bump). Prefer the decoded <n>_gltf.png from material.py.
            normal_stem = None
            raw_normal = getattr(mat, "normal", None)
            if raw_normal:
                normal_stem = normalize_tex_name(raw_normal)
            elif len(slots) > 2 and slots[2]:
                normal_stem = normalize_tex_name(slots[2])

            normal_file = None
            if normal_stem:
                normal_file = find_texture(tex_index, [normal_stem + "_gltf"])
                if normal_file is None:
                    src = find_texture(tex_index, [normal_stem])
                    if src is None:
                        root = strip_known_suffix(normal_stem)
                        src = find_texture(tex_index, [normal_stem + s for s in NORMAL_SUFFIXES]
                                           + [root + s for s in NORMAL_SUFFIXES])
                    if src:
                        normal_file = make_gltf_normal(tex_dir / src) or src

            if color_file:
                pbr.baseColorTexture = TextureInfo(index=get_texture_index(color_file))
            if normal_file:
                gltf_mat.normalTexture = NormalMaterialTexture(index=get_texture_index(normal_file), scale=1.0)

            # The _n also carries the gloss (red) and metal (blue) painting, the way the game reads it: show it for the game's own
            # normal maps and for a custom skin's alike. Glass keeps its own look, and only a *_n texture has that layout.
            cls_n = str(getattr(mat, "cls", "") or "").lower()
            if normal_stem and normal_stem.lower().endswith("_n") and "glass" not in cls_n:
                raw_n = find_texture(tex_index, [normal_stem])
                if raw_n and raw_n.lower().endswith(".dds"):
                    mr_file = make_gltf_mr(tex_dir / raw_n)
                    if mr_file:
                        pbr.metallicRoughnessTexture = TextureInfo(index=get_texture_index(mr_file))
                        pbr.metallicFactor = 1.0
                        pbr.roughnessFactor = 1.0

        # Alpha by War Thunder shader class:
        #  *_atest -> cut-out; glass/alpha_blend -> blended;
        #  *masked* -> diffuse alpha is the CAMO MASK, not opacity, so it stays OPAQUE.
        cls_name = str(getattr(mat, "cls", "") or "").lower()
        if "atest" in cls_name:
            gltf_mat.alphaMode = "MASK"
            gltf_mat.alphaCutoff = 127.0 / 255.0
        elif "glass" in cls_name or "alpha_blend" in cls_name:
            gltf_mat.alphaMode = "BLEND"
            if "glass" in cls_name:
                pbr.baseColorFactor = list(glass_look(mat))
                pbr.roughnessFactor = GLASS_ROUGHNESS
        else:
            gltf_mat.alphaMode = "OPAQUE"
        gltf_mat.doubleSided = True

        mat_idx_val = len(gltf.materials)
        gltf.materials.append(gltf_mat)
        model_mat_to_gltf[idx] = mat_idx_val
        mat_index_map.setdefault(mat_name, mat_idx_val)
        mat_index_map_ci.setdefault(mat_name.lower(), mat_idx_val)

    # =========================================================================
    # 3. MESH GEOMETRY
    # =========================================================================
    skeleton_nodes = dict(node_map)  # bones only; never overwritten by mesh nodes
    root_nodes = gltf.scenes[0].nodes

    # Meshes with no bone of their own (the hull '@root', skinned tracks) hang under the skeleton root node,
    # so selecting / rotating that node moves the whole vehicle.
    root_bone = gltf.nodes[root_nodes[0]].name if root_nodes else None
    root_bone_taken = root_bone is not None and gltf.nodes[skeleton_nodes[root_bone]].mesh is not None

    def inv_or_pinv(m):
        try:
            return np.linalg.inv(m)
        except np.linalg.LinAlgError:
            return np.linalg.pinv(m)

    rebased_names = []
    for obj in mdl:
        faces = obj.faces
        if len(faces) == 0:
            continue

        # ---- gather geometry: each unique vertex id is fetched ONCE, everything else is vectorised ----
        flat = np.fromiter(chain.from_iterable(faces), dtype=np.int64)
        if flat.size == 0:
            continue
        uniq, inverse = np.unique(flat, return_inverse=True)
        raw = np.array([mdl.getVertex(int(i))[:3] for i in uniq], dtype=np.float64)
        uvs = np.array([mdl.getUV(int(i))[:2] for i in uniq], dtype=np.float32)
        uvs[:, 1] = 1.0 - uvs[:, 1]  # source UVs are bottom-origin, glTF is top-origin
        indices = inverse.astype("<u4")

        # ---- routing: decide parent node + whether vertices need re-basing ----
        # Same rules as the OBJ writer (realres.Model.generateOBJ):
        #   rigid object WITH a same-named node -> vertices are node-LOCAL (world = node.wtm @ v)  -> keep as is
        #   skinned object                      -> vertices are already MODEL-space                -> re-base
        #   rigid object with no node           -> no matrix is applied, i.e. MODEL-space          -> re-base
        base_bone_name = f"bone_{obj.name}"
        is_skinned = bool(getattr(obj, "skinned", False))
        target_name = None
        reuse_existing_node = False
        force_world = False  # vertices are in model-root space -> must be re-based under the parent node
        if obj.name in ROOT_SPACE_SKINNED_MESHES:
            target_name, force_world = root_bone, True       # child of the skeleton root, NO bone matrix
        elif obj.name in skeleton_nodes:
            target_name, reuse_existing_node = obj.name, True
            force_world = is_skinned
        elif base_bone_name in skeleton_nodes:
            target_name, force_world = base_bone_name, True  # no node of its own -> model-space vertices
        elif root_bone is not None:
            target_name, force_world = root_bone, True       # e.g. the hull '@root'
            if not root_bone_taken:                          # the first such mesh lives ON the root node itself
                reuse_existing_node = root_bone_taken = True

        verts = raw
        if target_name is not None:
            W = node_world[target_name]
            # [REMOVED in cleanup] bounding-box guess for "is this mesh model-space or node-local?"
            #   (gltf_conv.py pre-cleanup lines 447 `has_skin_flag = hasattr(obj, "skinned")`, 452 `guess_space = False  # ...`,
            #   458 `guess_space = not has_skin_flag` and the 3-line `if guess_space:` block at 469-471;
            #   all inside the section-3 mesh loop, "routing" part).
            #   What it did: when a loader object had no `skinned` attribute it compared the mesh's bounding-box centre to
            #   the target node's world position (|c - W.translation| + 1e-3 < |c|) and re-based the vertices with
            #   inv(node_world) if the mesh looked "centred on the node" (model-space). This was the ONLY classifier in the
            #   first version; it fixed the Leclerc (antennas, ex_decor_01/02) but the F/A-18 export had misplaced meshes
            #   (lau_116_2, lau_116_2_cap) that disappeared once routing used `obj.skinned` instead - the bbox guess is the
            #   most likely cause (long, mirrored parts whose geometry sits far from the node origin), not proven.
            #   Worked with: `force_world` / `rebased_names` below and `node_world`.
            #   Why removed: realres.Model.Object ALWAYS has `.skinned` (set in Model.newObject), so the fallback could
            #   never run; routing now follows the OBJ writer's rules exactly (skinned or node-less -> re-base, rigid
            #   with a same-named node -> node-local).
            if force_world:
                Wi = inv_or_pinv(W)
                verts = raw @ Wi[:3, :3].T + Wi[:3, 3]
                rebased_names.append(obj.name)

        verts = np.ascontiguousarray(verts, dtype="<f4")
        uvs = np.ascontiguousarray(uvs, dtype="<f4")

        pos_acc = add_accessor(verts, ARRAY_BUFFER, FLOAT, "VEC3")
        uv_acc = add_accessor(uvs, ARRAY_BUFFER, FLOAT, "VEC2")

        def resolve_range(start):
            """glTF material for the face group starting at `start`: by model index first, then by name."""
            mid = getattr(obj, "material_ids", {}).get(start)
            mat_id = model_mat_to_gltf.get(mid) if mid is not None else None
            if mat_id is None:
                cand = obj.materials.get(start)
                if cand in mat_index_map:
                    mat_id = mat_index_map[cand]
                elif cand is not None and str(cand).lower() in mat_index_map_ci:
                    mat_id = mat_index_map_ci[str(cand).lower()]
            # default_mat when nothing resolved, or a textured-looking material found no base-colour texture (would
            # render white). Glass is meant to have no texture, so it keeps its own material.
            if mat_id is None or (export_textures and gltf.materials[mat_id].pbrMetallicRoughness.baseColorTexture is None
                                  and gltf.materials[mat_id].alphaMode != "BLEND"):
                mat_id = get_default_material()
            return mat_id

        n_faces = len(faces)
        runs = material_ranges(obj.materials.keys(), n_faces, resolve_range) or [(0, n_faces, get_default_material())]
        primitives = []
        for first, end, mat_id in runs:
            sub = np.ascontiguousarray(indices[3 * first:3 * end])
            sub_view = add_view(sub, ELEMENT_ARRAY_BUFFER)
            gltf.accessors.append(Accessor(
                bufferView=sub_view, componentType=UNSIGNED_INT, count=len(sub), type="SCALAR",
                min=[int(sub.min())], max=[int(sub.max())],
            ))
            primitives.append(Primitive(attributes=Attributes(POSITION=pos_acc, TEXCOORD_0=uv_acc),
                                        indices=len(gltf.accessors) - 1, material=mat_id))

        mesh_idx = len(gltf.meshes)
        gltf.meshes.append(Mesh(name=obj.name, primitives=primitives))

        # ---- node attachment ----
        if reuse_existing_node:
            gltf.nodes[skeleton_nodes[target_name]].mesh = mesh_idx
            node_idx = skeleton_nodes[target_name]
        else:
            node_idx = len(gltf.nodes)
            # avoid a duplicate node name when a same-named skeleton bone exists (e.g. 'track_l')
            node_name = f"{obj.name}_mesh" if obj.name in skeleton_nodes else obj.name
            gltf.nodes.append(Node(name=node_name, mesh=mesh_idx))
            if target_name is not None:
                parent = gltf.nodes[skeleton_nodes[target_name]]
                if parent.children is None:
                    parent.children = []
                parent.children.append(node_idx)
            else:
                root_nodes.append(node_idx)
        node_map.setdefault(obj.name, node_idx)

    _say(f"glTF: {len(rebased_names)} model-space mesh(es) re-based under their node: "
         + ", ".join(rebased_names[:40]) + (" ..." if len(rebased_names) > 40 else ""))

    # ---- handedness: the game's model space is left-handed, glTF / Blender are right-handed ----
    # The OBJ writer (realres.Model.getOBJ) negates z for exactly this reason. Without it the .glb is a MIRROR IMAGE of
    # the vehicle: port and starboard swapped and every decal / text reversed. One parent node with scale z = -1 above
    # the whole scene fixes that without touching vertices, node matrices or animation keys (so the clips, the weapon
    # merge and the pod mirroring all keep working in game space). glTF viewers flip the triangle winding for a
    # negative-determinant world matrix on their own.
    if MIRROR_Z_ROOT and gltf.scenes[0].nodes:
        wrapper = len(gltf.nodes)
        gltf.nodes.append(Node(name="wt_mirror_z", scale=[1.0, 1.0, -1.0], children=list(gltf.scenes[0].nodes)))
        gltf.scenes[0].nodes = [wrapper]

    blob = b"".join(chunks)
    gltf.buffers[0].byteLength = len(blob)
    gltf.set_binary_blob(blob)
    gltf.save(str(glb_path))
    return Path(glb_path)