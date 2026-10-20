"""
glb_merge.py - hang other .glb models (missiles, bombs, racks, tanks) on named nodes of a vehicle .glb.

The game does not store weapons inside the vehicle model: a Weapon{} block names an `emitter` node of the vehicle and the
weapon's own model is placed on that node (a rack model carries its own emitter nodes for what it holds). This module
does that placement on exported files, with plain json + the binary chunk (no glTF library):

    m = GlbMerger("vehicle.glb")
    mdl = m.load_model("aim9.glb", name="us_aim_9m")        # resources are copied once per model
    nodes = m.instance(mdl, parent=m.find_node("aim_9e_006"), label="slot11/aim9")
    m.save()

Every instance gets its own wrapper node (child of the emitter), the model's nodes below it, and shares the copied meshes /
materials / textures with the other instances of the same model. Animations and skins of the added model are dropped (the
weapon is rigid), and the vehicle's own nodes, meshes and animations are never touched: the file only grows.
Textures given as files (uri) next to the model are embedded, so the result is one self-contained .glb.
"""
from __future__ import annotations

import copy
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote

try:
    from parse.a2d_anim import read_glb, write_glb
except ImportError:                      # running outside the app (tests)
    from a2d_anim import read_glb, write_glb

Say = Callable[[str], None]

_TEX_KEYS = ("baseColorTexture", "metallicRoughnessTexture", "normalTexture", "occlusionTexture", "emissiveTexture")


@dataclass
class _Model:
    name: str
    nodes: list                                   # child nodes, already remapped: mesh -> base mesh index, no skin
    roots: list[int]                              # indices into `nodes` of the scene roots
    names: dict[str, int] = field(default_factory=dict)   # lower node name -> index into `nodes`
    uses: int = 0


class GlbMerger:
    def __init__(self, base_path, say: Say = print):
        self.path = Path(base_path)
        self.js, self.bin = read_glb(self.path)
        self.say = say
        for k in ("nodes", "meshes", "materials", "textures", "images", "samplers", "accessors", "bufferViews"):
            self.js.setdefault(k, [])
        self._names: dict[str, int] = {}
        for i, n in enumerate(self.js["nodes"]):
            nm = (n.get("name") or "").lower()
            if nm and nm not in self._names:
                self._names[nm] = i
        self.added = 0

    # ------------------------------------------------------------------ lookup
    def find_node(self, name: str) -> Optional[int]:
        return self._names.get((name or "").lower())

    # ------------------------------------------------------------------ binary helpers
    def _append(self, data: bytes) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin += data
        return off

    # ------------------------------------------------------------------ importing a model
    def load_model(self, path, name: str = "") -> Optional[_Model]:
        """Copy the resources of a .glb into the base file once. Returns None when the file cannot be used."""
        path = Path(path)
        try:
            cj, cb = read_glb(path)
        except Exception as e:                    # noqa: BLE001
            self.say(f"  [weapons] cannot read {path.name}: {type(e).__name__}: {e}")
            return None
        js = self.js
        base = self._append(bytes(cb))
        bv0 = len(js["bufferViews"])
        for bv in cj.get("bufferViews", []):
            nb = dict(bv)
            nb["buffer"] = 0
            nb["byteOffset"] = bv.get("byteOffset", 0) + base
            js["bufferViews"].append(nb)

        acc0 = len(js["accessors"])
        for a in cj.get("accessors", []):
            na = copy.deepcopy(a)
            if "bufferView" in na:
                na["bufferView"] += bv0
            if "sparse" in na:                    # the exporter never writes sparse accessors
                na.pop("sparse")
            js["accessors"].append(na)

        smp0 = len(js["samplers"])
        js["samplers"] += copy.deepcopy(cj.get("samplers", []))

        img0 = len(js["images"])
        for im in cj.get("images", []):
            ni = {k: v for k, v in im.items() if k not in ("uri", "bufferView")}
            if "bufferView" in im:
                ni["bufferView"] = im["bufferView"] + bv0
            elif "uri" in im:
                f = path.parent / unquote(im["uri"])
                if f.is_file():
                    off = self._append(f.read_bytes())
                    js["bufferViews"].append({"buffer": 0, "byteOffset": off, "byteLength": f.stat().st_size})
                    ni["bufferView"] = len(js["bufferViews"]) - 1
                    ni["mimeType"] = mimetypes.guess_type(f.name)[0] or "image/png"
            js["images"].append(ni)

        tex0 = len(js["textures"])
        for t in cj.get("textures", []):
            nt = copy.deepcopy(t)
            if "source" in nt:
                nt["source"] += img0
            if "sampler" in nt:
                nt["sampler"] += smp0
            js["textures"].append(nt)

        mat0 = len(js["materials"])
        for m in cj.get("materials", []):
            nm = copy.deepcopy(m)
            self._shift_textures(nm, tex0)
            js["materials"].append(nm)

        mesh0 = len(js["meshes"])
        for me in cj.get("meshes", []):
            nme = copy.deepcopy(me)
            for pr in nme.get("primitives", []):
                pr["attributes"] = {k: v + acc0 for k, v in pr.get("attributes", {}).items()}
                if "indices" in pr:
                    pr["indices"] += acc0
                if "material" in pr:
                    pr["material"] += mat0
                pr.pop("targets", None)
            js["meshes"].append(nme)

        used = set(cj.get("extensionsUsed", []))
        if used:
            js["extensionsUsed"] = sorted(set(js.get("extensionsUsed", [])) | used)

        nodes = []
        for n in cj.get("nodes", []):
            nn = copy.deepcopy(n)
            nn.pop("skin", None)
            nn.pop("camera", None)
            if "mesh" in nn:
                nn["mesh"] += mesh0
            nodes.append(nn)
        scenes = cj.get("scenes") or [{}]
        roots = list(scenes[cj.get("scene", 0)].get("nodes", [])) if scenes else []
        if not roots and nodes:                   # no scene: every node that is nobody's child
            kids = {c for n in nodes for c in n.get("children", [])}
            roots = [i for i in range(len(nodes)) if i not in kids]
        names: dict[str, int] = {}
        for i, n in enumerate(nodes):
            nm = (n.get("name") or "").lower()
            if nm and nm not in names:
                names[nm] = i
        return _Model(name or path.stem, nodes, roots, names)

    @staticmethod
    def _shift_textures(obj, tex0: int) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _TEX_KEYS and isinstance(v, dict) and "index" in v:
                    v["index"] += tex0
                else:
                    GlbMerger._shift_textures(v, tex0)
        elif isinstance(obj, list):
            for v in obj:
                GlbMerger._shift_textures(v, tex0)

    # ------------------------------------------------------------------ placing an instance
    def instance(self, model: _Model, parent: int, label: str) -> dict[str, int]:
        """Add the model under node `parent`. Returns {lower original node name: node index in the base file}."""
        nodes = self.js["nodes"]
        first = len(nodes)
        for n in model.nodes:
            nn = copy.deepcopy(n)
            if nn.get("children"):
                nn["children"] = [c + first for c in nn["children"]]
            if nn.get("name"):
                nn["name"] = f"{label}/{nn['name']}"       # prefixed: the vehicle's animation tracks match by name
            nodes.append(nn)
        wrapper = len(nodes)
        nodes.append({"name": label, "children": [r + first for r in model.roots]})
        p = nodes[parent]
        p.setdefault("children", []).append(wrapper)
        model.uses += 1
        self.added += 1
        return {nm: i + first for nm, i in model.names.items()}

    # ------------------------------------------------------------------ output
    def save(self, path=None) -> None:
        js = self.js
        for k in ("samplers", "textures", "images", "materials"):
            if not js.get(k):
                js.pop(k, None)
        js["buffers"] = [{"byteLength": len(self.bin)}]
        write_glb(path or self.path, js, self.bin)
