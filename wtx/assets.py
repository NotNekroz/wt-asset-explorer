"""Headless driver for the Dagor Asset Explorer backend.

What the DAE GUI does by hand ("open asset folder", pick a .grp, right-click -> Export to OBJ) is done here
in code:

  1. index        - read the header of every .grp (entry names + class ids + byte offsets) and every .dxp.bin (texture
                    names) once, and cache the result, so a vehicle can be found by name instantly
  2. resolve      - vehicle / weapon  ->  the DynModel (or RendInst) entry that holds its 3D model
  3. export       - load that .grp, make its skeleton / collision visible to the model, load only the
                    texture packs the model actually needs, then call DAE's own Model.exportObj()
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import util.log as log
from parse.gameres import GameResDesc, GameResourcePack
from parse.material import DDSx, DDSxTexturePack2, _loadDdsArray
from parse.realres import Anim2Data, AnimChar, CollisionGeom, DynModel, GeomNodeTree, REALRES_CLASSES_DICT, RendInst
from util.assetcacher import AssetCacher

Status = Callable[[str], None]
Progress = Callable[[int, int], None]

DYNMODEL_ID = DynModel.staticClassId
RENDINST_ID = RendInst.staticClassId
MODEL_CLASS_IDS = {DYNMODEL_ID, RENDINST_ID}
_CACHED_TYPES = (GeomNodeTree, CollisionGeom, DynModel, RendInst)  # same set DAE keeps in AssetCacher
INDEX_VERSION = 4  # Bumped to support raw byte offset caching
# Package folders under content*/ that are never read: this tool only exports vehicles. pkg_infantry holds infantry
# weapons and costs ~2.5 s of Desc.bin parsing per run. Empty this tuple to bring it back.
EXCLUDED_PKGS = ("pkg_infantry",)


def _clean_tex_name(s: str) -> str:
    """Strips Dagor engine material flags (*, $, #, ?) and extension suffixes for clean lookup."""
    if not s:
        return ""
    clean = re.split(r"[*$?#]", str(s))[0].strip().lower()
    if clean.endswith(".dds") or clean.endswith(".tga"):
        clean = clean.rsplit(".", 1)[0]
    return clean


@dataclass(frozen=True)
class AssetRef:
    """One 3D model inside one .grp."""
    name: str
    grp: str        # path relative to the game folder, '/' separated
    class_id: int
    reason: str = ""

    @property
    def kind(self) -> str:
        return "DynModel" if self.class_id == DYNMODEL_ID else "RendInst"

    @property
    def grp_file(self) -> str:
        return self.grp.rsplit("/", 1)[-1]


def _sig(p: Path) -> list[int]:
    st = p.stat()
    return [st.st_size, int(st.st_mtime)]


def _stem(grp_rel: str) -> str:
    s = grp_rel.rsplit("/", 1)[-1].lower()
    s = s[:-4] if s.endswith(".grp") else s
    return s[:-3] if s.endswith("-hq") else s


def _extract_grp_offsets(grp_pack: GameResourcePack) -> list[dict]:
    """Extracts entry metadata including exact byte offset and size for instant seek lookups."""
    res_cnt = grp_pack.getRealResEntryCnt()
    entries = []
    for i in range(res_cnt):
        entry = grp_pack.getRealResEntry(i)
        offset = entry._RealResEntry__offset
        if i + 1 < res_cnt:
            next_offset = grp_pack.getRealResEntry(i + 1)._RealResEntry__offset
            size = next_offset - offset
        else:
            size = grp_pack.size - offset

        entries.append({
            "name": entry.getName(),
            "class_id": entry.getClassId(),
            "offset": offset,
            "size": size
        })
    return entries


class AssetLibrary:
    def __init__(self, game_dir: Path, cache_dir: Path):
        self.game_dir = Path(game_dir)
        self.cache_dir = Path(cache_dir)
        self.grp_entries: dict[str, list[dict]] = {}       # grp rel -> [{"name": str, "class_id": int, "offset": int, "size": int}, ...]
        self.dxp_names: dict[str, list[str]] = {}          # dxp rel -> [texture names]
        self.desc_files: list[str] = []                    # rel paths of *Desc.bin
        self.name_index: dict[str, list[tuple[str, str, int]]] = {}   # lower name -> [(name, grp, classId)]
        self.tex_index: dict[str, list[str]] = {}          # cleaned tex name -> [dxp rel]
        self._descs_loaded: set[str] = set()
        self.overrides: dict[str, dict] = {}
        self._overrides_path = self.cache_dir / "overrides.json"
        self._pack_batch = None                            # set of texture packs already in memory while a weapon batch runs (None = no batch)
        self.last_node_world: dict = {}                    # skeleton world matrices of the model export_obj built last
        self.vehicle_blk: Path | None = None               # BLK of the vehicle selected in the GUI: its default_skin says which camo it wears
        self._load_overrides()

    # ------------------------------------------------------------------ helpers
    def _rel(self, p: Path) -> str:
        return p.relative_to(self.game_dir).as_posix()
    
    def _cache_grp_once(self, grp_rel: str, done: set) -> None:
        """Fallback when a .grp has no entry list in the index: cache every skeleton / collision / model in it."""
        if grp_rel in done:
            return
        done.add(grp_rel)
        grp_path = self.game_dir / grp_rel
        if not grp_path.exists():
            return
        try:
            pack = GameResourcePack(str(grp_path))
            for a in pack.getPackedFiles():
                if type(a) in _CACHED_TYPES:
                    AssetCacher.cacheAsset(a)
        except Exception:
            pass

    # What the model builder asks AssetCacher for is only "<model>_skeleton" (GeomNodeTree) and "<model>_collision"
    # (CollisionGeom) - realres.py _getCachedAsset / getSkeleton. The rest are the model's own side files, which are tiny.
    _COMPANION_SUFFIXES = ("_skeleton", "_collision", "_char", "_dmg_skeleton", "_dmg", "_xray", "_xray_skeleton")

    @classmethod
    def _companion_names(cls, base_name: str) -> set[str]:
        """Lower-case names of the files that belong to ONE model (nothing of any other vehicle or weapon)."""
        name = base_name.lower()
        stem = name[:-9] if name.endswith("_dynmodel") else name
        return {b + suf for b in {name, stem} for suf in cls._COMPANION_SUFFIXES}

    def _cache_entry(self, grp_rel: str, lname: str) -> bool:
        """Cache the single entry `lname` of a .grp from the index, without reading the other entries of the file."""
        grp_path = self.game_dir / grp_rel
        for item in self.grp_entries.get(grp_rel, []):
            item_name = item["name"] if isinstance(item, dict) else item[0]
            if item_name.lower() != lname:
                continue
            cid = item["class_id"] if isinstance(item, dict) else item[1]
            cls_type = REALRES_CLASSES_DICT.get(cid)
            if cls_type in _CACHED_TYPES:
                try:
                    AssetCacher.cacheAsset(cls_type(
                        filePath=str(grp_path), name=item_name,
                        size=item["size"] if isinstance(item, dict) else 0,
                        offset=item["offset"] if isinstance(item, dict) else 0))
                    return True
                except Exception:
                    return False
        return False

    def _preload_companions(self, base_name: str, status: Status):
        """Cache the skeleton / collision files of this one model, wherever in the game files they are."""
        n = 0
        done: set = set()
        for comp in sorted(self._companion_names(base_name)):
            hits = self.name_index.get(comp)          # {lower name: [(name, grp, classId)]}, no scan of every .grp
            if not hits:
                continue
            grp_rel = hits[0][1]
            if grp_rel in self.grp_entries:
                n += self._cache_entry(grp_rel, comp)
            else:
                self._cache_grp_once(grp_rel, done)
        status(f"  cached {n} companion file(s) of {base_name}")

    @property
    def index_path(self) -> Path:
        import hashlib
        key = hashlib.sha1(str(self.game_dir).lower().encode()).hexdigest()[:10]
        return self.cache_dir / f"asset_index_{key}.json"

    def discover(self) -> tuple[list[Path], list[Path], list[Path]]:
        """All .grp / .dxp.bin / *Desc.bin under content*/res (including content.hq)."""
        grps, dxps, descs = [], [], []
        content_dirs = [p for p in sorted(self.game_dir.glob("content*")) if p.is_dir()]
        for cdir in content_dirs:
            res_dirs = [p / "res" for p in sorted(cdir.glob("*"))
                        if (p / "res").is_dir() and p.name.lower() not in EXCLUDED_PKGS]
            for res in res_dirs:
                for root, _dirs, files in os.walk(res):
                    for f in files:
                        full = Path(root) / f
                        if f.endswith(".grp"):
                            grps.append(full)
                        elif f.endswith(".dxp.bin"):
                            dxps.append(full)
                        elif f.endswith("Desc.bin"):
                            descs.append(full)
        return grps, dxps, descs

    # ------------------------------------------------------------------ index
    def build_index(self, status: Status, progress: Progress | None = None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        status("Scanning content folders (standard & HQ) for .grp / .dxp.bin files...")
        grps, dxps, descs = self.discover()
        self.desc_files = [self._rel(p) for p in descs]
        if not grps:
            raise RuntimeError(f"No .grp files found under {self.game_dir}. Is this the War Thunder folder?")

        cached = {"grps": {}, "dxps": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if data.get("version") == INDEX_VERSION:
                cached = data
        except Exception:
            pass

        new_grps: dict[str, dict] = {}
        new_dxps: dict[str, dict] = {}
        total = len(grps) + len(dxps)
        done = 0
        reused = 0
        failed = 0
        status(f"Indexing {len(grps)} model packs and {len(dxps)} texture packs...")

        log.setMuted(True)
        try:
            for p in grps:
                rel, sig = self._rel(p), _sig(p)
                old = cached["grps"].get(rel)
                if old and old["sig"] == sig:
                    new_grps[rel] = old
                    reused += 1
                else:
                    try:
                        pack = GameResourcePack(str(p))
                        entries = _extract_grp_offsets(pack)
                    except Exception:
                        entries, failed = [], failed + 1
                    new_grps[rel] = {"sig": sig, "entries": entries}
                done += 1
                if progress and done % 40 == 0:
                    progress(done, total)

            for p in dxps:
                rel, sig = self._rel(p), _sig(p)
                old = cached["dxps"].get(rel)
                if old and old["sig"] == sig:
                    new_dxps[rel] = old
                    reused += 1
                else:
                    try:
                        names = DDSxTexturePack2.peekNames(str(p))
                    except Exception:
                        names, failed = [], failed + 1
                    new_dxps[rel] = {"sig": sig, "names": names}
                done += 1
                if progress and done % 40 == 0:
                    progress(done, total)
        finally:
            log.setMuted(False)

        try:
            self.index_path.write_text(json.dumps({"version": INDEX_VERSION, "grps": new_grps, "dxps": new_dxps}),
                                       encoding="utf-8")
        except OSError:
            pass
        if progress:
            progress(total, total)

        self.grp_entries = {rel: d["entries"] for rel, d in new_grps.items()}
        self.dxp_names = {rel: d["names"] for rel, d in new_dxps.items()}
        self._rebuild_lookup()
        n_models = sum(1 for v in self.name_index.values() for _n, _g, c in v if c in MODEL_CLASS_IDS)
        status(f"Asset index ready: {len(self.grp_entries)} model packs, {n_models} models, "
               f"{len(self.tex_index)} textures ({reused} files from cache"
               + (f", {failed} unreadable" if failed else "") + ")")

    def _rebuild_lookup(self) -> None:
        self.name_index.clear()
        self.tex_index.clear()
        for grp, entries in self.grp_entries.items():
            for item in entries:
                # Support structure compatibility
                if isinstance(item, dict):
                    name, cid = item["name"], item["class_id"]
                else:
                    name, cid = item[0], item[1]
                self.name_index.setdefault(name.lower(), []).append((name, grp, cid))
        for dxp, names in self.dxp_names.items():
            for n in names:
                cleaned = _clean_tex_name(n)
                if cleaned:
                    self.tex_index.setdefault(cleaned, []).append(dxp)

    # ------------------------------------------------------------------ animations (.a2d)
    def find_a2d(self, ref: AssetRef) -> list[tuple[str, str, dict]]:
        """Anim2Data resources that belong to a model: '<model>_anim' and '<model>_anim*'.
        Returns [(name, grp_rel, entry_dict)], same-GRP first."""
        stem = ref.name.lower()
        if stem.endswith("_dynmodel"):
            stem = stem[:-9]
        want = stem + "_anim"
        a2d_id = Anim2Data.staticClassId
        found: list[tuple[str, str, dict]] = []
        seen: set = set()

        def take(grp_rel: str, item) -> None:
            if not isinstance(item, dict) or item["class_id"] != a2d_id:
                return
            lname = item["name"].lower()
            if (lname == want or lname.startswith(want + "_")) and (lname, grp_rel) not in seen:
                seen.add((lname, grp_rel))
                found.append((item["name"], grp_rel, item))

        for item in self.grp_entries.get(ref.grp, []):
            take(ref.grp, item)
        if not found:                                   # animation pack lives in another .grp
            for lname, lst in self.name_index.items():
                if lname == want or lname.startswith(want + "_"):
                    for n, g, c in lst:
                        if c == a2d_id:
                            for item in self.grp_entries.get(g, []):
                                if isinstance(item, dict) and item["name"] == n:
                                    take(g, item)
        return found

    def find_animtree(self, ref: AssetRef) -> Optional[tuple[str, str, dict]]:
        """AnimChar (.anm animtree) resource of a model: '<model>_animtree'. Returns (name, grp_rel, entry_dict) or
        None. It tells which nodes each parameter (gear, flaps ...) drives, which is what splits the a2d into
        named clips."""
        stem = ref.name.lower()
        if stem.endswith("_dynmodel"):
            stem = stem[:-9]
        want = stem + "_animtree"
        anm_id = AnimChar.staticClassId

        def pick(grp_rel: str):
            for item in self.grp_entries.get(grp_rel, []):
                if isinstance(item, dict) and item["class_id"] == anm_id and item["name"].lower() == want:
                    return item["name"], grp_rel, item
            return None

        hit = pick(ref.grp)
        if hit:
            return hit
        for n, g, c in self.name_index.get(want, []):    # animtree lives in another .grp
            if c == anm_id:
                hit = pick(g)
                if hit:
                    return hit
        return None

    def _load_animtree(self, ref: AssetRef, status: Status) -> Optional[dict]:
        """Parsed animtree (anm_graph.extract_bytes output) of the model, or None (-> one unsplit clip)."""
        try:
            hit = self.find_animtree(ref)
            if not hit:
                near = [i["name"] for i in self.grp_entries.get(ref.grp, [])
                        if isinstance(i, dict) and i["class_id"] == AnimChar.staticClassId]
                status("  No animtree '" + ref.name + "_animtree' found, animations stay one unsplit clip."
                       + (f" (AnimChar resources in this GRP: {', '.join(near[:6])})" if near else ""))
                return None
            from parse import anm_graph
            name, grp_rel, item = hit
            res = AnimChar(filePath=str(self.game_dir / grp_rel), name=name, size=item["size"], offset=item["offset"])
            graph = anm_graph.extract_bytes(anm_graph.unwrap(res.getBin().read()), name)
            status(f"  Animtree '{name}': {len(graph['bindings'])} parameter bindings")
            return graph
        except Exception as e:
            status(f"  [WARNING] animtree unreadable, keeping one unsplit clip: {type(e).__name__}: {e}")
            return None

    def _add_animations(self, ref: AssetRef, glb_file: Path, status: Status) -> int:
        from parse.a2d_anim import add_a2d_to_glb
        hits = self.find_a2d(ref)
        if not hits:
            status("No .a2d animation found for this model (skipping animations).")
            return 0
        resources = []
        for name, grp_rel, item in hits:
            try:
                res = Anim2Data(filePath=str(self.game_dir / grp_rel), name=name,
                                size=item["size"], offset=item["offset"])
                resources.append((name, res.getBin().read()))
            except Exception as e:
                status(f"  [WARNING] could not read a2d '{name}': {type(e).__name__}: {e}")
        status(f"Adding {len(resources)} animation(s) to the GLB ...")
        graph = self._load_animtree(ref, status)
        return add_a2d_to_glb(glb_file, resources, status, graph=graph)

    # ------------------------------------------------------------------ resolving
    def _models_named(self, lname: str, reason: str) -> list[AssetRef]:
        return [AssetRef(n, g, c, reason) for n, g, c in self.name_index.get(lname, []) if c in MODEL_CLASS_IDS]

    @staticmethod
    def _grp_rank(ref: AssetRef, wanted: str) -> tuple:
        stem = _stem(ref.grp)
        # 0 = High Quality (content.hq or -hq stem), 1 = Standard Quality
        is_hq = 0 if ("content.hq" in ref.grp.lower() or ref.grp.lower().endswith("-hq.grp")) else 1
        exact = 0 if stem == wanted else (1 if stem.endswith("_" + wanted) else 2)
        return (is_hq, exact, len(stem), ref.grp)

    def _dedupe(self, refs: list[AssetRef]) -> list[AssetRef]:
        seen, out = set(), []
        for r in refs:
            if (r.name, r.grp) not in seen:
                seen.add((r.name, r.grp))
                out.append(r)
        return out

    def find_vehicle_models(self, vehicle_id: str, model_id: str) -> list[AssetRef]:
        """Ranked list of candidate 3D models for a vehicle (best HQ model first)."""
        override = self._get_override(f"vehicle:{vehicle_id}")
        if override:
            return [override]

        mid = (model_id or "").lower().strip()
        mid = "" if mid in ("n/a", "none") else mid
        vid = vehicle_id.lower().replace("-", "_")
        stems = [s for s in dict.fromkeys([mid, vid]) if s]

        found: list[AssetRef] = []
        for s in stems:
            for suffix in ("_dynmodel", ""):
                hits = sorted(self._models_named(s + suffix, f"entry '{s + suffix}'"),
                              key=lambda r: self._grp_rank(r, s))
                found += hits

        if not found:
            for s in stems:
                for grp, entries in self.grp_entries.items():
                    gs = _stem(grp)
                    if gs == s or gs.endswith("_" + s):
                        models = []
                        for item in entries:
                            name = item["name"] if isinstance(item, dict) else item[0]
                            cid = item["class_id"] if isinstance(item, dict) else item[1]
                            if cid in MODEL_CLASS_IDS:
                                models.append((name, cid))
                        models.sort(key=lambda e: (
                            0 if "content.hq" in grp.lower() else 1,
                            not e[0].lower().endswith("_dynmodel"),
                            not e[0].lower().startswith(s),
                            len(e[0])
                        ))
                        found += [AssetRef(n, grp, c, f"model in {grp.rsplit('/', 1)[-1]}") for n, c in models[:3]]

        if not found:
            for s in stems:
                if len(s) < 4:
                    continue
                hits = [AssetRef(n, g, c, f"name contains '{s}'")
                        for lname, lst in self.name_index.items() if s in lname
                        for n, g, c in lst if c in MODEL_CLASS_IDS]
                hits.sort(key=lambda r: (
                    0 if "content.hq" in r.grp.lower() else 1,
                    not r.name.lower().endswith("_dynmodel"),
                    len(r.name)
                ))
                found += hits[:8]
        return self._dedupe(found)

    def find_weapon_models(self, weapon_id: str, hints: list[str]) -> list[AssetRef]:
        override = self._get_override(f"weapon:{weapon_id}")
        if override:
            return [override]

        wid = weapon_id.lower()
        raw_names = [h.lower() for h in hints] + [wid]
        names = []

        for n in raw_names:
            if n not in names:
                names.append(n)
            cleaned = re.sub(r"(_ldgp|_default|_launcher|_container|_pylon|_pod|_slot|_set|_group)", "", n)
            if cleaned and cleaned not in names:
                names.append(cleaned)

        found: list[AssetRef] = []
        for n in names:
            for suffix in ("_dynmodel", ""):
                hits = sorted(self._models_named(n + suffix, f"entry '{n + suffix}'"),
                              key=lambda r: self._grp_rank(r, n))
                found += hits

        if not found:
            for n in names:
                if len(n) < 4:
                    continue
                hits = [AssetRef(nm, g, c, f"name contains '{n}'")
                        for lname, lst in self.name_index.items() if n in lname
                        for nm, g, c in lst if c in MODEL_CLASS_IDS]
                hits.sort(key=lambda r: (
                    0 if "content.hq" in r.grp.lower() else 1,
                    not r.name.lower().endswith("_dynmodel"),
                    len(r.name)
                ))
                found += hits[:8]
                if found:
                    break

        if not found:
            wid_tokens = set(re.findall(r"[a-z0-9]+", wid)) - {"ldgp", "default", "launcher", "container", "pylon", "pod", "slot"}
            if len(wid_tokens) >= 2:
                scored_hits = []
                for lname, lst in self.name_index.items():
                    m_tokens = set(re.findall(r"[a-z0-9]+", lname))
                    overlap = wid_tokens.intersection(m_tokens)
                    if len(overlap) >= len(wid_tokens) - 1 and len(overlap) >= 2:
                        for nm, g, c in lst:
                            if c in MODEL_CLASS_IDS:
                                score = len(overlap) * 10 - abs(len(wid_tokens) - len(m_tokens))
                                if "content.hq" in g.lower():
                                    score += 5
                                scored_hits.append((score, AssetRef(nm, g, c, f"token match '{' '.join(overlap)}'")))
                scored_hits.sort(key=lambda x: x[0], reverse=True)
                found += [r for _s, r in scored_hits[:8]]

        return self._dedupe(found)

    # ------------------------------------------------------------------ loadout weapon models
    def attach_mounts(self, glb_file: Path, mounts, out_dir: Path, status: Status, textures: bool = True) -> int:
        """Hang the weapon models of a loadout on the vehicle .glb. Returns how many models were placed.

        mounts: [(slot, preset, Mount)] from LoadoutData.mounts(). A Mount is a chain of levels (rack -> pod -> rocket):
        the first level hangs on the vehicle's `emitter` node, each next level on the emitter nodes its container's own
        model declares. Each distinct model is exported once to a temporary folder and copied into the vehicle file once;
        repeated use only adds nodes."""
        import shutil
        from parse.glb_merge import GlbMerger

        glb_file, out_dir = Path(glb_file), Path(out_dir)
        tmp_root = out_dir / "_weapon_models_tmp"
        merger = GlbMerger(glb_file, status)
        models: dict[str, object] = {}              # level key -> loaded model (or None when unavailable)
        refs_cache: dict[str, Optional[AssetRef]] = {}
        placed, problems = 0, []

        def model_for(level) -> object:
            key = "|".join((level.stem, *level.hints)).lower()
            if key in models:
                return models[key]
            ref = None
            if key not in refs_cache:
                found = self.find_weapon_models(level.stem, list(level.hints))
                refs_cache[key] = found[0] if found else None
            ref = refs_cache[key]
            mdl = None
            if ref is None:
                problems.append(f"no 3D model for '{level.stem}'" + (f" (BLK names {', '.join(level.hints)})" if level.hints else ""))
            else:
                dest = tmp_root / re.sub(r"[^\w.-]+", "_", ref.name)
                status(f"  [weapons] exporting {ref.name} ({ref.grp_file})")
                try:
                    path = self.export_gltf(ref, dest, status, textures=textures, animations=False, strip_fx=True,
                                     prune_unused=False)
                    mdl = merger.load_model(path, ref.name)
                except Exception as e:             # noqa: BLE001
                    problems.append(f"'{ref.name}' could not be exported: {type(e).__name__}: {e}")
            models[key] = mdl
            return mdl

        def place(levels, depth: int, parent: int, label: str) -> None:
            nonlocal placed
            lv = levels[depth]
            mdl = model_for(lv)
            if mdl is None:
                if depth + 1 < len(levels):          # rack model missing: still show what it carries, once
                    place(levels, depth + 1, parent, label)
                return
            names = merger.instance(mdl, parent, f"{label}/{lv.stem}")
            names_root = len(merger.js["nodes"]) - 1                 # the wrapper node of this instance
            placed += 1
            if depth + 1 >= len(levels):
                return
            slots = [e.lower() for e in lv.emitters]
            if not slots:                              # a pod / launcher without emitter nodes: its model is complete
                return                                 # (the rockets inside are not separate objects to place)
            for i in range(max(1, lv.count)):
                node = names.get(slots[i % len(slots)])
                if node is None:
                    problems.append(f"'{lv.stem}' has no node '{slots[i % len(slots)]}' (placed at its origin)")
                    node = names_root
                place(levels, depth + 1, node, f"{label}/{i + 1}")

        self._pack_batch = set()                      # texture packs are read once for the whole loadout
        try:
            for slot, preset, m in mounts:
                parent = merger.find_node(m.emitter)
                if parent is None:
                    problems.append(f"vehicle has no node '{m.emitter}' (slot {slot} / {preset})")
                    continue
                place(m.levels, 0, parent, f"wpn_s{slot}_{m.emitter}")
            if merger.added:
                merger.save()
        finally:
            self._pack_batch = None
            AssetCacher.clearCache(DDSx)          # unload the textures once, after the last weapon
            shutil.rmtree(tmp_root, ignore_errors=True)
        status(f"  [weapons] {placed} weapon model(s) placed for {len(mounts)} mount(s)")
        for msg in dict.fromkeys(problems):
            status(f"  [weapons] ! {msg}")
        return placed

    def attach_mounts_obj(self, obj_file: Path, mounts, vehicle_world: dict, out_dir: Path, status: Status,
                          textures: bool = True) -> int:
        """OBJ twin of attach_mounts: append the weapon models of a loadout to the vehicle .obj. Returns models placed.

        An .obj has no node tree, so the emitter placement comes from the skeleton matrices (`vehicle_world`, lower node
        name -> world matrix in game model space) of the vehicle and of each rack / pod model. Each distinct model is
        exported once to a temporary folder as its own .obj and copied into the vehicle file for every use."""
        import shutil
        import numpy as np
        from parse.obj_merge import ObjMerger, load_obj_model, to_obj_space

        obj_file, out_dir = Path(obj_file), Path(out_dir)
        tmp_root = out_dir / "_weapon_models_tmp"
        merger = ObjMerger(obj_file, status)
        models: dict[str, object] = {}              # level key -> (ObjModel, {node: world matrix}) or None
        refs_cache: dict[str, Optional[AssetRef]] = {}
        placed, problems = 0, []

        def model_for(level):
            key = "|".join((level.stem, *level.hints)).lower()
            if key in models:
                return models[key]
            if key not in refs_cache:
                found = self.find_weapon_models(level.stem, list(level.hints))
                refs_cache[key] = found[0] if found else None
            ref = refs_cache[key]
            entry = None
            if ref is None:
                problems.append(f"no 3D model for '{level.stem}'" + (f" (BLK names {', '.join(level.hints)})" if level.hints else ""))
            else:
                dest = tmp_root / re.sub(r"[^\w.-]+", "_", ref.name)
                status(f"  [weapons] exporting {ref.name} ({ref.grp_file})")
                try:
                    path = self.export_obj(ref, dest, status, textures=textures, strip_fx=True, prune_unused=False)
                    nodes = dict(self.last_node_world)          # read right after the export: the next one replaces it
                    mdl = load_obj_model(path, re.sub(r"[^\w.-]+", "_", ref.name))
                    if mdl is None:
                        problems.append(f"'{ref.name}' exported no geometry")
                    else:
                        entry = (mdl, nodes)
                except Exception as e:             # noqa: BLE001
                    problems.append(f"'{ref.name}' could not be exported: {type(e).__name__}: {e}")
            models[key] = entry
            return entry

        def place(levels, depth: int, G: np.ndarray, label: str) -> None:
            """G: world matrix (game model space) of the emitter node this level hangs on."""
            nonlocal placed
            lv = levels[depth]
            entry = model_for(lv)
            if entry is None:
                if depth + 1 < len(levels):          # rack model missing: still show what it carries, once
                    place(levels, depth + 1, G, label)
                return
            mdl, nodes = entry
            merger.instance(mdl, to_obj_space(G), f"{label}/{lv.stem}")
            placed += 1
            if depth + 1 >= len(levels):
                return
            slots = [e.lower() for e in lv.emitters]
            if not slots:                              # a pod / launcher without emitter nodes: its model is complete
                return
            for i in range(max(1, lv.count)):
                name = slots[i % len(slots)]
                local = nodes.get(name)
                if local is None:
                    problems.append(f"'{lv.stem}' has no node '{name}' (placed at its origin)")
                    local = np.eye(4)
                place(levels, depth + 1, G @ local, f"{label}/{i + 1}")

        self._pack_batch = set()                      # texture packs are read once for the whole loadout
        try:
            for slot, preset, m in mounts:
                E = vehicle_world.get(m.emitter.lower())
                if E is None:
                    problems.append(f"vehicle has no node '{m.emitter}' (slot {slot} / {preset})")
                    continue
                place(m.levels, 0, E, f"wpn_s{slot}_{m.emitter}")
            if merger.added:
                merger.save()
        finally:
            self._pack_batch = None
            AssetCacher.clearCache(DDSx)          # unload the textures once, after the last weapon
            shutil.rmtree(tmp_root, ignore_errors=True)
        status(f"  [weapons] {placed} weapon model(s) placed for {len(mounts)} mount(s)")
        for msg in dict.fromkeys(problems):
            status(f"  [weapons] ! {msg}")
        return placed

    def all_models(self) -> list[AssetRef]:
        out = [AssetRef(n, g, c) for lst in self.name_index.values() for n, g, c in lst if c in MODEL_CLASS_IDS]
        out.sort(key=lambda r: r.name.lower())
        return out

    # ------------------------------------------------------------------ manual overrides
    def _load_overrides(self) -> None:
        try:
            self.overrides = json.loads(self._overrides_path.read_text(encoding="utf-8"))
        except Exception:
            self.overrides = {}

    def _get_override(self, key: str) -> AssetRef | None:
        d = self.overrides.get(key)
        if not d:
            return None
        ref = AssetRef(d["name"], d["grp"], d["class_id"], "chosen manually")
        return ref if (self.game_dir / ref.grp).exists() else None

    def remember(self, key: str, ref: AssetRef) -> None:
        self.overrides[key] = {"name": ref.name, "grp": ref.grp, "class_id": ref.class_id}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._overrides_path.write_text(json.dumps(self.overrides, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ export
    def _ensure_descs(self, grp_rel: str, status: Status) -> None:
        parts = grp_rel.split("/")
        pkg_name = parts[1] if len(parts) > 1 else ""
        for rel in self.desc_files:
            rel_parts = rel.split("/")
            rel_pkg = rel_parts[1] if len(rel_parts) > 1 else ""
            if rel_pkg in ("base", pkg_name) or rel.startswith(parts[0] + "/"):
                if rel in self._descs_loaded:
                    continue
                status(f"Loading {rel} ...")
                self._descs_loaded.add(rel)
                try:
                    desc = GameResDesc(str(self.game_dir / rel))
                    desc.loadDataBlock()
                    AssetCacher.appendGameResDesc(desc)
                except Exception as e:
                    status(f"[warning] could not read {rel}: {e}. Materials may be unnamed.")

    def preload_base_descs(self, status: Status) -> None:
        self._ensure_descs("content/base/res/x.grp", status)

    def _load_texture_packs(self, mdl, status: Status) -> None:
        wanted: set[str] = set()
        for mat in (getattr(mdl, "materials", None) or []):
            for tex in (getattr(mat, "textures", None) or []):
                if tex:
                    cleaned = _clean_tex_name(tex)
                    if cleaned:
                        wanted.add(cleaned)
            
            # Also extract texture names from dynamic/camo shader parameters (t1, camo_tex, etc.)
            try:
                mat_params = mat.getParams() if hasattr(mat, "getParams") else {}
                for p_key in ("camo_tex", "t1", "t0", "t2", "t3", "t4", "t5"):
                    p_val = mat_params.get(p_key)
                    if p_val:
                        cleaned = _clean_tex_name(p_val)
                        if cleaned:
                            wanted.add(cleaned)
            except Exception:
                pass

        packs: dict[str, None] = {}
        missing = 0
        for t in wanted:
            where = self.tex_index.get(t)
            if where:
                for d in where:
                    packs[d] = None
            else:
                missing += 1
        batch = self._pack_batch
        if batch is not None:
            # weapon batch: the packs stay in memory from one weapon to the next (they are cleared once, when the batch ends)
            if set(packs) <= batch:
                status(f"Model uses {len(wanted)} textures -> {len(packs)} texture pack(s) already loaded"
                       + (f" ({missing} not found in any pack)" if missing else ""))
                return
            packs = {k: None for k in (batch | set(packs))}     # reload the union so HQ packs still load last
        status(f"Model uses {len(wanted)} textures -> loading {len(packs)} texture pack(s)"
               + (f" ({missing} not found in any pack)" if missing else ""))
        AssetCacher.clearCache(DDSx)

        # Sort so standard packs load FIRST and HQ packs load LAST (HQ DDSx textures overwrite standard ones in memory)
        sorted_pack_paths = sorted(packs.keys(), key=lambda p: 1 if "content.hq" in p.lower() else 0)

        for rel in sorted_pack_paths:
            log.setMuted(True)
            try:
                pack = DDSxTexturePack2(str(self.game_dir / rel))
                for ddsx in pack.getPackedFiles():
                    AssetCacher.cacheAsset(ddsx)
            finally:
                log.setMuted(False)
            status(f"  texture pack {rel.rsplit('/', 1)[-1]}")
            if batch is not None:
                batch.add(rel)

    @staticmethod
    def _prune_unused_textures(model_file, out_dir, status: Status) -> None:
        """Last step of an export: remove the materials and texture files the finished model does not use."""
        try:
            from parse.tex_prune import prune_obj_materials, prune_glb_materials, prune_textures_dir
            if Path(model_file).suffix.lower() == ".obj":
                prune_obj_materials(model_file, status)
            else:
                prune_glb_materials(model_file, status)
            prune_textures_dir(out_dir, status)
        except Exception as e:          # never lose the exported model because of the cleanup
            status(f"[WARNING] texture cleanup skipped: {type(e).__name__}: {e}")

    @staticmethod
    def _skeleton_world(mdl) -> dict:
        """lower node name -> 4x4 world matrix (game model space) of every skeleton node of a model; {} without skeleton."""
        import numpy as np
        out: dict = {}
        sk = getattr(mdl, "skeleton", None)
        if sk is None:
            return out
        try:
            for node in sk.getNodes():
                m = np.array([[float(node.wtm[r][c]) for c in range(4)] for r in range(4)], dtype=float)
                out.setdefault(str(node.name).lower(), m)
        except Exception:                          # noqa: BLE001 - a broken skeleton only disables weapon placement
            return {}
        return out

    def _open_model(self, ref: AssetRef, status: Status, lod: int = 0, geometry: bool = True):
        """Load one model the way export_obj does (skeleton / collision cached, LOD built) and return the Model.
        Used by export_skin_template; export_obj / export_gltf keep their own copy of this block.

        geometry=False skips building the mesh and returns the loaded resource itself, which already carries
        ``materials`` (and their texture names). That is all a skin template needs, and it avoids building the
        vertex data of big models (an F-16 is ~440k vertices, enough to run out of memory)."""
        log.subLevel(log.curLevel)
        grp_path = self.game_dir / ref.grp
        if not grp_path.exists():
            raise FileNotFoundError(f"GRP file not found: {grp_path}")

        self._ensure_descs(ref.grp, status)
        status(f"Loading {ref.grp_file} ...")

        for cls in _CACHED_TYPES:
            AssetCacher.clearCache(cls)
        
        self._preload_companions(ref.name, status)

        target = None
        ref_name_lower = ref.name.lower()

        # --- FAST-PATH: Offset cache lookup with Skeleton / Collision Caching ---
        entries = self.grp_entries.get(ref.grp, [])
        cached_meta = None
        wanted = self._companion_names(ref.name)       # only this model's own skeleton / collision, not the whole .grp

        for item in entries:
            item_name = item["name"] if isinstance(item, dict) else item[0]
            item_cid = item["class_id"] if isinstance(item, dict) else item[1]

            # Ensure GeomNodeTree, CollisionGeom, etc. inside this .grp are cached for model computation
            if item_cid in REALRES_CLASSES_DICT:
                cls_type = REALRES_CLASSES_DICT[item_cid]
                if cls_type in _CACHED_TYPES and item_name.lower() in wanted:
                    try:
                        res_obj = cls_type(
                            filePath=str(grp_path),
                            name=item_name,
                            size=item["size"] if isinstance(item, dict) else 0,
                            offset=item["offset"] if isinstance(item, dict) else 0
                        )
                        AssetCacher.cacheAsset(res_obj)
                    except Exception:
                        pass

            if item_name.lower() == ref_name_lower:
                cached_meta = item

        if cached_meta:
            cid = cached_meta["class_id"] if isinstance(cached_meta, dict) else cached_meta[1]
            if cid in REALRES_CLASSES_DICT:
                try:
                    cls_type = REALRES_CLASSES_DICT[cid]
                    target = cls_type(
                        filePath=str(grp_path),
                        name=cached_meta["name"] if isinstance(cached_meta, dict) else cached_meta[0],
                        size=cached_meta["size"] if isinstance(cached_meta, dict) else 0,
                        offset=cached_meta["offset"] if isinstance(cached_meta, dict) else 0
                    )
                except Exception:
                    target = None

        # --- FALLBACK: Direct .grp container scanning ---
        if target is None:
            pack = GameResourcePack(str(grp_path))
            assets = pack.getPackedFiles()

            for a in assets:
                if type(a) in _CACHED_TYPES:
                    AssetCacher.cacheAsset(a)
                a_name = getattr(a, "name", None) or getattr(a, "resName", None)
                if a_name and str(a_name).lower() == ref_name_lower:
                    target = a

            if target is None:
                for a in assets:
                    a_name = getattr(a, "name", None) or getattr(a, "resName", None)
                    if a_name and (ref_name_lower in str(a_name).lower() or str(a_name).lower() in ref_name_lower):
                        target = a
                        break

            if target is None:
                models_in_grp = [a for a in assets if isinstance(a, (DynModel, RendInst))]
                if len(models_in_grp) == 1:
                    target = models_in_grp[0]
                else:
                    raise LookupError(f"'{ref.name}' is not a model in {ref.grp_file}")

        status(f"Reading {target.__class__.__name__} '{getattr(target, 'name', ref.name)}' ...")
        target.computeData()
        if not geometry:
            target.generateMaterials()
            return target
        if lod >= target.lodCount:
            lod = 0
        status(f"Building LOD {lod} of {target.lodCount} ...")
        mdl = target.getModel(lod)
        return mdl

    @staticmethod
    def _dds_to_tga(dds: Path, dst: Path, tmp: Path, rgb=None, clear_alpha: bool = False) -> None:
        """Convert a .dds to an uncompressed 32-bit .tga (the format the game reads from UserSkins).
        rgb: optional replacement colour (h, w, 3), the camo-baked picture.
        clear_alpha: write alpha 0 everywhere. On tanks / ships the diffuse's alpha is the mask the game paints its camo
        through (white = camo goes here), so black alpha makes the game show the texture as it is, with no camo on top."""
        import numpy as np
        from PIL import Image
        arr = _loadDdsArray(str(dds), str(tmp))
        if arr is None:
            raise RuntimeError(f"could not decode {Path(dds).name}")
        arr = np.array(arr, dtype=np.uint8)
        if rgb is not None and rgb.shape[:2] == arr.shape[:2]:
            arr[..., :3] = rgb
        if clear_alpha:
            arr[..., 3] = 0
        Image.fromarray(np.ascontiguousarray(arr), "RGBA").save(str(dst), format="TGA")

    def _skin_details(self, mat, tex: str, skin_dir: Path, work: Path) -> int:
        """Details layers (panel lines, rivets) of one texture of the skin template, written into <skin folder>/Details (wtx/skin_details.py).
        _n: the normal map as a detail layer. _c: a cavity map computed from the material's _n, and the fine detail of the colour texture.
        Returns the number of files written. These files are rebuilt on every export (they are layers to paste, not files to edit)."""
        from wtx import skin_details as sd
        low = tex.lower()
        if not low.endswith(("_n", "_c")):
            return 0

        def load(name):
            dds = work / "textures" / f"{name}.dds"
            if not dds.exists():
                mat.exportTexture(name, str(work), True)
            return _loadDdsArray(str(dds), str(work)) if dds.exists() else None

        out_dir = Path(skin_dir) / sd.DETAILS_DIR
        if low.endswith("_n"):
            arr = load(tex)
            return sd.write_normal_details(out_dir, tex, arr) if arr is not None else 0
        rgb = load(tex)
        if rgb is None:
            return 0
        normal = getattr(mat, "normal", None)
        normal_arr = None
        if normal:
            n_name = str(normal).split("*")[0]
            n_name = n_name[:-4] if n_name.lower().endswith(".dds") else n_name
            normal_arr = load(n_name)
        return sd.write_colour_details(out_dir, tex, rgb, normal_arr)

    def export_skin_template(self, ref: AssetRef, skin_dir: Path, vehicle_id: str, status: Status,
                             weapon_levels=(), lod: int = 0, vehicle_name: str = "") -> tuple[Path, int]:
        """Same as _export_skin_template_impl, with the camo the vehicle's BLK swaps in (see _vehicle_camo)."""
        with self._vehicle_camo(status, fixed_skin=False):   # a template keeps the game's own textures
            return self._export_skin_template_impl(ref, skin_dir, vehicle_id, status, weapon_levels=weapon_levels, lod=lod, vehicle_name=vehicle_name)

    def _export_skin_template_impl(self, ref: AssetRef, skin_dir: Path, vehicle_id: str, status: Status,
                                   weapon_levels=(), lod: int = 0, vehicle_name: str = "") -> tuple[Path, int]:
        """Write a custom-skin folder for a vehicle: every albedo (_c) and normal (_n) texture its model (and the
        weapon models of the chosen loadout) uses, as .tga files with the game's own names (tanks and ships get their camo baked in, like the OBJ / GLB export), plus <vehicle id>.blk
        that maps each game texture to its file. Edit the files, copy the folder into War Thunder/UserSkins.

        The textures are written exactly as the game stores them (normal maps keep the game's channel layout, nothing
        is re-swizzled for Blender), so what you paint is what the game reads. Files already in the folder are never
        overwritten, so running this again keeps your edits.
        Next to the textures it writes <guide folder>/<texture>_uv.png (the UV island outlines of every colour texture) and INSTRUCTIONS.txt (how the .blk and the alpha channel work, fixes).
        weapon_levels: MountLevel objects (stem + hints) whose models should be included. Returns (.blk path, texture count)."""
        import shutil
        import tempfile
        from wtx import uv_guide
        from wtx import skin_details
        from wtx.skin_blk import SKIN_EXT, skin_textures, write_blk
        from wtx.skin_docs import instructions_text
        from parse.material import bakeCamoPairs, getCamoPair

        skin_dir = Path(skin_dir)
        skin_dir.mkdir(parents=True, exist_ok=True)

        jobs = [(ref, ref.name)]
        seen = set()
        for lv in weapon_levels:
            key = "|".join((lv.stem, *lv.hints)).lower()
            if key in seen:
                continue
            seen.add(key)
            found = self.find_weapon_models(lv.stem, list(lv.hints))
            if found:
                jobs.append((found[0], found[0].name))
            else:
                status(f"  [skin] no 3D model found for weapon '{lv.stem}' - its textures are not included")

        names: list[str] = []
        done_refs = set()
        details_written = 0
        _det = Path(skin_dir) / skin_details.DETAILS_DIR
        if _det.is_dir():                                  # layers are rebuilt every time: drop files of older versions (e.g. *_detail_cavity.png)
            for _old in _det.iterdir():
                if _old.is_file():
                    try:
                        _old.unlink()
                    except OSError:
                        pass
        work = Path(tempfile.mkdtemp(prefix="wt_skin_"))
        try:
            for model_ref, label in jobs:
                if (model_ref.grp, model_ref.name) in done_refs:
                    continue
                done_refs.add((model_ref.grp, model_ref.name))
                try:
                    mdl = self._open_model(model_ref, status, lod, geometry=False)   # materials only, no mesh
                    self._load_texture_packs(mdl, status)
                except Exception as e:                        # noqa: BLE001 - one broken weapon must not lose the rest
                    status(f"  [skin] ! could not read {label}: {type(e).__name__}: {e}")
                    continue
                wanted_here = []
                camo_of: dict[str, str] = {}              # diffuse -> camo texture (tanks / ships: painted by mask)
                for mat in (getattr(mdl, "materials", None) or []):
                    try:
                        diffuse, camo = getCamoPair(mat)
                    except Exception:                     # noqa: BLE001
                        diffuse, camo = None, None
                    if diffuse and camo:
                        camo_of.setdefault(diffuse, camo)
                    for tex in skin_textures(mat.getTextureSlots()):
                        if tex not in names:
                            names.append(tex)
                            wanted_here.append((mat, tex))
                for mat, tex in wanted_here:
                    target = skin_dir / f"{tex}{SKIN_EXT}"
                    if target.exists():
                        status(f"    kept your existing {target.name}")
                        continue
                    mat.exportTexture(tex, str(work), True)
                    got = work / "textures" / f"{tex}.dds"
                    if not got.exists():
                        continue
                    baked_rgb = None
                    camo = camo_of.get(tex)
                    if camo:                              # same camo bake as the OBJ / GLB export
                        try:
                            mat.exportTexture(camo, str(work), True)
                            baked = f"{tex}__{camo}"
                            bakeCamoPairs(str(work / "textures"), {baked: (tex, camo)})
                            arr = _loadDdsArray(str(work / "textures" / f"{baked}.dds"), str(work))
                            if arr is not None:
                                baked_rgb = arr[..., :3]
                                status(f"    {tex}: camo '{camo}' baked in")
                        except Exception as e:            # noqa: BLE001 - fall back to the plain texture
                            status(f"  [skin] ! camo bake failed for {tex}: {type(e).__name__}: {e}")
                    try:
                        self._dds_to_tga(got, target, work, baked_rgb, clear_alpha=bool(camo))
                    except Exception as e:                # noqa: BLE001
                        status(f"  [skin] ! could not write {target.name}: {type(e).__name__}: {e}")

                # Details layers (panel lines, rivets): one pass per model, a failure only loses these files.
                for mat, tex in wanted_here:
                    try:
                        details_written += self._skin_details(mat, tex, skin_dir, work)
                    except Exception as e:                # noqa: BLE001
                        status(f"  [skin] ! details for {tex} failed: {type(e).__name__}: {e}")

                # UV guides: need the built mesh, so a model too big for memory only loses these, never the textures.
                tris_by_tex = {}
                try:
                    geo = mdl.getModel(lod if lod < mdl.lodCount else 0)
                    tris_by_tex = uv_guide.collect(
                        geo, getattr(mdl, "materials", None), lambda m: skin_textures(m.getTextureSlots()))
                    geo = None
                except Exception as e:                    # noqa: BLE001 - MemoryError included
                    geo = None
                    status(f"  [skin] ! no UV guides for {label}: {type(e).__name__}: {e}")
                guided = 0
                for mat, tex in wanted_here:
                    guide = None
                    tris = None if tex.lower().endswith("_n") else tris_by_tex.get(tex)   # _n shares the _c layout
                    if tris:
                        size = uv_guide.texture_size(work / "textures" / f"{tex}.dds")
                        if size is None:                  # kept an existing skin file: take its size
                            try:
                                from PIL import Image as _PILImage
                                with _PILImage.open(skin_dir / f"{tex}{SKIN_EXT}") as _im:
                                    size = _im.size
                            except Exception:             # noqa: BLE001
                                size = None
                        try:
                            if uv_guide.draw_guide(tris, size, skin_dir / uv_guide.GUIDE_DIR / f"{tex}_uv.png",
                                                    on_skip=lambda n, t=tex: status(f"    {t}: left out {n} island(s) lying on top of other islands")):
                                guide = f"{uv_guide.GUIDE_DIR}/{tex}_uv.png"
                                guided += 1
                        except Exception as e:            # noqa: BLE001
                            status(f"  [skin] ! UV guide for {tex} failed: {type(e).__name__}: {e}")
                tris_by_tex = None
                AssetCacher.clearCache(DDSx)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        present = [n for n in names if (skin_dir / f"{n}{SKIN_EXT}").exists()]
        for n in names:
            if n not in present:
                status(f"  [skin] ! {n} was not found in the game files - left out of the .blk")
        if details_written:
            status(f"  Details: {details_written} file(s) in {skin_details.DETAILS_DIR}/ (panel lines and rivets to paste over your skin)")
        blk, fresh = write_blk(skin_dir, vehicle_id, present)
        if not fresh:
            status(f"  [skin] {vehicle_id}.blk already exists and was kept; the new one is {blk.name}")
        shown = vehicle_name or vehicle_id
        (skin_dir / "INSTRUCTIONS.txt").write_text(instructions_text(shown, vehicle_id, SKIN_EXT), encoding="utf-8")
        return blk, len(present)

    @contextlib.contextmanager
    def _vehicle_camo(self, status: Status, fixed_skin: bool = True):
        """For the duration of an export, the materials use the camo the selected vehicle's BLK swaps in (default_skin ->
        replace_tex), not the placeholder camo their material lists. Nothing changes when there is no vehicle or no swap.
        The [camo] / [net] lines of the export are printed together right before the end of the log."""
        from parse import material as _material
        from wtx.vehicle_camo import default_fixed_skin_swaps, default_camo_swaps, camouflage_net_nodes
        _material.DIAG_LINES.clear()
        diag = _material.DIAG_LINES
        _nodes = camouflage_net_nodes(self.vehicle_blk)
        _material.setNetNodes(_nodes)
        swaps = default_camo_swaps(self.vehicle_blk)
        # set_tex ... camo_skin_tex: a fixed skin picture the game lays over the body / turret / gun by its alpha (material.py).
        _material.setFixedSkinOverlays(default_fixed_skin_swaps(self.vehicle_blk) if fixed_skin else {})
        if swaps:
            _material.setVehicleTexSwaps(swaps)
        else:
            _material.clearVehicleTexSwaps()
        try:
            yield
        finally:
            _material.setNetNodes(set())
            _material.setFixedSkinOverlays({})
            if swaps:
                _material.clearVehicleTexSwaps()
            if diag:
                for _ln in diag:
                    status(_ln)
                diag.clear()

    @contextlib.contextmanager
    def _skin_textures(self, skin_dir, textures: bool, status: Status, gltf: bool = False):
        """Skin preview: for the duration of an export, the textures the custom-skin folder supplies are used as they are
        (material.SKIN_OVERRIDES): not extracted from the game, no camo baked into them, plain file names. Whatever the model
        needs that the skin does not supply (tracks, machine guns, nets, weapon models ...) is extracted from the game as usual."""
        if not skin_dir or not textures:
            yield
            return
        from parse import material as _material
        from wtx.skin_blk import read_skin_folder_report
        skin, skin_notes = read_skin_folder_report(Path(skin_dir))
        if not skin:
            status(f"[skin] no textures found in {skin_dir}")
            yield
            return
        for _n in skin_notes:
            status(f"[skin] {_n}")
        _material.setSkinOverrides(skin, gltf=gltf)
        try:
            yield
        finally:
            used = set(_material.SKIN_USED)
            _material.clearSkinOverrides()
            unused = sorted(k for k in skin if k not in used)
            if unused:
                status("[skin] not used by this model (not one of its textures): " + ", ".join(unused))

    def export_obj(self, ref: AssetRef, out_dir: Path, status: Status, textures: bool = True, lod: int = 0,
                   strip_fx: bool = True, hide_nodes=None, mounts=None, mirror_nodes=None,
                   prune_unused: bool = True, skin_dir=None) -> Path:
        """Same as _export_obj_impl; with skin_dir the custom skin's textures are used on the model (see _skin_textures)."""
        with self._vehicle_camo(status), self._skin_textures(skin_dir, textures, status):
            return self._export_obj_impl(ref, out_dir, status, textures=textures, lod=lod, strip_fx=strip_fx, hide_nodes=hide_nodes, mounts=mounts, mirror_nodes=mirror_nodes, prune_unused=prune_unused, skin_dir=skin_dir)

    def _export_obj_impl(self, ref: AssetRef, out_dir: Path, status: Status, textures: bool = True, lod: int = 0,
                   strip_fx: bool = True, hide_nodes=None, mounts=None, mirror_nodes=None,
                   prune_unused: bool = True, skin_dir=None) -> Path:
        """Export one model (with MTL and textures) to <out_dir>. Returns the .obj path.

        hide_nodes: names of pylon / launcher / pod objects to leave out (the loadout selection); None = keep everything.
        mounts: [(slot, preset, Mount)] weapon models to hang on the vehicle's emitter nodes (see attach_mounts_obj).
        mirror_nodes: vehicle pod groups to reflect to the other side of the jet (the game draws them there).
        prune_unused: at the end, drop the materials / texture files the finished .obj does not use (see tex_prune).
        skin_dir: a custom-skin folder whose textures replace the game's in the finished export (the skin preview)."""
        log.subLevel(log.curLevel)
        grp_path = self.game_dir / ref.grp
        if not grp_path.exists():
            raise FileNotFoundError(f"GRP file not found: {grp_path}")

        self._ensure_descs(ref.grp, status)
        status(f"Loading {ref.grp_file} ...")

        for cls in _CACHED_TYPES:
            AssetCacher.clearCache(cls)
        
        self._preload_companions(ref.name, status)

        target = None
        ref_name_lower = ref.name.lower()

        # --- FAST-PATH: Offset cache lookup with Skeleton / Collision Caching ---
        entries = self.grp_entries.get(ref.grp, [])
        cached_meta = None
        wanted = self._companion_names(ref.name)       # only this model's own skeleton / collision, not the whole .grp

        for item in entries:
            item_name = item["name"] if isinstance(item, dict) else item[0]
            item_cid = item["class_id"] if isinstance(item, dict) else item[1]

            # Ensure GeomNodeTree, CollisionGeom, etc. inside this .grp are cached for model computation
            if item_cid in REALRES_CLASSES_DICT:
                cls_type = REALRES_CLASSES_DICT[item_cid]
                if cls_type in _CACHED_TYPES and item_name.lower() in wanted:
                    try:
                        res_obj = cls_type(
                            filePath=str(grp_path),
                            name=item_name,
                            size=item["size"] if isinstance(item, dict) else 0,
                            offset=item["offset"] if isinstance(item, dict) else 0
                        )
                        AssetCacher.cacheAsset(res_obj)
                    except Exception:
                        pass

            if item_name.lower() == ref_name_lower:
                cached_meta = item

        if cached_meta:
            cid = cached_meta["class_id"] if isinstance(cached_meta, dict) else cached_meta[1]
            if cid in REALRES_CLASSES_DICT:
                try:
                    cls_type = REALRES_CLASSES_DICT[cid]
                    target = cls_type(
                        filePath=str(grp_path),
                        name=cached_meta["name"] if isinstance(cached_meta, dict) else cached_meta[0],
                        size=cached_meta["size"] if isinstance(cached_meta, dict) else 0,
                        offset=cached_meta["offset"] if isinstance(cached_meta, dict) else 0
                    )
                except Exception:
                    target = None

        # --- FALLBACK: Direct .grp container scanning ---
        if target is None:
            pack = GameResourcePack(str(grp_path))
            assets = pack.getPackedFiles()

            for a in assets:
                if type(a) in _CACHED_TYPES:
                    AssetCacher.cacheAsset(a)
                a_name = getattr(a, "name", None) or getattr(a, "resName", None)
                if a_name and str(a_name).lower() == ref_name_lower:
                    target = a

            if target is None:
                for a in assets:
                    a_name = getattr(a, "name", None) or getattr(a, "resName", None)
                    if a_name and (ref_name_lower in str(a_name).lower() or str(a_name).lower() in ref_name_lower):
                        target = a
                        break

            if target is None:
                models_in_grp = [a for a in assets if isinstance(a, (DynModel, RendInst))]
                if len(models_in_grp) == 1:
                    target = models_in_grp[0]
                else:
                    raise LookupError(f"'{ref.name}' is not a model in {ref.grp_file}")

        status(f"Reading {target.__class__.__name__} '{getattr(target, 'name', ref.name)}' ...")
        target.computeData()
        if lod >= target.lodCount:
            lod = 0
        status(f"Building LOD {lod} of {target.lodCount} ...")
        mdl = target.getModel(lod)
        self.last_node_world = self._skeleton_world(mdl)       # emitter nodes of this model, for weapon placement

        if textures:
            self._load_texture_packs(mdl, status)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        status(f"Writing OBJ to {out_dir} ...")
        mdl.exportObj(str(out_dir), textures, True)
        if self._pack_batch is None:                   # inside a weapon batch the textures stay loaded until the batch ends
            AssetCacher.clearCache(DDSx)
        obj_file = out_dir / f"{mdl.exportName}.obj"
        if strip_fx:                    # jet flames, gun fire, flares: game effects, useless (and ugly) in Blender
            try:
                from parse.fx_strip import strip_obj
                strip_obj(obj_file, status)
            except Exception as e:      # never lose the exported model because of the cleanup
                status(f"[WARNING] effect stripping skipped: {type(e).__name__}: {e}")
        if hide_nodes:                  # loadout: leave out the pylons / rails / pods the chosen loadout does not show
            try:
                from parse.loadout_filter import hide_nodes_obj
                hide_nodes_obj(obj_file, hide_nodes, status)
            except Exception as e:      # never lose the exported model because of the loadout filter
                status(f"[WARNING] loadout filter skipped: {type(e).__name__}: {e}")
        vehicle_world = self.last_node_world
        if mirror_nodes:                # targeting pods the game shows on the other side than the model file does
            try:
                from parse.loadout_filter import mirror_nodes_obj
                mirror_nodes_obj(obj_file, mirror_nodes, status)
            except Exception as e:      # never lose the exported model because of the mirroring
                status(f"[WARNING] pod mirroring skipped: {type(e).__name__}: {e}")
        if mounts:                      # loadout: hang the weapon models on the emitter nodes (after the hide step)
            try:
                self.attach_mounts_obj(obj_file, mounts, vehicle_world, out_dir, status, textures=textures)
            except Exception as e:      # never lose the exported model because of the weapons
                status(f"[WARNING] weapon models skipped: {type(e).__name__}: {e}")
        if prune_unused and textures:
            self._prune_unused_textures(obj_file, out_dir, status)
        return obj_file
    
    def export_gltf(self, ref: AssetRef, out_dir: Path, status: Status, textures: bool = True, lod: int = 0,
                    animations: bool = True, strip_fx: bool = True, hide_nodes=None, mounts=None,
                    mirror_nodes=None, prune_unused: bool = True, skin_dir=None) -> Path:
        """Same as _export_gltf_impl; with skin_dir the custom skin's textures are used on the model (see _skin_textures)."""
        with self._vehicle_camo(status), self._skin_textures(skin_dir, textures, status, gltf=True):
            return self._export_gltf_impl(ref, out_dir, status, textures=textures, lod=lod, animations=animations, strip_fx=strip_fx, hide_nodes=hide_nodes, mounts=mounts, mirror_nodes=mirror_nodes, prune_unused=prune_unused, skin_dir=skin_dir)

    def _export_gltf_impl(self, ref: AssetRef, out_dir: Path, status: Status, textures: bool = True, lod: int = 0,
                    animations: bool = True, strip_fx: bool = True, hide_nodes=None, mounts=None,
                    mirror_nodes=None, prune_unused: bool = True, skin_dir=None) -> Path:
        """Export one model (with textures, and its .a2d animations) to <out_dir> as a binary .glb file.

        hide_nodes: names of pylon / launcher / pod meshes to leave out (the loadout selection); None = keep everything.
        mounts: [(slot, preset, Mount)] weapon models to hang on the vehicle's emitter nodes (see attach_mounts).
        mirror_nodes: vehicle pod nodes to reflect to the other side of the jet (the game draws them there)."""
        from parse.gltf_conv import export_model_to_gltf

        log.subLevel(log.curLevel)
        grp_path = self.game_dir / ref.grp
        if not grp_path.exists():
            raise FileNotFoundError(f"GRP file not found: {grp_path}")

        self._ensure_descs(ref.grp, status)
        status(f"Loading {ref.grp_file} ...")

        for cls in _CACHED_TYPES:
            AssetCacher.clearCache(cls)
        
        self._preload_companions(ref.name, status)

        target = None
        ref_name_lower = ref.name.lower()
        entries = self.grp_entries.get(ref.grp, [])
        cached_meta = None
        wanted = self._companion_names(ref.name)       # only this model's own skeleton / collision, not the whole .grp

        for item in entries:
            item_name = item["name"] if isinstance(item, dict) else item[0]
            item_cid = item["class_id"] if isinstance(item, dict) else item[1]

            if item_cid in REALRES_CLASSES_DICT:
                cls_type = REALRES_CLASSES_DICT[item_cid]
                if cls_type in _CACHED_TYPES and item_name.lower() in wanted:
                    try:
                        res_obj = cls_type(
                            filePath=str(grp_path),
                            name=item_name,
                            size=item["size"] if isinstance(item, dict) else 0,
                            offset=item["offset"] if isinstance(item, dict) else 0
                        )
                        AssetCacher.cacheAsset(res_obj)
                    except Exception:
                        pass

            if item_name.lower() == ref_name_lower:
                cached_meta = item

        if cached_meta:
            cid = cached_meta["class_id"] if isinstance(cached_meta, dict) else cached_meta[1]
            if cid in REALRES_CLASSES_DICT:
                try:
                    cls_type = REALRES_CLASSES_DICT[cid]
                    target = cls_type(
                        filePath=str(grp_path),
                        name=cached_meta["name"] if isinstance(cached_meta, dict) else cached_meta[0],
                        size=cached_meta["size"] if isinstance(cached_meta, dict) else 0,
                        offset=cached_meta["offset"] if isinstance(cached_meta, dict) else 0
                    )
                except Exception:
                    target = None

        if target is None:
            pack = GameResourcePack(str(grp_path))
            assets = pack.getPackedFiles()
            for a in assets:
                if type(a) in _CACHED_TYPES:
                    AssetCacher.cacheAsset(a)
                a_name = getattr(a, "name", None) or getattr(a, "resName", None)
                if a_name and str(a_name).lower() == ref_name_lower:
                    target = a

        status(f"Reading {target.__class__.__name__} '{getattr(target, 'name', ref.name)}' ...")
        target.computeData()
        if lod >= target.lodCount:
            lod = 0
        mdl = target.getModel(lod)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if textures:
            self._load_texture_packs(mdl, status)
            mdl.exportTextures(str(out_dir), exportTexture=True, forceConvert=True)

        status(f"Writing GLTF/GLB to {out_dir} ...")
        glb_file = export_model_to_gltf(mdl, out_dir, export_textures=textures)

        if strip_fx:                    # jet flames, gun fire, flares: game effects, useless (and ugly) in Blender
            try:
                from parse.fx_strip import strip_glb
                strip_glb(glb_file, status)
            except Exception as e:      # never lose the exported model because of the cleanup
                status(f"[WARNING] effect stripping skipped: {type(e).__name__}: {e}")

        if hide_nodes:                  # loadout: leave out the pylons / rails / pods the chosen loadout does not show
            try:
                from parse.loadout_filter import hide_nodes_glb
                hide_nodes_glb(glb_file, hide_nodes, status)
            except Exception as e:      # never lose the exported model because of the loadout filter
                status(f"[WARNING] loadout filter skipped: {type(e).__name__}: {e}")

        if mirror_nodes:                # targeting pods the game shows on the other side than the model file does
            try:
                from parse.loadout_filter import mirror_nodes_glb
                mirror_nodes_glb(glb_file, mirror_nodes, status)
            except Exception as e:      # never lose the exported model because of the mirroring
                status(f"[WARNING] pod mirroring skipped: {type(e).__name__}: {e}")

        if animations:
            try:
                self._add_animations(ref, Path(glb_file), status)
            except Exception as e:      # never lose the exported model because of an animation problem
                status(f"[WARNING] animations skipped: {type(e).__name__}: {e}")

        if self._pack_batch is None:                   # inside a weapon batch the textures stay loaded until the batch ends
            AssetCacher.clearCache(DDSx)
        if mounts:                      # after the animations: the added nodes must not be matched by their tracks
            try:
                self.attach_mounts(Path(glb_file), mounts, out_dir, status, textures=textures)
            except Exception as e:      # never lose the exported vehicle because of the weapon models
                status(f"[WARNING] weapon models skipped: {type(e).__name__}: {e}")
        if prune_unused and textures:
            self._prune_unused_textures(glb_file, out_dir, status)
        return glb_file