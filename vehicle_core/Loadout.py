"""Loadout.py - the weapon slots of one vehicle, read from the vehicle's own BLK (no regex guessing).

Where the data is (flightmodels/<vehicle>.blk):

  WeaponSlots {
    HideNodes { node:t = "pylon1" ... }              every model node that is hidden by default
    WeaponSlot {
      index:i = 3
      WeaponPreset {
        name:t = "agm_65d"
        ShowNodes { node:t = "pylon2" ... }           nodes this choice makes visible
        HideNodes { node:t = "launcher_aim9_1" }      nodes this choice hides
        Weapon { trigger:t = ... blk:t = ".../us_agm_65d.blk" emitter:t = ... }
        DependentWeaponPreset { slot:i = 7  preset:t = "sniper_pod" }     "this choice also needs that one"
      }
    }
  }
  weapon_presets { preset { name:t  blk:t } }        the stock loadouts; each blk is a list of  Weapon { slot:i  preset:t }

The game hides the whole HideNodes list, then, for every chosen preset, shows its ShowNodes and hides its HideNodes.
That is all this module reproduces, so it only controls pylon / launcher / pod geometry that is INSIDE the vehicle
model. Missiles, bombs and tanks are separate models (`mesh:t` in the weapon BLK) the game attaches at the preset's
emitter nodes; they are not part of the vehicle model and are not handled here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Vehicle-model pod nodes that the game draws mirrored (see LoadoutData.mirrored_nodes)
MIRRORED_POD_RE = re.compile(r"lantirn|sniper|litening|optic\d", re.I)

# --------------------------------------------------------------------------------------------- BLK text parser
_BLOCK_RE = re.compile(r'^"?([^"{}:=\s]+)"?\s*\{\s*(\})?$')
_PAIR_RE = re.compile(r'^"?([^":=\s]+)"?\s*:\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*;?$')


def parse_blk(text: str) -> list:
    """BLK text -> [(key, value)] where a block's value is again such a list. Duplicate keys and order are kept."""
    root: list = []
    stack = [root]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "}":
            if len(stack) > 1:
                stack.pop()
            continue
        m = _BLOCK_RE.match(line)
        if m:
            if not m.group(2):                       # "name {}" on one line is an empty block: nothing to push
                blk: list = []
                stack[-1].append((m.group(1), blk))
                stack.append(blk)
            else:
                stack[-1].append((m.group(1), []))
            continue
        m = _PAIR_RE.match(line)
        if m:
            key, typ, val = m.groups()
            if typ == "t":
                val = val.strip().strip('"')
            elif typ in ("i", "r"):
                try:
                    val = int(val) if typ == "i" else float(val)
                except ValueError:
                    pass
            elif typ == "b":
                val = val.strip().lower() in ("true", "yes", "1")
            stack[-1].append((key, val))
    return root


def _get(blk, key, default=None):
    for k, v in blk or []:
        if k == key:
            return v
    return default


def _all(blk, key) -> list:
    return [v for k, v in blk or [] if k == key]


def _nodes(blk, key) -> tuple[str, ...]:
    return tuple(n for n in _all(_get(blk, key), "node") if isinstance(n, str) and n)


def _stem(path: str) -> str:
    s = path.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.blkx?$", "", s, flags=re.I).lower()


def _pretty(s: str) -> str:
    return s.replace("_", " ").strip()


# --- readable names for the stock loadouts ("aim_9m_default" -> "AIM-9M Default") ------------------------------
# Munition designators: letters + number are joined with a hyphen and upper-cased ("gbu 54b" -> "GBU-54B").
_DESIGNATORS = {
    "aim", "agm", "gbu", "blu", "lau", "cbu", "agr", "atm", "adm", "agm", "arm", "fab", "kab", "rbs", "rb", "kh",
    "ub", "zb", "rbk", "bk", "bl", "sc", "sd", "gb", "ls", "gp", "mc", "hot", "mica", "asm", "nsm", "jsm",
    "aam", "mbd", "taurus", "alarm", "sdb", "jdam", "ffar", "hvar", "apkws", "mm", "tv", "ks", "s", "r", "t",
}
# Words that are acronyms; everything else is simply capitalised.
_ACRONYMS = {"pids", "ecm", "he", "ap", "hei", "gps", "ir", "sead", "ffar", "hvar", "jdam", "eo", "ew", "tgp", "ecp",
             "atgm", "aa", "ag", "cas", "dme", "fl", "ir"}
_KEEP_LOWER = {"x"}


def _strip_vehicle_prefix(name: str, vehicle_id: str) -> str:
    """Remove the leading part of a preset name that repeats the vehicle id.

    Preset names start with the vehicle name, but not always with the whole id: vehicle 'fa_18a_raaf' has presets
    'fa_18a_default'.  So the longest run of leading id words (at least two, or the whole id when it is shorter)
    that the preset name starts with is cut off.
    """
    n, v = name.lower(), vehicle_id.lower().split("_")
    for k in range(len(v), 0, -1):
        if k == 1 and len(v) > 1:
            break                                  # one shared word ("f", "su") is too little to call a prefix
        pre = "_".join(v[:k]) + "_"
        if n.startswith(pre) and len(n) > len(pre):
            return n[len(pre):]
    return name


def pretty_stock_name(raw: str) -> str:
    """Turn a WeaponPreset id into a display name: 'gbu_54b_pids' -> 'GBU-54B PIDS', 'aim_9m_default' -> 'AIM-9M Default'."""
    toks: list[str] = []
    for part in re.split(r"[\s_]+", raw.strip()):
        # split glued letter/number tokens: 'agm154' -> 'agm', '154'; '2xaim9m' stays readable via the loop below
        toks.extend(t for t in re.findall(r"\d+[a-z]?|[a-z]+|\d+", part, flags=re.I) if t) if part else None
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if low in _DESIGNATORS and nxt[:1].isdigit():
            # designator + number (+ optional trailing letter variant): AIM-9M
            out.append(f"{t.upper()}-{nxt.upper()}")
            i += 2
            continue
        if low == "mk" and nxt[:1].isdigit():
            out.append(f"Mk {nxt.upper()}")
            i += 2
            continue
        if low == "x" and nxt.isdigit():                       # 'x 2' -> 'x2'
            out.append(f"x{nxt}")
            i += 2
            continue
        if len(low) == 1 and low.isalpha() and nxt[:1].isdigit():   # 'm 62' -> 'M62'
            out.append(f"{t.upper()}{nxt.upper()}")
            i += 2
            continue
        if re.fullmatch(r"\d+x", low):                        # '2x' stays '2x'
            out.append(low)
        elif low in _ACRONYMS:
            out.append(t.upper())
        elif t[:1].isdigit():
            out.append(t.upper())
        elif low in _KEEP_LOWER:
            out.append(low)
        else:
            out.append(t.capitalize())
        i += 1
    return " ".join(out) or raw


# --------------------------------------------------------------------------------------------- data model
@dataclass
class WeaponEntry:
    """One weapon of a preset, as the game's choice list shows it: name stem, ammo and mass."""
    stem: str                              # weapon BLK stem (leaf of a container chain)
    chapter: str                           # key of CHAPTERS
    ammo: int = 0
    mass_kg: float = 0.0                   # additional (container / pylon) mass + ammo * single-item mass


@dataclass(frozen=True)
class MountLevel:
    """One BLK of a weapon chain (rack -> pod -> rocket): the model it shows and where its contents hang."""
    stem: str                              # BLK stem
    hints: tuple[str, ...] = ()            # model / mesh names written in it (what the 3D model is called)
    emitters: tuple[str, ...] = ()         # nodes INSIDE this level's model where the next level's items hang
    count: int = 1                         # items of the next level it carries (bullets)


@dataclass(frozen=True)
class Mount:
    """One Weapon{} block with an emitter: the model chain the game attaches at a node of the vehicle."""
    emitter: str                           # node of the VEHICLE model the first level hangs on
    stem: str                              # leaf weapon BLK stem (the missile / bomb / tank itself)
    trigger: str = ""
    levels: tuple[MountLevel, ...] = ()    # outermost (container) first, the leaf weapon last


@dataclass
class PresetOption:
    name: str                              # WeaponPreset name, unique inside its slot
    label: str                             # text shown in the GUI
    show: tuple[str, ...] = ()
    hide: tuple[str, ...] = ()
    deps: tuple[tuple[int, str], ...] = ()  # (slot index, preset name) that this choice drags in
    weapons: tuple[str, ...] = ()          # BLK stems of the real weapons (countermeasures / guns left out)
    icon: str = ""                         # gameuiskin name of the cell / list icon (iconType, or the game's fallback)
    chapter: str = ""                      # key of CHAPTERS, the group this choice sits in
    entries: tuple[WeaponEntry, ...] = ()  # every weapon of the preset with ammo and mass
    req_mod: tuple[str, ...] = ()          # reqModification: needed to use it in the game
    bans: tuple[tuple[int, str], ...] = ()  # BannedWeaponPreset (slot index, preset name)
    mounts: tuple[Mount, ...] = ()         # the weapon models this choice puts on the vehicle (for the GLB export)

    @property
    def mass_kg(self) -> float:
        return sum(e.mass_kg for e in self.entries)

    @property
    def sort_key(self) -> str:
        """The choice list sorts by weapon id (the leaf BLK stem); a bundle sorts by its first weapon."""
        return self.entries[0].stem if self.entries else self.name

    @property
    def visual(self) -> bool:
        """True when choosing it changes anything in the vehicle model."""
        return bool(self.show or self.hide)


@dataclass
class Slot:
    index: int
    order: int = 0
    tier: Optional[int] = None             # cell position in the game's editor (ascending = left to right); None = the gun
    presets: list[PresetOption] = field(default_factory=list)   # all of them, including the ones with no model change
    centre: bool = False                   # notUseforDisbalanceCalculation

    @property
    def label(self) -> str:
        return f"Slot {self.index}"

    @property
    def options(self) -> list[PresetOption]:
        """What the GUI offers: every choice of the slot (the game lists them all, also the ones that change no node)."""
        return list(self.presets)

    @property
    def visual_options(self) -> list[PresetOption]:
        """Only the choices that change the vehicle model."""
        return [p for p in self.presets if p.visual]


@dataclass
class StockLoadout:
    name: str
    label: str
    slots: dict[int, str]                  # slot index -> preset name


@dataclass
class LoadoutData:
    base_hidden: frozenset                 # lower-case node names hidden when nothing is chosen
    slots: list[Slot]
    stock: list[StockLoadout] = field(default_factory=list)
    max_mass: float = 0.0                  # WeaponSlots.maxloadMass (0 = unknown)
    max_mass_left: float = 0.0
    max_mass_right: float = 0.0
    max_disbalance: float = 0.0

    @property
    def cells(self) -> list[Slot]:
        """The slots the game's editor draws, left to right (tier ascending). The gun has no tier and is not drawn."""
        return sorted((s for s in self.slots if s.tier is not None), key=lambda s: (s.tier, s.index))

    @property
    def has_choices(self) -> bool:
        """True when at least one drawn slot offers two or more presets, i.e. the panel is worth showing."""
        return any(len(s.presets) >= 2 for s in self.cells)

    def banned(self, selection: dict[int, str], slot: int, preset: str) -> bool:
        """True when `preset` in `slot` clashes with what other slots hold (BannedWeaponPreset, in either direction)."""
        me = self.option(slot, preset)
        for s, p in selection.items():
            if s == slot:
                continue
            other = self.option(s, p)
            if other and (slot, preset) in other.bans:
                return True
            if me and (s, p) in me.bans:
                return True
        return False

    def groups(self, slot: Slot) -> list[tuple[str, list[PresetOption]]]:
        """The choice list of a slot as the game prints it: [(chapter key, presets)], chapters in the game's order,
        presets sorted by weapon id inside a chapter."""
        out = []
        for ch in CHAPTERS:
            items = sorted((p for p in slot.presets if p.chapter == ch), key=lambda p: (p.sort_key, p.name))
            if items:
                out.append((ch, items))
        rest = [p for p in slot.presets if p.chapter not in CHAPTERS]
        if rest:
            out.append(("", sorted(rest, key=lambda p: (p.sort_key, p.name))))
        return out

    def total_mass(self, selection: dict[int, str]) -> float:
        tot = 0.0
        for s in self.cells:
            o = self.option(s.index, selection.get(s.index, "")) if selection.get(s.index) else None
            if o:
                tot += o.mass_kg
        return tot

    def __post_init__(self):
        self._by_key = {(s.index, p.name): p for s in self.slots for p in s.presets}

    def option(self, slot: int, preset: str) -> Optional[PresetOption]:
        return self._by_key.get((slot, preset))

    def hidden_nodes(self, selection: dict[int, str]) -> set[str]:
        """Lower-case node names to remove from the export for this selection ({slot index: preset name}).

        Nothing selected -> exactly the game's default HideNodes list, i.e. the jet without any pylon, pod or tank.
        """
        hidden = set(self.base_hidden)
        extra: set[str] = set()
        for slot, preset in selection.items():
            opt = self.option(slot, preset)
            if not opt:
                continue
            hidden -= {n.lower() for n in opt.show}
            extra |= {n.lower() for n in opt.hide}
        return hidden | extra

    def mirrored_nodes(self, selection: dict[int, str]) -> set[str]:
        """Lower-case names of vehicle pod nodes the game shows on the OTHER side of the jet than the model file does.

        The F-16's targeting pods (LANTIRN / Litening / Sniper) exist in the model only on the side of the AN/ASQ-213
        HARM pod; the game mirrors them. Only the nodes a chosen preset shows are returned."""
        out: set[str] = set()
        for slot, preset in selection.items():
            opt = self.option(slot, preset)
            if opt:
                out |= {n.lower() for n in opt.show if MIRRORED_POD_RE.search(n)}
        return out

    def choose(self, selection: dict[int, str], slot: int, preset: Optional[str]) -> dict[int, str]:
        """New selection after setting `slot` to `preset` (None = empty), with the game's DependentWeaponPreset rules.

        A choice that needs a companion pulls it in (GBU-12 needs the targeting pod, a wing tank needs the other wing
        tank). Removing a choice also removes a companion that points straight back at it (the tank pair). A dependency
        that names a preset the target slot does not have is ignored.
        """
        sel = dict(selection)

        def drop(s: int):
            old = sel.pop(s, None)
            o = self.option(s, old) if old else None
            for ds, dp in (o.deps if o else ()):
                back = self.option(ds, dp)
                if sel.get(ds) == dp and back and (s, old) in back.deps:
                    drop(ds)

        def put(s: int, p: str):
            if sel.get(s) == p:
                return
            drop(s)
            sel[s] = p
            o = self.option(s, p)
            for ds, dp in (o.deps if o else ()):
                if self.option(ds, dp) is not None:
                    put(ds, dp)

        if preset:
            put(slot, preset)
        else:
            drop(slot)
        return sel

    def mounts(self, selection: dict[int, str]) -> list[tuple[int, str, Mount]]:
        """(slot, preset, Mount) of every weapon model the selection puts on the vehicle, in slot order."""
        out = []
        for s in self.cells:
            name = selection.get(s.index)
            opt = self.option(s.index, name) if name else None
            for m in (opt.mounts if opt else ()):
                out.append((s.index, name, m))
        return out

    def stock_selection(self, name: str) -> dict[int, str]:
        for s in self.stock:
            if s.name == name:
                return dict(s.slots)
        return {}


# --------------------------------------------------------------------------------------------- chapters / icons
# editweaponrypreset.nut WEAPON_CHAPTERS_ORDER; the titles are what the game's choice list prints above each group
CHAPTERS = ("bombs", "guided_bombs", "torpedoes", "rockets", "atgm", "aam",
            "countermeasures", "additional_guns", "targetingPod", "fuel_tanks")
CHAPTER_TITLES = {
    "bombs": "Bombs", "guided_bombs": "Guided Bombs", "torpedoes": "Torpedoes", "rockets": "Rockets",
    "atgm": "Air to Ground Missiles", "aam": "Air to Air Missiles", "countermeasures": "Countermeasures",
    "additional_guns": "Additional Guns", "targetingPod": "Targeting Pods and Equipment", "fuel_tanks": "Fuel Tanks",
}
# weaponryinfo.nut triggerTypeToLoc
_TRIGGER_CHAPTER = {
    "bombs": "bombs", "mines": "bombs", "guided bombs": "guided_bombs", "torpedoes": "torpedoes",
    "rockets": "rockets", "agm": "atgm", "atgm": "atgm", "aam": "aam",
    "flares": "countermeasures", "chaffs": "countermeasures", "countermeasures": "countermeasures",
    "fuel tanks": "fuel_tanks", "boosters": "targetingPod", "container_item": "targetingPod",
    "targetingpod": "targetingPod", "add gun": "additional_guns", "machine gun": "additional_guns",
    "cannon": "additional_guns", "gunner1": "additional_guns", "gunner2": "additional_guns",
}
_NO_BULLET_PARAMS = ("smoke", "flares", "chaffs", "countermeasures")
# config/gui.blk weaponrySizes: (id, max kg); only used for the icon of a preset that has no iconType
DEFAULT_SIZES = (("small", 99.0), ("middle", 250.0), ("large", 500.0), ("special", 1000.0), ("heavy", 10000.0))


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def fmt_kg(v: float) -> str:
    """Mass the way the game prints it: one decimal, halves rounded up (1079.55 -> 1079.6), no trailing .0."""
    from decimal import Decimal, ROUND_HALF_UP
    d = Decimal(repr(round(float(v), 4))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    t = format(d, "f")
    return t[:-2] if t.endswith(".0") else t


def _weapon_size(mass_kg: float, sizes=DEFAULT_SIZES) -> str:
    for sid, mx in sorted(sizes, key=lambda x: x[1]):
        if mass_kg <= mx:
            return sid
    return ""


def _chapter_of(trigger: str, blk, edit_chapter: str = "") -> str:
    if edit_chapter:
        return edit_chapter
    t = (trigger or "").lower()
    ch = _TRIGGER_CHAPTER.get(t)
    if ch:
        return ch
    if _get(blk, "container") or _get(blk, "mesh") and not _get(blk, "bombGun") and not _get(blk, "rocketGun"):
        return "targetingPod"
    return "additional_guns"


def tier_icon(icon_type: Optional[str], trigger: str, mass_kg: float, items: int, per_tier: int = 0,
              sizes=DEFAULT_SIZES) -> str:
    """getTierIcon() of weaponrypresetsparams.nut: iconType when set, else the icon the game derives from trigger and size."""
    if icon_type:
        return icon_type
    t = (trigger or "").lower()
    group = items > 1
    block = per_tier > 1
    if t == "torpedoes":
        return "air_torpedo"
    if t == "atgm":
        return "atgm_type2_x2"
    if t in ("machine gun", "cannon", "add gun"):
        return "multibarrel_gun" if block or group else "machine_gun"
    if t == "agm":
        return "missile_air_to_earth"
    if t == "aam":
        return "missile_air_to_air_group" if block or group else "missile_air_to_air"
    if t in ("flares", "chaffs"):
        return "ltc"
    size = _weapon_size(mass_kg, sizes)
    w = "nar" if block and t == "rockets" else t
    return f"{w}_{size}_group" if block or group else f"{w}_{size}"


# --------------------------------------------------------------------------------------------- weapon mass / ammo
def _leaf_weapon(resolve_blk, cache: dict, path: str, bullets_count: int = 1, containers: int = 1,
                 container_mass: float = 0.0, depth: int = 0):
    """getWeaponBlkParams(): follow container -> blk chains. Returns (leaf blk, bullets_count, container_mass_kg)."""
    blk = _read_weapon_blk(resolve_blk, cache, path)
    bc = bullets_count * int(_num(_get(blk, "bullets"), 1) or 1)
    if blk is not None and _get(blk, "container") and isinstance(_get(blk, "blk"), str) and depth < 8:
        cm = container_mass + _num(_get(blk, "mass")) * containers
        return _leaf_weapon(resolve_blk, cache, _get(blk, "blk"), bc, int(_num(_get(blk, "bullets"), 1) or 1), cm, depth + 1)
    return blk, bc, container_mass


def _read_weapon_blk(resolve_blk, cache: dict, path: str):
    key = path.replace("\\", "/").lower()
    if key in cache:
        return cache[key]
    blk = None
    if resolve_blk:
        try:
            f = resolve_blk(path)
            if f:
                blk = parse_blk(Path(f).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            blk = None
    cache[key] = blk
    return blk


def _preset_entries(preset_blk, resolve_blk, cache: dict, sizes) -> tuple[list[WeaponEntry], str, str]:
    """Walk the Weapon{} blocks of a preset like addSlotWeaponsFromBlk / addWeaponsFromBlk / calcWeaponWeight.

    Returns (entries, chapter of the first entry, trigger of the first entry)."""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for w in _all(preset_blk, "Weapon"):
        blk_path = _get(w, "blk")
        if not isinstance(blk_path, str) or not blk_path or _get(w, "dummy"):
            continue
        if _get(w, "triggerGroup") == "commander" and _get(w, "bullets") is None:
            continue
        key = tuple(str(_get(w, k, "")).lower() for k in ("blk", "dm", "flash", "emitter"))
        if key in merged:                     # the game folds identical weapons together: bullets are added
            merged[key]["bullets"] = (merged[key]["bullets"] if merged[key]["bullets"] is not None else 1) + \
                                     (int(_num(_get(w, "bullets"), 1)) if _get(w, "bullets") is not None else 1)
            continue
        merged[key] = {"w": w, "bullets": int(_num(_get(w, "bullets"))) if _get(w, "bullets") is not None else None}
        order.append(key)
    out: list[WeaponEntry] = []
    first_trigger = ""
    for key in order:
        w, bullets = merged[key]["w"], merged[key]["bullets"]
        trig = str(_get(w, "trigger", "") or "").lower()
        blk, bcount, cmass = _leaf_weapon(resolve_blk, cache, _get(w, "blk"))
        ammo = bullets if bullets is not None else bcount
        additional = _num(_get(w, "mass"), _num(_get(blk, "mass"))) + cmass
        single = 0.0
        is_gun = False
        if blk is not None:
            payload = _get(blk, "payload")
            if payload:
                single = _num(_get(payload, "mass"))
            if trig == "countermeasures":
                single = _num(_get(blk, "mass"))
            if trig not in _NO_BULLET_PARAMS:
                tag = ("rocket" if _get(blk, "rocketGun") else "bomb" if _get(blk, "bombGun")
                       else "torpedo" if _get(blk, "torpedoGun") else "bullet")
                item = _get(blk, tag)
                if item:
                    single = _num(_get(item, "mass"), single)
                is_gun = tag == "bullet" and trig in ("machine gun", "cannon", "add gun")
        mass = additional + (0 if is_gun else ammo) * single
        # the name shown is the leaf weapon (the container chain's last blk), as the game's loc("weapons/<id>")
        leaf = _leaf_stem(resolve_blk, cache, _get(w, "blk"))
        stem = _stem(_get(w, "blk"))
        edit = str(_get(w, "editPresetChapter", "") or "")
        out.append(WeaponEntry(leaf or stem, _chapter_of(trig, blk, edit), int(ammo), round(mass, 3)))
        if not first_trigger:
            first_trigger = trig
    # the game sorts a slot's weapons by id and glues entries of one preset onto the first one: a bundle of
    # countermeasures + a bomb is listed under Countermeasures because "countermeasure_..." sorts before "us_..."
    out.sort(key=lambda e: e.stem)
    chapter = out[0].chapter if out else ""
    return out, chapter, first_trigger


def _leaf_stem(resolve_blk, cache: dict, path: str) -> str:
    cur, seen = path, 0
    while seen < 8:
        blk = _read_weapon_blk(resolve_blk, cache, cur)
        nxt = _get(blk, "blk") if blk is not None and _get(blk, "container") else None
        if not isinstance(nxt, str):
            return _stem(cur)
        cur, seen = nxt, seen + 1
    return _stem(cur)


# --------------------------------------------------------------------------------------------- mounts
_MODEL_KEY = re.compile(r"(model|mesh)", re.I)
_NO_MODEL_TRIGGERS = ("countermeasures", "flares", "chaffs", "cannon", "machine gun", "add gun", "commander")


def _model_hints(blk, depth: int = 0) -> list[str]:
    """model / mesh names written in a weapon BLK (top level first), as the app's weapon_model_hints reads them."""
    out: list[str] = []
    for k, v in blk or []:
        if isinstance(v, str) and _MODEL_KEY.search(k):
            st = re.sub(r"\.(blk|blkx|dyn|ri)$", "", v.replace("\\", "/").rsplit("/", 1)[-1], flags=re.I)
            if st and st not in out:
                out.append(st)
    if depth < 2:
        for k, v in blk or []:
            if isinstance(v, list):
                out += [h for h in _model_hints(v, depth + 1) if h not in out]
    return out


def _preset_mounts(preset_blk, resolve_blk, cache: dict) -> tuple[Mount, ...]:
    """Every Weapon{} block of a preset that has an emitter and a real weapon behind it.

    A container BLK (container:b = yes) carries `mesh` (its own model), `emitter` lines (nodes inside that model where
    its contents hang), `bullets` (how many) and `blk` (the contents). The chain is followed down to the leaf weapon."""
    out: list[Mount] = []
    for w in _all(preset_blk, "Weapon"):
        blk_path, emitter = _get(w, "blk"), _get(w, "emitter")
        trig = str(_get(w, "trigger", "") or "").lower()
        if (not isinstance(blk_path, str) or not blk_path or not isinstance(emitter, str) or not emitter
                or _get(w, "dummy") or trig.startswith("gunner") or trig in _NO_MODEL_TRIGGERS
                or "dummy_weapon" in blk_path.lower()):
            continue
        levels: list[MountLevel] = []
        cur, seen = blk_path, 0
        while cur and seen < 8:
            blk = _read_weapon_blk(resolve_blk, cache, cur)
            nxt = _get(blk, "blk") if blk is not None and _get(blk, "container") else None
            nxt = nxt if isinstance(nxt, str) else ""
            ems = tuple(e for e in _all(blk, "emitter") if isinstance(e, str) and e)
            n = int(_num(_get(blk, "bullets"), 0)) or (int(_num(_get(w, "bullets"), 0)) if seen == 0 else 0)
            n = n or max(1, len(ems))
            hints = tuple(_model_hints(blk)) if not nxt else tuple(_model_hints([(k, v) for k, v in blk if not isinstance(v, list)]))
            levels.append(MountLevel(_stem(cur), hints, ems if nxt else (), n if nxt else 1))
            cur, seen = nxt, seen + 1
        # only an "empty_mesh" placeholder (the AN/AAQ-33's own geometry is a vehicle node) is not a model to place;
        # a weapon whose model shares the emitter's name (the ASQ-213 pod, the centre drop tank) still is one
        first = [h.lower() for h in levels[0].hints]
        if len(levels) == 1 and first and all(h.startswith("empty_mesh") for h in first):
            continue
        out.append(Mount(emitter, levels[-1].stem, trig, tuple(levels)))
    return tuple(out)


# --------------------------------------------------------------------------------------------- loading
_SKIP_TRIGGERS = ("countermeasures", "cannon")


def _weapon_stems(preset_blk) -> tuple[str, ...]:
    stems: list[str] = []
    for w in _all(preset_blk, "Weapon"):
        trig = str(_get(w, "trigger", "") or "").lower()
        blk = _get(w, "blk")
        if not blk or _get(w, "dummy") or trig.startswith("gunner") or trig in _SKIP_TRIGGERS:
            continue
        st = _stem(blk)
        if st and "dummy_weapon" not in st and st not in stems:
            stems.append(st)
    return tuple(stems)


def _label(name: str, stems: tuple[str, ...], name_for: Optional[Callable[[str], Optional[str]]]) -> str:
    parts: list[str] = []
    for st in stems:
        disp = None
        if name_for:
            try:
                disp = name_for(st)
            except Exception:
                disp = None
        disp = disp or _pretty(st)
        if disp not in parts:
            parts.append(disp)
    return " + ".join(parts) or _pretty(name)


def load_loadout(vehicle_blk, vehicle_id: str,
                 resolve_blk: Optional[Callable[[str], Optional[Path]]] = None,
                 name_for: Optional[Callable[[str], Optional[str]]] = None) -> Optional[LoadoutData]:
    """Read the slot data of a vehicle. Returns None when the vehicle has no slot with a visible choice.

    resolve_blk: game path ("gameData/FlightModels/weaponPresets/x.blk") -> file on disk; needed for the stock loadouts.
    name_for:    weapon BLK stem -> localized display name (or None).
    """
    try:
        text = Path(vehicle_blk).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if "WeaponSlots" not in text:
        return None
    root = parse_blk(text)
    ws = _get(root, "WeaponSlots")
    if not ws:
        return None

    base_hidden = frozenset(n.lower() for n in _nodes(ws, "HideNodes"))
    tiers_count = int(_num(_get(ws, "weaponsSlotCount"), 13)) or 13
    wcache: dict = {}
    slots: list[Slot] = []
    for sl in _all(ws, "WeaponSlot"):
        idx = _get(sl, "index")
        if not isinstance(idx, int):
            continue
        tier = _get(sl, "tier")
        slot = Slot(index=idx, order=int(_get(sl, "order", 0) or 0),
                    tier=tier if isinstance(tier, int) else None,
                    centre=bool(_get(sl, "notUseforDisbalanceCalculation", False)))
        seen: set[str] = set()
        for pb in _all(sl, "WeaponPreset"):
            name = _get(pb, "name")
            if not isinstance(name, str) or name in seen:
                continue
            seen.add(name)
            stems = _weapon_stems(pb)
            deps = tuple((int(_get(d, "slot")), str(_get(d, "preset")))
                         for d in _all(pb, "DependentWeaponPreset")
                         if isinstance(_get(d, "slot"), int) and _get(d, "preset"))
            bans = tuple((int(_get(d, "slot")), str(_get(d, "preset")))
                         for d in _all(pb, "BannedWeaponPreset")
                         if isinstance(_get(d, "slot"), int) and _get(d, "preset"))
            entries, chapter, trig = _preset_entries(pb, resolve_blk, wcache, DEFAULT_SIZES)
            req = [str(r) for r in _all(pb, "reqModification") if isinstance(r, str) and r]
            for w in _all(pb, "Weapon"):                      # a Weapon-level reqModification also counts, like the game
                for r in _all(w, "reqModification"):
                    if isinstance(r, str) and r and r not in req and not _all(pb, "reqModification"):
                        req.append(r)
            icon_type = _get(pb, "iconType")
            icon = tier_icon(icon_type if isinstance(icon_type, str) else None, trig,
                             sum(e.mass_kg for e in entries), len(entries),
                             max((e.ammo for e in entries), default=0) if len(entries) == 1 else 0)
            slot.presets.append(PresetOption(name, _label(name, stems, name_for), _nodes(pb, "ShowNodes"),
                                             _nodes(pb, "HideNodes"), deps, stems, icon, chapter,
                                             tuple(entries), tuple(req), bans,
                                             _preset_mounts(pb, resolve_blk, wcache)))
        slots.append(slot)
    slots = sorted((s for s in slots if s.options), key=lambda s: s.index)
    if not slots:
        return None

    # labels that collide inside one slot get the preset name appended, so every entry stays unique
    for sl_ in slots:
        count: dict[str, int] = {}
        for p in sl_.options:
            count[p.label] = count.get(p.label, 0) + 1
        for p in sl_.options:
            if count[p.label] > 1:
                p.label = f"{p.label}  [{p.name}]"

    stock: list[StockLoadout] = []
    if resolve_blk:
        prefix = vehicle_id.lower() + "_"
        for pr in _all(_get(root, "weapon_presets"), "preset"):
            nm, path = _get(pr, "name"), _get(pr, "blk")
            if not isinstance(nm, str) or not isinstance(path, str):
                continue
            f = resolve_blk(path)
            if not f:
                continue
            try:
                body = parse_blk(Path(f).read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            chosen = {int(_get(w, "slot")): str(_get(w, "preset")) for w in _all(body, "Weapon")
                      if isinstance(_get(w, "slot"), int) and _get(w, "preset")}
            stock.append(StockLoadout(nm, nm, chosen))
        # drop the vehicle-name part of the preset names ("fa_18a_aim_9l" of vehicle "fa_18a_raaf" -> "aim_9l")
        for st_ in stock:
            st_.label = pretty_stock_name(_strip_vehicle_prefix(st_.name, vehicle_id))
    data = LoadoutData(base_hidden, slots, stock,
                       _num(_get(ws, "maxloadMass")), _num(_get(ws, "maxloadMassLeftConsoles")),
                       _num(_get(ws, "maxloadMassRightConsoles")), _num(_get(ws, "maxDisbalance")))
    return data if data.has_choices else None       # tanks, ships and one-choice aircraft get no panel
