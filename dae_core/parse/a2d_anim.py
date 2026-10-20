"""
a2d_anim.py - Dagor Anim2Data (.a2d) -> glTF animation.

Pipeline (all verified on fa_18e_block_2_anim.a2d):
  GRP resource bytes  ->  CompressedData (3-byte size + method 0x80 = Oodle; the app's own daKernel Oodle)
                      ->  'ANIM' v3 container: 16-byte header, ACL 2.1 buffer, then one name per ACL track
                      ->  acl_decode.decode()  (pure numpy port of ACL's decompressor)
                      ->  per-frame TRS written as glTF animation channels on the already-exported GLB nodes

Conventions (checked against the exported GLB, every one of the 312 bones matches at frame 0):
  * a2d keys are absolute LOCAL transforms in the same space as the exporter's node.tm; no axis conversion
  * tracks are matched to GLB nodes by name
  * gltf_conv decomposes tm with "flip X scale if det < 0"; keys are composed to a matrix and decomposed with
    that same rule, otherwise mirrored bones (a2d scale (1,1,-1), GLB scale (-1,1,1)) would rotate wrongly
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

try:
    from parse.acl_decode import decode as acl_decode
    from parse import anm_clips
except ImportError:                      # running outside the app (tests)
    from acl_decode import decode as acl_decode
    import anm_clips

Say = Callable[[str], None]

# Blender's timeline starts at frame 1 while glTF times start at 0 s, so the clip's first key (the rest pose)
# would sit on frame 0, one frame before the visible range. Shifting every key time by this many frames puts the
# rest pose on Blender's start frame. Set to 0 for no shift (keys then start at t = 0 s).
TIME_OFFSET_FRAMES = 1

# With an animtree available the a2d is also split into one glTF animation per mechanism (gear, flaps, hook ...).
# True: the full, unsplit clip is written as well (it is the original export and plays everything at once; costs
# about the size of the a2d again). False: only the named clips.
KEEP_FULL_CLIP = True


# ----------------------------------------------------------------------------------------------------------------
# container
# ----------------------------------------------------------------------------------------------------------------
@dataclass
class A2d:
    version: int
    track_names: list
    label_names: list
    acl_blob: bytes = field(repr=False, default=b"")


def unwrap(raw: bytes) -> bytes:
    """GRP bytes -> plain 'ANIM' container. Uses the app's CompressedData (Oodle via daKernel)."""
    if raw[:4] == b"ANIM":
        return raw
    from util.decompression import CompressedData
    from util.fileread import BinFile
    out = CompressedData(BinFile(raw)).decompress()
    if not out or out[:4] != b"ANIM":
        raise ValueError("a2d did not unpack to an ANIM container (compression method unsupported?)")
    return bytes(out)


def parse_container(data: bytes) -> A2d:
    label, version, hdr_size, _unk = struct.unpack_from("<4sIII", data, 0)
    if label != b"ANIM":
        raise ValueError("not an ANIM container")
    if version >> 8 != 3:
        raise ValueError(f"unsupported ANIM version {version:#x} (only v3 / ACL is supported)")
    acl_size = struct.unpack_from("<I", data, hdr_size)[0]
    blob = bytes(data[hdr_size:hdr_size + acl_size])
    num_tracks = struct.unpack_from("<I", blob, 16)[0]          # tracks_header.num_tracks

    pos, names = hdr_size + acl_size, []
    while pos < len(data):
        end = data.index(0, pos)
        s = data[pos:end]
        if not s or not all(32 <= c < 127 for c in s):
            break
        names.append(s.decode("ascii"))
        pos = end + 1
    if len(names) < num_tracks:
        raise ValueError(f"only {len(names)} names for {num_tracks} tracks")
    return A2d(version, names[:num_tracks], names[num_tracks:], blob)


# ----------------------------------------------------------------------------------------------------------------
# math (mirrors gltf_conv's node decomposition)
# ----------------------------------------------------------------------------------------------------------------
def _quat_to_mat(q):
    x, y, z, w = q
    return np.array([[1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
                     [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
                     [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)]])


def _mat_to_quat(R):
    t = np.trace(R)
    if t > 0:
        s = np.sqrt(t + 1) * 2; w = .25 * s
        x = (R[2, 1] - R[1, 2]) / s; y = (R[0, 2] - R[2, 0]) / s; z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2]) * 2; w = (R[2, 1] - R[1, 2]) / s; x = .25 * s
        y = (R[0, 1] + R[1, 0]) / s; z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2]) * 2; w = (R[0, 2] - R[2, 0]) / s; x = (R[0, 1] + R[1, 0]) / s
        y = .25 * s; z = (R[1, 2] + R[2, 1]) / s
    else:
        s = np.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1]) * 2; w = (R[1, 0] - R[0, 1]) / s; x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s; z = .25 * s
    q = np.array([x, y, z, w]); return q / np.linalg.norm(q)


def _decompose(q, s):
    """rotation+scale as stored in the a2d -> (quat, scale) the way gltf_conv writes node TRS"""
    M = _quat_to_mat(q) * np.asarray(s, float)[None, :]
    cols = [M[:, i].copy() for i in range(3)]
    sc = [float(np.linalg.norm(c)) for c in cols]
    for i in range(3):
        if sc[i] > 1e-6:
            cols[i] /= sc[i]
    R = np.column_stack(cols)
    if np.linalg.det(R) < 0:
        R[:, 0] *= -1; sc[0] *= -1
    return _mat_to_quat(R), np.array(sc)


# ----------------------------------------------------------------------------------------------------------------
# GLB
# ----------------------------------------------------------------------------------------------------------------
def read_glb(path):
    d = Path(path).read_bytes()
    jl, _ = struct.unpack_from("<II", d, 12)
    js = json.loads(d[20:20 + jl])
    off = 20 + jl
    bl, _ = struct.unpack_from("<II", d, off)
    return js, bytearray(d[off + 8:off + 8 + bl])


def write_glb(path, js, binb):
    while len(binb) % 4:
        binb.append(0)
    jb = json.dumps(js, separators=(",", ":")).encode()
    jb += b" " * ((4 - len(jb) % 4) % 4)
    total = 12 + 8 + len(jb) + 8 + len(binb)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<II", len(jb), 0x4E4F534A)); f.write(jb)
        f.write(struct.pack("<II", len(binb), 0x004E4942)); f.write(bytes(binb))


def add_animation(js: dict, binb: bytearray, a2d: A2d, name: str, say: Say = print,
                  only_animated: bool = True, frame_range: Optional[tuple] = None,
                  track_filter: Optional[Callable[[str], bool]] = None, extras: Optional[dict] = None,
                  decoded: Optional[dict] = None) -> int:
    """Decode `a2d` and append one animation to the glTF dict / binary chunk. Returns the channel count.

    track_filter: keep only the tracks whose name it accepts (used to cut one mechanism out of the whole a2d).
    extras:       stored as the glTF animation's "extras" (the Blender side reads it to know what a clip is).
    decoded:      acl_decode() result of this a2d, so splitting into many clips decodes it once.
    A clip without any channel is not written (returns 0)."""
    tr = decoded if decoded is not None else acl_decode(a2d.acl_blob)
    rot, trans, scale, rate = tr["rot"], tr["trans"], tr["scale"], tr["rate"]
    f0, f1 = frame_range if frame_range else (0, rot.shape[0] - 1)
    nf = f1 - f0 + 1
    times = ((np.arange(nf) + TIME_OFFSET_FRAMES) / rate).astype("<f4")

    names = {}
    for i, n in enumerate(js["nodes"]):
        names.setdefault(n.get("name"), i)

    def add_acc(arr, typ):
        while len(binb) % 4:
            binb.append(0)
        start = len(binb)
        binb.extend(np.ascontiguousarray(arr, "<f4").tobytes())
        js.setdefault("bufferViews", []).append({"buffer": 0, "byteOffset": start, "byteLength": int(arr.nbytes)})
        acc = {"bufferView": len(js["bufferViews"]) - 1, "componentType": 5126, "count": int(len(arr)), "type": typ}
        if typ == "SCALAR":
            acc["min"], acc["max"] = [float(arr.min())], [float(arr.max())]
        js.setdefault("accessors", []).append(acc)
        return len(js["accessors"]) - 1

    t_acc = None                                     # created with the first channel, so an empty clip adds nothing
    samplers, channels, missing = [], [], 0
    for k, nm in enumerate(a2d.track_names):
        if track_filter is not None and not track_filter(nm):
            continue
        ni = names.get(nm)
        if ni is None:
            missing += 1
            continue
        R, T, S = rot[f0:f1 + 1, k], trans[f0:f1 + 1, k], scale[f0:f1 + 1, k]
        anim = [np.ptp(R, 0).max() > 1e-6, np.ptp(T, 0).max() > 1e-6, np.ptp(S, 0).max() > 1e-6]
        if only_animated and not any(anim):
            continue
        if t_acc is None:
            t_acc = add_acc(times, "SCALAR")
        dec = [_decompose(R[f], S[f]) for f in range(nf)]
        q = np.array([d[0] for d in dec]); s = np.array([d[1] for d in dec])
        for f in range(1, nf):                       # one hemisphere -> shortest-path slerp
            if np.dot(q[f], q[f - 1]) < 0:
                q[f] = -q[f]
        for flag, path, arr, typ in ((anim[0], "rotation", q, "VEC4"), (anim[1], "translation", T, "VEC3"),
                                     (anim[2], "scale", s, "VEC3")):
            if flag or not only_animated:
                samplers.append({"input": t_acc, "output": add_acc(arr, typ), "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1, "target": {"node": ni, "path": path}})
    if not channels:
        return 0
    anim = {"name": name, "samplers": samplers, "channels": channels}
    if extras:
        anim["extras"] = extras
    js.setdefault("animations", []).append(anim)
    if js.get("buffers"):
        js["buffers"][0]["byteLength"] = len(binb)
    say(f"  animation '{name}': {len(channels)} channels, {nf} frames @ {rate:g} fps"
        + (f", {missing} track(s) without a matching node" if missing else ""))
    return len(channels)


def add_named_clips(js: dict, binb: bytearray, a2d: A2d, graph: dict, res_name: str, say: Say = print,
                    suffix: str = "") -> int:
    """Split `a2d` into one animation per mechanism of the animtree `graph` (anm_graph.extract output).
    Returns the number of clips written. Clip names: 'gear', 'flaps', 'hook', ... (+ suffix when several a2d)."""
    groups = anm_clips.build_groups(graph)
    hits, unclaimed = anm_clips.match_tracks(groups, a2d.track_names)
    decoded = acl_decode(a2d.acl_blob)
    written = 0
    for gname, tracks in hits.items():
        g = groups[gname]
        keep = set(tracks)
        n = add_animation(js, binb, a2d, gname + suffix, say, track_filter=keep.__contains__, decoded=decoded,
                          extras={"wt_group": gname, "wt_params": g.params, "wt_source": res_name})
        written += bool(n)
    skipped = [g for g in groups if g not in hits]
    say(f"  named clips: {written} written from {len(groups)} mechanisms in the animtree"
        f" ({len(skipped)} matched no track in this a2d, {len(unclaimed)} track(s) belong to no mechanism)")
    return written


def add_a2d_to_glb(glb_path, resources, say: Say = print, graph: Optional[dict] = None) -> int:
    """resources: [(name, raw_bytes_from_grp), ...]. Rewrites the GLB in place; returns animations added.
    graph: optional animtree (anm_graph.extract output); with it the a2d is also split into named clips."""
    js, binb = read_glb(glb_path)
    added = 0
    for name, raw in resources:
        try:
            a2d = parse_container(unwrap(raw))
            if graph is None or KEEP_FULL_CLIP:
                if add_animation(js, binb, a2d, name, say):
                    added += 1
            if graph is not None:
                suffix = f"@{name}" if len(resources) > 1 else ""
                added += add_named_clips(js, binb, a2d, graph, name, say, suffix)
        except Exception as e:                       # one bad clip must not lose the model
            say(f"  [WARNING] a2d '{name}' skipped: {type(e).__name__}: {e}")
    if added:
        write_glb(glb_path, js, binb)
    return added