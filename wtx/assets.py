"""Headless driver for the Dagor Asset Explorer backend.

What the DAE GUI does by hand ("open asset folder", pick a .grp, right-click -> Export to OBJ) is done here
in code:

  1. index        - read the header of every .grp (entry names + class ids) and every .dxp.bin (texture
                    names) once, and cache the result, so a vehicle can be found by name instantly
  2. resolve      - vehicle / weapon  ->  the DynModel (or RendInst) entry that holds its 3D model
  3. export       - load that .grp, make its skeleton / collision visible to the model, load only the
                    texture packs the model actually needs, then call DAE's own Model.exportObj()
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import util.log as log
from parse.gameres import GameResDesc, GameResourcePack
from parse.material import DDSx, DDSxTexturePack2
from parse.realres import CollisionGeom, DynModel, GeomNodeTree, RendInst
from util.assetcacher import AssetCacher

Status = Callable[[str], None]
Progress = Callable[[int, int], None]

DYNMODEL_ID = DynModel.staticClassId
RENDINST_ID = RendInst.staticClassId
MODEL_CLASS_IDS = {DYNMODEL_ID, RENDINST_ID}
_CACHED_TYPES = (GeomNodeTree, CollisionGeom, DynModel, RendInst)  # same set DAE keeps in AssetCacher
INDEX_VERSION = 3


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


class AssetLibrary:
    def __init__(self, game_dir: Path, cache_dir: Path):
        self.game_dir = Path(game_dir)
        self.cache_dir = Path(cache_dir)
        self.grp_entries: dict[str, list[list]] = {}       # grp rel -> [[name, classId], ...]
        self.dxp_names: dict[str, list[str]] = {}          # dxp rel -> [texture names]
        self.desc_files: list[str] = []                    # rel paths of *Desc.bin
        self.name_index: dict[str, list[tuple[str, str, int]]] = {}   # lower name -> [(name, grp, classId)]
        self.tex_index: dict[str, list[str]] = {}          # cleaned tex name -> [dxp rel]
        self._descs_loaded: set[str] = set()
        self.overrides: dict[str, dict] = {}
        self._overrides_path = self.cache_dir / "overrides.json"
        self._load_overrides()

    # ------------------------------------------------------------------ helpers
    def _rel(self, p: Path) -> str:
        return p.relative_to(self.game_dir).as_posix()

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
            res_dirs = [p / "res" for p in sorted(cdir.glob("*")) if (p / "res").is_dir()]
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
                        entries = [[n, c] for n, c in GameResourcePack(str(p)).getEntryList()]
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
            for name, cid in entries:
                self.name_index.setdefault(name.lower(), []).append((name, grp, cid))
        for dxp, names in self.dxp_names.items():
            for n in names:
                cleaned = _clean_tex_name(n)
                if cleaned:
                    self.tex_index.setdefault(cleaned, []).append(dxp)

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
                        models = [(n, c) for n, c in entries if c in MODEL_CLASS_IDS]
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
        packs: dict[str, None] = {}
        missing = 0
        for t in wanted:
            where = self.tex_index.get(t)
            if where:
                for d in where:
                    packs[d] = None
            else:
                missing += 1
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

    def export_obj(self, ref: AssetRef, out_dir: Path, status: Status, textures: bool = True, lod: int = 0) -> Path:
        """Export one model (with MTL and textures) to <out_dir>. Returns the .obj path."""
        log.subLevel(log.curLevel)
        grp_path = self.game_dir / ref.grp
        if not grp_path.exists():
            raise FileNotFoundError(f"GRP file not found: {grp_path}")

        self._ensure_descs(ref.grp, status)

        status(f"Loading {ref.grp_file} ...")
        pack = GameResourcePack(str(grp_path))
        assets = pack.getPackedFiles()

        for cls in _CACHED_TYPES:
            AssetCacher.clearCache(cls)

        target = None
        ref_name_lower = ref.name.lower()

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

        if textures:
            self._load_texture_packs(mdl, status)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        status(f"Writing OBJ to {out_dir} ...")
        mdl.exportObj(str(out_dir), textures, True)
        AssetCacher.clearCache(DDSx)
        return out_dir / f"{mdl.exportName}.obj"