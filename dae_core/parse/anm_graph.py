"""
anm_graph.py - extract the animation graph from a Dagor "animtree" (*.anm, magic b"\\0ac2") compiled from an *.animTree.blk.

What is VERIFIED (checked against the CDK sample sources and values decoded from the F/A-18E file):
  * header: 72 bytes; u32[1]=payload size, u32[2]=offset of the parameter table, u32[14]=end of the name table
  * name table: NUL-separated block/param names starting at 0x48 (50 names for the F/A-18E)
  * parameter table (at u32[2]+8): 8-byte entries (u32 value, u16 name_index, u16 type)
        type 1=string  2=int  3=float  4=point2  5=point3  9=bool   (the same letters as t/i/r/p2/p3/b in a .blk)
        string / point2 / point3 values are offsets: absolute = value + 0x38
  * the entries are the .blk params in source (depth-first) order, so the stream reads like the source file.

What is NOT decoded: the block tree (which block owns which params / children). Only the parameter
stream is used here, so grouping is by key patterns, not by real parent blocks. See "confidence" in the output.

usage: python anm_graph.py <file.anm> [out.json]
"""
import json, re, struct, sys
from pathlib import Path

STR, INT, REAL, P2, P3, BOOL = 1, 2, 3, 4, 5, 9


def unwrap(raw: bytes) -> bytes:
    """GRP resource bytes -> plain 'ac2' animtree. Plain bytes pass through; a CompressedData wrapper (3-byte size +
    method byte, same as the .a2d resources) is unpacked with the app's own decompressor."""
    if raw[:4] == b"\x00ac2":
        return bytes(raw)
    from util.decompression import CompressedData
    from util.fileread import BinFile
    out = CompressedData(BinFile(raw)).decompress()
    if not out or bytes(out[:4]) != b"\x00ac2":
        raise ValueError("animtree resource did not unpack to an ac2 block")
    return bytes(out)


def load(path):
    return load_bytes(Path(path).read_bytes())


def load_bytes(d):
    d = bytes(d)
    if d[:4] != b"\x00ac2":
        raise ValueError("not an ac2 animtree")
    h = struct.unpack_from("<18I", d, 0)
    names = [s.decode() for s in d[0x48:h[14]].split(b"\0") if s]
    table = h[2] + 8
    n = (len(d) - 8 - table) // 8
    ents = [struct.unpack_from("<IHH", d, table + 8 * i) for i in range(n)]
    return d, names, ents


def value(d, e):
    v, _, ty = e
    if ty == STR:
        a = v + 0x38
        return d[a:d.find(b"\0", a)].decode("latin1")
    if ty == INT:
        return v
    if ty == REAL:
        return round(struct.unpack("<f", struct.pack("<I", v))[0], 6)
    if ty == P2:
        return [round(x, 6) for x in struct.unpack_from("<2f", d, v + 0x38)]
    if ty == P3:
        return [round(x, 6) for x in struct.unpack_from("<3f", d, v + 0x38)]
    if ty == BOOL:
        return bool(v)
    return ("raw", ty, v)


def extract(path):
    return extract_bytes(Path(path).read_bytes(), Path(path).name)


def extract_bytes(data, file="animtree"):
    """Same as extract(), but from the bytes of an animtree (e.g. an AnimChar resource read out of a .grp)."""
    d, names, ents = load_bytes(data)
    s = [(names[e[1]], value(d, e)) for e in ents if e[1] < len(names)]
    out = {"file": file, "param_entries": len(s), "bindings": [], "remaps": [], "math": [],
           "entities": [], "a2d": {}, "confidence": {
               "bindings": "high: param immediately followed by nodeRE/node, verified on gear/flaps/wings",
               "remaps": "high: param, destParam, paramMapping* sequence",
               "math": "medium: param, op, v0/p0/p1 sequence",
               "entities": "medium: split at every 'name' key, keys before a name (bnl) are attached to it"}}
    i = 0
    while i < len(s):
        k, v = s[i]
        nk = s[i + 1][0] if i + 1 < len(s) else None
        if k == "param" and nk in ("nodeRE", "node"):
            # one param can be followed by SEVERAL node / nodeRE keys (the Su-33 binds p_intake_grid_door to
            # nozzle4_15 AND nozzle3_15, p_flaps_rel_l_1 to nine flap nodes, ...). Take all of them, one binding each.
            j = i + 1
            targets = []
            while j < len(s) and s[j][0] in ("nodeRE", "node"):
                targets.append((s[j][0], s[j][1])); j += 1
            aon = False
            if j < len(s) and s[j][0] == "arrayNodesOnly":
                aon = bool(s[j][1]); j += 1
            for tk, tv in targets:
                out["bindings"].append({"param": v, tk: tv, "array_nodes_only": aon})
            i = j; continue
        if k == "param" and nk == "destParam":
            r = {"param": v, "dest": s[i + 1][1], "points": []}
            j = i + 2
            while j < len(s) and s[j][0] == "paramMapping":
                r["points"].append(s[j][1]); j += 1
            out["remaps"].append(r); i = j; continue
        if k == "param" and nk == "op":
            m = {"dest": v, "op": s[i + 1][1]}
            j = i + 2
            while j < len(s) and s[j][0] in ("v0", "p0", "p1"):
                m[s[j][0]] = s[j][1]; j += 1
            out["math"].append(m); i = j; continue
        i += 1
    # entities: split at 'name'
    cur = None; pend = []
    for k, v in s:
        if k == "name":
            cur = {"name": v, "keys": pend}; pend = []
            out["entities"].append(cur)
        elif k == "bnl":
            pend.append((k, v))
        elif cur is not None:
            cur["keys"].append((k, v))
    for e in out["entities"]:
        e["kind"] = ("group" if e["name"].startswith("#") else "proc" if e["name"].startswith("~") else "other")
        e["keys"] = [{k: v} for k, v in e["keys"]]
    for k, v in s:
        if k in ("a2d_id", "key_start", "key_end"):
            out["a2d"][k] = v
    return out


if __name__ == "__main__":
    r = extract(sys.argv[1])
    dst = sys.argv[2] if len(sys.argv) > 2 else str(Path(sys.argv[1]).with_suffix(".graph.json"))
    Path(dst).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print({k: (len(v) if isinstance(v, list) else v) for k, v in r.items() if k != "confidence"})