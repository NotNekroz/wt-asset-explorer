"""
acl_decode.py - pure Python/numpy decoder for ACL 2.1 (Animation Compression Library) transform tracks,
specialised for what Dagor's a2dExp writes: uniformly sampled qvvf, rotation = quatf_drop_w_variable,
translation/scale = vector3f_variable, no database, no stripped keyframes.

Ported from the decompression path of ACL v2.1.0 (MIT, github.com/nfrechette/acl) and checked
bit-for-bit against the real library on fa_18e_block_2_anim.a2d. Anything outside that configuration
raises NotImplementedError instead of returning garbage.

    decode(blob) -> dict(rot[F,T,4] xyzw, trans[F,T,3], scale[F,T,3], default[F,T,3] bool, rate, num_tracks, num_samples)

`blob` is the ACL buffer: starts with raw_buffer_header {u32 size, u32 hash}.
"""
import struct
import numpy as np

ACL_TAG = 0xAC11AC11
ROT_DROP_W_VARIABLE = 3
VEC3_VARIABLE = 1


def _bits(buf, off, n):
    """n bits (n<=32) at bit offset `off`, MSB-first - ACL's animated-data bit order."""
    b = off >> 3
    v = int.from_bytes(buf[b:b + 8], "big")
    return (v >> (64 - (off & 7) - n)) & ((1 << n) - 1)


def _f32_from_u32(u):
    return struct.unpack("<f", struct.pack("<I", u))[0]


def _unpack_xyz(buf, off, nbits, raw_bits):
    """-> (xyz float32[3], is_raw)"""
    if nbits == raw_bits:
        return np.array([_f32_from_u32(_bits(buf, off + 32 * i, 32)) for i in range(3)], np.float32)
    mx = np.float32(1.0) / np.float32((1 << nbits) - 1)
    return np.array([_bits(buf, off + nbits * i, nbits) for i in range(3)], np.float32) * mx


def _quat_from_xyz(x, y, z):
    x, y, z = np.float32(x), np.float32(y), np.float32(z)
    # w from the (float32) xyz, but with the cancellation done in float64: 1-|xyz|^2 is ~1e-8 for 180-degree
    # rotations and float32 noise there turns into ~2e-4 of error in w (ACL's SIMD path has the same noise).
    d = float(x) * float(x) + float(y) * float(y) + float(z) * float(z)
    q = np.array([x, y, z, np.sqrt(abs(1.0 - d))], np.float64)
    return (q / np.linalg.norm(q)).astype(np.float32)     # ACL's default rotation normalization policy


def decode(blob: bytes):
    f32 = np.float32
    _size, _hash = struct.unpack_from("<II", blob, 0)  # raw header: buffer size + hash, neither is needed for decoding
    tag, ver, alg, ttype, nt, ns, rate, misc = struct.unpack_from("<IHBBIIfI", blob, 8)
    if tag != ACL_TAG:
        raise ValueError("not an ACL compressed_tracks buffer")
    if ver != 10 or alg != 0 or ttype != 12:
        raise NotImplementedError(f"ACL version={ver} algorithm={alg} track_type={ttype} (only 10/0/12 supported)")
    has_scale = bool(misc & 1)
    default_scale = (misc >> 1) & 1
    scale_fmt, trans_fmt, rot_fmt = (misc >> 2) & 1, (misc >> 3) & 1, (misc >> 4) & 15
    if rot_fmt != ROT_DROP_W_VARIABLE or trans_fmt != VEC3_VARIABLE or (has_scale and scale_fmt != VEC3_VARIABLE):
        raise NotImplementedError(f"unsupported formats rot={rot_fmt} trans={trans_fmt} scale={scale_fmt}")
    if misc & (1 << 8) or misc & (1 << 10):
        raise NotImplementedError("ACL database / stripped keyframes not supported")

    TH = 32  # transform_tracks_header offset inside the blob (8 raw header + 24 tracks header)
    (nseg, nvar, nrot, ntrans, nscale, ncrot, nctrans, ncscale,
     _o_db, o_seg, o_types, o_const, o_clip) = struct.unpack_from("<13I", blob, TH)
    buf = bytes(blob) + bytes(32)  # tail padding for 8-byte bit reads
    RAW = 31  # raw bit rate marker in version >= 2.1.99.1

    # ---- sub-track types: [rot entries][trans entries][scale entries], 2 bits each, track 0 in the top bits
    nent = (nt + 15) // 16
    types = struct.unpack_from(f"<{nent * (3 if has_scale else 2)}I", buf, TH + o_types)

    def kind(section, t):
        return (types[section * nent + t // 16] >> (30 - 2 * (t % 16))) & 3   # 0 default, 1 constant, 2 animated

    kinds = [[kind(s, t) for t in range(nt)] for s in range(3 if has_scale else 2)]
    if not has_scale:
        kinds.append([0] * nt)

    # ---- constant data: rotations (SoA groups of 4, xyz floats) | translations (AoS xyz) | scales (AoS xyz)
    cbase = TH + o_const
    cr = np.frombuffer(buf, "<f4", ncrot * 3, cbase).astype(f32) if ncrot else np.zeros(0, f32)
    ct = np.frombuffer(buf, "<f4", nctrans * 3, cbase + 12 * ncrot).reshape(-1, 3) if nctrans else np.zeros((0, 3), f32)
    cs = np.frombuffer(buf, "<f4", ncscale * 3, cbase + 12 * (ncrot + nctrans)).reshape(-1, 3) if ncscale else np.zeros((0, 3), f32)

    def const_rot(i):
        g, u = divmod(i, 4)
        gs = min(4, ncrot - 4 * g)
        x, y, z = (cr[12 * g + gs * k + u] for k in range(3))
        return _quat_from_xyz(x, y, z)

    # ---- per-sub-track enumeration: constant index and animated index (running counters in track order)
    out_rot = np.zeros((ns, nt, 4), f32); out_rot[..., 3] = 1
    out_tr = np.zeros((ns, nt, 3), f32)
    out_sc = np.full((ns, nt, 3), default_scale, f32)
    is_default = np.ones((ns, nt, 3), bool)
    rot_anim, tr_anim, sc_anim = [], [], []        # track index of each animated sub-track
    ci = [0, 0, 0]
    for t in range(nt):
        # [REMOVED in cleanup] `(cnt, arr)` pairs  (acl_decode.py line 103 of the pre-cleanup file, in decode(), the per-track
        #   sub-track loop right after `ci = [0, 0, 0]`).
        #   What it was: this loop iterated `enumerate(((None, out_rot), (None, out_tr), (None, out_sc)))` and unpacked each
        #   item as `(cnt, arr)`. `cnt` was always None and never read - a leftover placeholder for a per-sub-track counter
        #   (`ci[s]` is the counter actually used). Only `arr` (the rot/translation/scale output array) was used.
        #   Worked with: `out_rot`, `out_tr`, `out_sc` and `ci` / `kinds[s][t]`. Behaviour is identical after the change.
        for s, arr in enumerate((out_rot, out_tr, out_sc)):
            k = kinds[s][t]
            if k == 1:
                v = const_rot(ci[0]) if s == 0 else (ct[ci[1]] if s == 1 else cs[ci[2]])
                arr[:, t] = v; is_default[:, t, s] = False; ci[s] += 1
            elif k == 2:
                (rot_anim, tr_anim, sc_anim)[s].append(t); is_default[:, t, s] = False
    assert len(rot_anim) == nrot and len(tr_anim) == ntrans and len(sc_anim) == nscale, "sub-track count mismatch"
    assert ci[0] == ncrot and ci[1] == nctrans and ci[2] == ncscale, "constant count mismatch"

    # ---- segments
    multi = nseg > 1
    starts = list(struct.unpack_from(f"<{nseg}I", buf, TH + 52)) if multi else [0]
    seg_hdrs = [struct.unpack_from("<4I", buf, TH + o_seg + 16 * i) for i in range(nseg)]  # pose, rot, trans bits, data offset
    rot_pad = (nrot + 3) & ~3
    clip_rot = np.frombuffer(buf, "<f4", nrot * 6, TH + o_clip) if nrot else np.zeros(0, f32)
    clip_tr = np.frombuffer(buf, "<f4", ntrans * 6, TH + o_clip + nrot * 24).reshape(-1, 6) if ntrans else np.zeros((0, 6), f32)
    clip_sc = np.frombuffer(buf, "<f4", nscale * 6, TH + o_clip + nrot * 24 + ntrans * 24).reshape(-1, 6) if nscale else np.zeros((0, 6), f32)

    inv255, inv65535 = f32(1 / 255), f32(1 / 65535)
    for f in range(ns):
        si = max(i for i in range(nseg) if starts[i] <= f)
        pose_bits, rot_bits, tr_bits, sdata = seg_hdrs[si]
        fmt = TH + sdata
        rng = (fmt + nvar + 1) & ~1
        anim = (rng + (6 * nvar if multi else 0) + 3) & ~3
        off = anim * 8 + (f - starts[si]) * pose_bits   # absolute bit offset into buf

        # rotations
        fmt_r, rng_r = fmt, rng
        for j, t in enumerate(rot_anim):
            nb = buf[fmt_r + j]
            g, u = divmod(j, 4)
            gs = min(4, nrot - 4 * g)
            if nb == 0:
                r = rng_r + 24 * g + u
                v = np.array([(buf[r] << 8) | buf[r + 4], (buf[r + 8] << 8) | buf[r + 12], (buf[r + 16] << 8) | buf[r + 20]], f32) * inv65535
                use_seg, use_clip = False, True
            elif nb == RAW:
                v = _unpack_xyz(buf, off, nb, RAW); off += 96; use_seg = use_clip = False
            else:
                v = _unpack_xyz(buf, off, nb, RAW); off += 3 * nb; use_seg = use_clip = True
            if multi and use_seg:
                r = rng_r + 24 * g + u
                mn = np.array([buf[r], buf[r + 4], buf[r + 8]], f32) * inv255
                ex = np.array([buf[r + 12], buf[r + 16], buf[r + 20]], f32) * inv255
                v = v * ex + mn
            if use_clip:
                base = 24 * g
                mn = np.array([clip_rot[base + gs * k + u] for k in range(3)], f32)
                ex = np.array([clip_rot[base + gs * (3 + k) + u] for k in range(3)], f32)
                v = v * ex + mn
            out_rot[f, t] = _quat_from_xyz(v[0], v[1], v[2])
        off = anim * 8 + (f - starts[si]) * pose_bits + rot_bits

        # translations then scales (AoS ranges)
        def vectors(tracks, fmt_p, rng_p, clip, out, off):
            for j, t in enumerate(tracks):
                nb = buf[fmt_p + j]
                r = rng_p + 6 * j
                if nb == 0:
                    v = np.array(struct.unpack_from("<3H", buf, r), f32) * inv65535
                    use_seg, use_clip = False, True
                elif nb == RAW:
                    v = _unpack_xyz(buf, off, nb, RAW); off += 96; use_seg = use_clip = False
                else:
                    v = _unpack_xyz(buf, off, nb, RAW); off += 3 * nb; use_seg = use_clip = True
                if multi and use_seg:
                    mn = np.array(struct.unpack_from("<3B", buf, r), f32) * inv255
                    ex = np.array(struct.unpack_from("<3B", buf, r + 3), f32) * inv255
                    v = v * ex + mn
                if use_clip:
                    v = v * clip[j, 3:6] + clip[j, 0:3]
                out[f, t] = v
            return off

        fmt_t, rng_t = fmt + rot_pad, rng + rot_pad * 6
        vectors(tr_anim, fmt_t, rng_t, clip_tr, out_tr, off)
        if has_scale and nscale:
            vectors(sc_anim, fmt_t + ntrans, rng_t + 6 * ntrans, clip_sc, out_sc, off + tr_bits)

    return {"rot": out_rot, "trans": out_tr, "scale": out_sc, "default": is_default,
            "rate": rate, "num_tracks": nt, "num_samples": ns}