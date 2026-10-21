bl_info = {
    "name": "WT Aircraft Tools",
    "author": "wt-asset-explorer",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar (N) > WT Aircraft",
    "description": "Readable part names + animation sliders for a War Thunder aircraft imported from GLB; part names for OBJ",
    "category": "Animation",
}
"""
WT Aircraft Tools
=================
One button ("Prepare model") does, in this order:
  0. strip: removes empties with nothing under them (armament / pylon nodes of unexported weapons)
  1. part naming: readable names, custom properties wt_name / label, and collections:
       Parts - <model> > Mesh      > groups (Body, Wings, Landing gear, ...)
                       > Skeleton  > the same groups, holding every empty (the "bones")
     Collections never change parenting: the hierarchy stays exactly as it is.
  2. animation sliders: one 0..1 slider per animation clip

WHICH MODEL? (applies to every button)
  The add-on only touches ONE model, the one you point at:
  - it looks at the selected objects (or the active object if nothing is selected) and climbs
    to the top parent of each. A part selected, a few parts, or the whole model all give the
    same model.
  - nothing selected: if the scene holds exactly one model it is used; with two or more models
    it stops and asks you to select a part of the one you want.
  - selected parts of DIFFERENT models: it stops and asks you to pick one model.
  A "model" = an object without parent that has children. Everything else in the scene
  (other aircraft, cameras, lights) is never changed.

OBJ MODELS (no skeleton, no animation)
  An OBJ import is a flat list of mesh objects: no empties, no parents, no clips. For those the panel shows
  only "Rename parts only" and "Revert names". The parts go into  Parts - <vehicle> > <group>  (no Mesh /
  Skeleton split). The model = every loose mesh in the same collection as the part you select (or in the
  Parts collection the add-on made for it). Import ONE OBJ per collection, or name the first one before
  importing the next: two OBJs sitting unnamed in the same collection are treated as one model.

IMPORT the GLB with Animation ON, Animation mode = "Actions (NLA Tracks)".
Untested in real Blender (written without it): the model choice and the naming rules were
tested with fake objects only. Messages go to the status bar and the console prefixed [WT Aircraft].
"""
import json
import re
import uuid

import bpy

# ----------------------------------------------------------------------------- settings
NAME_FORMAT = "{label}  [{orig}]"
ROOT_COLLECTION = "Parts"
SKIP_WORDS = ("_anim",)          # the full clip: not a slider, it would fight the named clips
STASH_KEY = "wt_stash_action"
VALUES_KEY = "wt_slider_values"   # custom property on the model root: its slider positions (JSON)


def log(*a):
    print("[WT Aircraft]", *a)


# ----------------------------------------------------------------------------- which model
def top_ancestor(o):
    while o.parent is not None:
        o = o.parent
    return o


def choose_model(selected, active, scene_objects):
    """-> (root, error text). Pure logic, works on any objects with .parent/.children/.name."""
    picked = list(selected) or ([active] if active is not None else [])
    roots = []
    for o in picked:
        r = top_ancestor(o)
        if r.children and all(r is not x for x in roots):
            roots.append(r)
    if len(roots) == 1:
        return roots[0], ""
    if len(roots) > 1:
        return None, "Selection spans %d models (%s): select parts of ONE model" % (
            len(roots), ", ".join(r.name for r in roots[:4]))
    cands = [o for o in scene_objects if o.parent is None and o.children]
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return None, "No model found in the scene"
    return None, "The scene has %d models (%s): select a part or the whole model you want" % (
        len(cands), ", ".join(c.name for c in cands[:4]))


def find_model(context):
    return choose_model(context.selected_objects, context.active_object, context.scene.objects)


def model_objects(root):
    return [root] + list(root.children_recursive)


def flat_mesh(o):
    """A part of an OBJ import: a mesh with no parent and no children."""
    return o.type == "MESH" and o.parent is None and not o.children


def _first_coll(o):
    cs = list(o.users_collection)
    return cs[0].name if cs else ""


def obj_set(o, scene_objects):
    """(key, objects) of the loose OBJ model that part `o` belongs to. Named parts are found by their wt_tag,
    parts not named yet by the collection they sit in."""
    tag = o.get("wt_tag")
    if tag:
        return ("tag", tag), [x for x in scene_objects if flat_mesh(x) and x.get("wt_tag") == tag]
    cname = _first_coll(o)
    return ("coll", cname), [x for x in scene_objects
                             if flat_mesh(x) and "wt_tag" not in x and _first_coll(x) == cname]


def choose_target(picked, scene_objects):
    """-> (kind, data, error). kind 'glb' (data = root object) or 'obj' (data = dict with objs / key / coll)
    or None. Pure logic: works on any objects with .parent/.children/.type/.users_collection/.get."""
    scene_objects = list(scene_objects)
    roots, flats = [], []
    for o in picked:
        r = top_ancestor(o)
        if r.children:
            if all(r is not x for x in roots):
                roots.append(r)
        elif flat_mesh(o):
            flats.append(o)
    if roots and flats:
        return None, None, "Selection mixes a GLB model and loose OBJ parts: select one kind"
    if flats:
        keys, sets = [], {}
        for o in flats:
            k, objs = obj_set(o, scene_objects)
            if k not in sets:
                keys.append(k)
                sets[k] = objs
        if len(keys) > 1:
            return None, None, "Selection spans %d OBJ models: select parts of ONE model" % len(keys)
        k = keys[0]
        return "obj", {"objs": sets[k], "key": k, "coll": k[1] if k[0] == "coll" else ""}, ""
    if roots:
        root, err = choose_model(picked, None, scene_objects)
        return ("glb", root, "") if root is not None else (None, None, err)
    cands = [o for o in scene_objects if o.parent is None and o.children]
    obj_sets = {}
    for o in scene_objects:
        if flat_mesh(o):
            k, objs = obj_set(o, scene_objects)
            obj_sets.setdefault(k, objs)
    total = len(cands) + len(obj_sets)
    if total == 1:
        if cands:
            return "glb", cands[0], ""
        k = next(iter(obj_sets))
        return "obj", {"objs": obj_sets[k], "key": k, "coll": k[1] if k[0] == "coll" else ""}, ""
    if total == 0:
        return None, None, "No model found in the scene"
    names = [c.name for c in cands] + ["OBJ in '%s'" % (k[1] if k[0] == "coll" else k[1]) for k in obj_sets]
    return None, None, "The scene has %d models (%s): select a part or the whole model you want" % (
        total, ", ".join(names[:4]))


def find_target(context):
    picked = list(context.selected_objects) or ([context.active_object] if context.active_object is not None else [])
    return choose_target(picked, context.scene.objects)


def _valid(ob):
    try:
        ob.name
        return True
    except ReferenceError:
        return False


# ----------------------------------------------------------------------------- part naming
SIDE = {"l": "Left", "r": "Right", "c": "Center"}
GEAR_WHOLE = {"c": "Entire nose gear (center)", "l": "Entire left main gear", "r": "Entire right main gear"}
GEAR_SHORT = {"c": "Nose gear", "l": "Left main gear", "r": "Right main gear"}

VOCAB = {
    "spar": "wing spar", "wing": "wing", "flaperon": "flaperon", "slat": "slat", "elevator": "stabilizer",
    "rudder": "rudder", "fin": "vertical fin", "tail": "tail", "engine": "engine", "prop": "engine fan",
    "tank": "fuel tank", "oil": "oil tank", "cover": "cover", "wingcontrol": "wing control",
    "tailcontrol": "tail control", "pilot": "pilot", "seat": "ejection seat", "refueling": "refuelling probe part",
    "optic": "optic", "gun": "gun", "turret": "turret", "pgo": "PGO", "emtr": "emitter", "flare": "flare",
    "jet": "jet", "flame": "flame", "pylon": "pylon", "pbs": "PBS", "nozzle": "nozzle", "chute": "chute",
    "airbrake": "airbrake", "blister": "canopy", "wheel": "wheel", "gear": "gear", "dm": "(damage)",
    "break": "break", "exhaust": "exhaust", "fuel": "fuel", "dump": "dump", "shellrejection": "shell ejection",
    "inversion": "inversion trail", "from": "start", "to": "end", "main": "main", "pod": "pod", "launcher": "launcher",
    "connector": "connector", "holder": "holder", "ecm": "ECM", "apk": "APK", "damocles": "Damocles",
}


def _intake(m, clips):
    kind = ("grid door" if "intake_grid_door" in clips else "ramp" if "intake_ramp" in clips
            else "auxiliary flap" if any(c.startswith("intake_aux") for c in clips) else "part")
    return "Intake %s %s %s" % (m.group(1), kind, m.group(2))


# (regex on the original name, group, label text or function(match, clips) or None = generic). First match wins.
RULES = [
    (r"^wt_mirror_z$", "Body", "Model root"),
    (r"^@root", "Body", "Main body"),
    (r"^fuse$", "Body", "Fuselage"),
    (r"^tail$", "Tail", "Tail section"),
    (r"^cockpit$", "Cockpit", "Cockpit interior"),
    (r"^blister(\d*)$", "Cockpit", lambda m, c: "Canopy" + (" part " + m.group(1) if m.group(1) not in ("", "1") else "")),
    (r"^(pilot|seat)", "Cockpit", None),
    (r"^gear_([clr])_dm$", "Landing gear", lambda m, c: GEAR_WHOLE[m.group(1)]),
    (r"^gear_([clr])$", "Landing gear", lambda m, c: GEAR_SHORT[m.group(1)] + " strut"),
    (r"^wheel_([clr])$", "Landing gear", lambda m, c: GEAR_SHORT[m.group(1)] + " wheel"),
    (r"^gear_([clr])(\d+)$", "Landing gear", lambda m, c: "%s part %s" % (GEAR_SHORT[m.group(1)], m.group(2))),
    (r"^(spar|wing|flaperon|slat)", "Wings", None),
    (r"^(wingcontrol|tank[45])", "Wings", None),
    (r"^(elevator|rudder|fin|tailcontrol|cover|oil|tank[3678])", "Tail", None),
    (r"^airbrake_l(\d*)$", "Airbrake and chute", lambda m, c: "Airbrake" + (" panel " + m.group(1) if m.group(1) else "")),
    (r"^chute(\d+)$", "Airbrake and chute", lambda m, c: "Chute part " + m.group(1)),
    (r"^nozzle([12])_main$", "Engines", lambda m, c: "Engine %s nozzle (main)" % m.group(1)),
    (r"^nozzle([12])_(\d+)$", "Engines", lambda m, c: "Engine %s nozzle petal %s" % (m.group(1), m.group(2))),
    (r"^nozzle([34])_(\d+)$", "Intakes", _intake),
    (r"^(engine|prop)", "Engines", None),
    (r"^refueling", "Refuelling probe", None),
    (r"^(optic|pgo)", "Sensors", None),
    (r"^(emtr|flare|jet_flame)", "Effects and markers", None),
    (r"^(pbs_pylon_\d+)", "Loadout", None),
    (r"^(pylon_\d+)", "Loadout", None),
    (r"(pylon|_pod|^fab|^ofab|^ecm|^apk|^damocles)", "Loadout", None),
]
def _weapon(m, clips):
    """Weapon hung on a pylon by the exporter: 'wpn_s12_pylon_8_r_73/su_r_73_default[/<part>]'."""
    text = "Pylon %s weapon: %s" % (m.group(2), m.group(3).replace("_", " "))
    leaf = (m.group(5) or "").split("/")[-1]
    leaf = re.sub(r"\.\d+$", "", leaf)
    if leaf == "wt_mirror_z":
        text += " (root)"
    elif leaf and leaf not in ("@root", "mesh"):
        text += " - " + generic_label(leaf)
    return text


RULES.insert(0, (r"^wpn_s(\d+)_pylon_(\d+)_([^/]+)/([^/]+)(?:/(.+))?$", "Loadout", _weapon))
COMPILED = [(re.compile(p), g, l) for p, g, l in RULES]


def generic_label(orig):
    base = re.sub(r"\.\d+$", "", orig)
    words, side, damage = [], "", False
    for tok in re.findall(r"[A-Za-z]+|\d+", base):
        low = tok.lower()
        if low == "dm":
            damage = True
        elif low in SIDE and len(tok) == 1:
            side = SIDE[low]
        elif low in VOCAB:
            words.append(VOCAB[low])
        else:
            words.append(tok)
    text = " ".join(words)
    if side:
        text = (side + " " + text).strip()
    if damage:
        text += " (damage)"
    return (text[:1].upper() + text[1:]) if text else orig


def classify(orig, clips=(), parent_group=None):
    base = re.sub(r"\.\d+$", "", orig)
    for rx, group, label in COMPILED:
        m = rx.search(base)
        if m:
            if label is None:
                return group, generic_label(orig)
            return group, (label(m, clips) if callable(label) else label)
    return (parent_group or "Other"), generic_label(orig)


def pylon_sub(orig):
    base = re.sub(r"\.\d+$", "", orig)
    m = re.match(r"^pbs_pylon_(\d+)", base)
    if m:
        return "PBS pylon " + m.group(1)
    m = re.match(r"^pylon_(\d+)", base)
    if not m:
        m = re.match(r"^wpn_s\d+_pylon_(\d+)_", base)
    return ("Pylon " + m.group(1)) if m else ""


def clips_of(obj):
    ad = obj.animation_data
    if ad is None:
        return []
    names = {t.name for t in ad.nla_tracks if t.strips}
    if ad.action is not None:
        names.add(ad.action.name)
    return sorted(names)


def child_collection(parent, name, key):
    """child collection of `parent` marked with custom property wt_key == key (created if missing).
    Looked up by the key, not by the name: Blender renames duplicates to 'Body.001'."""
    for c in parent.children:
        if c.get("wt_key") == key:
            return c
    c = bpy.data.collections.new(name)
    c["wt_key"] = key
    parent.children.link(c)
    return c


def drop_collection(c):
    for ch in list(c.children):
        drop_collection(ch)
    bpy.data.collections.remove(c)


def find_collection_by_key(key):
    for c in bpy.data.collections:
        if c.get("wt_key") == key:
            return c
    return None


def find_parts_collection(root):
    tag = root.get("wt_model")
    return find_collection_by_key("%s|parts" % tag) if tag else None


def model_tag(root):
    """Unique id of a model, kept on its root. The collections are keyed with it: the game name of the root is the
    SAME for every aircraft ('wt_mirror_z'), so it cannot tell two models apart."""
    tag = root.get("wt_model")
    if not tag:
        tag = uuid.uuid4().hex[:8]
        root["wt_model"] = tag
    return str(tag)


def drop_if_empty(c):
    """Remove a collection (and its sub-collections) only if no object is left in it: a collection that is still
    shared with another model is kept."""
    if c is not None and not list(c.all_objects):
        drop_collection(c)
        return True
    return False


def clean_name(name):
    return re.sub(r"\.\d+$", "", name)


def _depth(o):
    d = 0
    while o.parent is not None:
        o = o.parent
        d += 1
    return d


def parts_collection_name(root):
    return "%s - %s" % (ROOT_COLLECTION, root["wt_name"])


def vehicle_name(context, root, use_field=True):
    """Vehicle name for the collection title: the panel field if filled, else taken from the
    full clip '<vehicle>_anim' (su_30sm2_anim -> su_30sm2), else the root's game name."""
    v = context.scene.wtair_vehicle.strip() if use_field else ""
    if v:
        return v
    for o in model_objects(root):
        ad = o.animation_data
        if not ad:
            continue
        names = [t.name for t in ad.nla_tracks]
        if ad.action is not None:
            names.append(ad.action.name)
        names.append(str(o.get(STASH_KEY) or ""))
        for n in names:
            if n.endswith("_anim") and len(n) > 5:
                return n[:-5]
    return str(root.get("wt_name") or root.name)


TITLE_CACHE = {}


def model_title(root):
    """Name shown in the panel for a model: the vehicle name stored when it was named / prepared
    (custom property wt_vehicle), else the one read from its full clip ('su_30sm2_anim'), else the root name."""
    v = root.get("wt_vehicle")
    if v:
        return str(v)
    k = root.as_pointer()
    if k not in TITLE_CACHE:
        TITLE_CACHE[k] = vehicle_name(None, root, use_field=False)
    return TITLE_CACHE[k]


def remember_vehicle(context, root, force=False):
    if force or "wt_vehicle" not in root:
        root["wt_vehicle"] = vehicle_name(context, root)


def same_obj(a, b):
    try:
        return a is not None and b is not None and a.as_pointer() == b.as_pointer()
    except ReferenceError:
        return False


def set_outliner_children(context, show):
    """Outliner filter 'Object Children'. When on, Blender lists every child under its parent,
    greyed out if the child sits in another collection (so parts show up twice)."""
    wm = getattr(context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "OUTLINER":
                for sp in area.spaces:
                    if hasattr(sp, "use_filter_children"):
                        sp.use_filter_children = show
                area.tag_redraw()


def apply_names(context, root, do_rename, do_group):
    """Name the parts of ONE model (root and everything under it)."""
    objs = model_objects(root)
    remember_vehicle(context, root, force=True)      # the panel shows this name for the model
    legacy_tag = str(root["wt_name"]) if ("wt_name" in root and "wt_model" not in root) else None   # named by an older version
    for o in objs:                                   # remember the game names first
        if "wt_name" not in o:
            o["wt_name"] = o.name
    objs.sort(key=_depth)                            # parents first so children inherit the group
    groups, results, counts = {}, {}, {}
    for o in objs:
        pg = groups.get(o.parent.name) if (o.parent is not None and o is not root) else None
        group, label = classify(o["wt_name"], clips_of(o), pg)
        groups[o.name] = group
        results[o.name] = (o["wt_name"], group, label)
    mesh_root = skel_root = None
    kinds = {"Mesh": 0, "Skeleton": 0}
    if do_group:
        tag = model_tag(root)
        pname = "%s - %s" % (ROOT_COLLECTION, root["wt_vehicle"])
        coll_root = child_collection(context.scene.collection, pname, tag + "|parts")
        if clean_name(coll_root.name) != pname:            # Blender adds .001 to a repeated title: not a reason to rename
            coll_root.name = pname
        mesh_root = child_collection(coll_root, "Mesh", tag + "|Mesh")
        skel_root = child_collection(coll_root, "Skeleton", tag + "|Skeleton")
    for o in objs:
        orig, group, label = results[o.name]
        o["label"] = label
        kind = "Skeleton" if o.type == "EMPTY" else "Mesh"
        kinds[kind] += 1
        if do_group:
            base = skel_root if kind == "Skeleton" else mesh_root
            sfx = " (skeleton)" if kind == "Skeleton" else ""     # collection names are global in Blender
            target = child_collection(base, group + sfx, "%s|%s|%s" % (tag, kind, group))
            sub = pylon_sub(orig) if group == "Loadout" else ""
            if sub:
                target = child_collection(target, sub + sfx, "%s|%s|%s|%s" % (tag, kind, group, sub))
            for c in list(o.users_collection):
                c.objects.unlink(o)
            target.objects.link(o)
        counts[group] = counts.get(group, 0) + 1
    if do_group:
        # collections of older versions (keyed by the root's game name, shared by every aircraft): removed once empty
        if legacy_tag:
            drop_if_empty(find_collection_by_key(legacy_tag + "|parts"))
        drop_if_empty(next((c for c in bpy.data.collections
                            if not c.get("wt_key") and c.name == "%s - %s" % (ROOT_COLLECTION, root["wt_name"])), None))
    if do_rename:
        for o in objs:
            orig, group, label = results[o.name]
            o.name = NAME_FORMAT.format(label=label, orig=orig)[:63]
    if do_group:
        set_outliner_children(context, not context.scene.wtair_flat)
    log("named %d parts of %s (%d mesh, %d skeleton): %s" % (
        len(objs), root.name, kinds["Mesh"], kinds["Skeleton"],
        ", ".join("%s %d" % kv for kv in sorted(counts.items()))))
    return len(objs)


GENERIC_COLLECTIONS = ("", "Collection", "Scene Collection")


def _new_tag(coll_name, scene_objects):
    used = {o.get("wt_tag") for o in scene_objects if o.get("wt_tag")}
    base = coll_name or "OBJ model"
    tag, i = base, 2
    while tag in used:
        tag = "%s#%d" % (base, i)
        i += 1
    return tag


def apply_names_obj(context, info, do_rename, do_group):
    """Name the parts of ONE loose OBJ model: no hierarchy, so no Skeleton collection and no parent groups."""
    objs = list(info["objs"])
    scene_objs = list(context.scene.objects)
    tag = info["key"][1] if info["key"][0] == "tag" else _new_tag(info["coll"], scene_objs)
    src = info["coll"] or ""
    for o in objs:                                   # remember the game names first
        if "wt_name" not in o:
            o["wt_name"] = o.name
            o["wt_tag"] = tag
            o["wt_src_coll"] = _first_coll(o)
    results, counts = {}, {}
    for o in objs:
        group, label = classify(o["wt_name"], clips_of(o), None)
        results[o.name] = (o["wt_name"], group, label)
    if do_group:
        v = context.scene.wtair_vehicle.strip()
        if not v:
            first = next((o.get("wt_src_coll") for o in objs if o.get("wt_src_coll")), src)
            v = first if first not in GENERIC_COLLECTIONS else "OBJ model"
        pname = "%s - %s" % (ROOT_COLLECTION, v)
        coll_root = child_collection(context.scene.collection, pname, tag + "|parts")
        if coll_root.name != pname:
            coll_root.name = pname
    for o in objs:
        orig, group, label = results[o.name]
        o["label"] = label
        if do_group:
            target = child_collection(coll_root, group, "%s|%s" % (tag, group))
            sub = pylon_sub(orig) if group == "Loadout" else ""
            if sub:
                target = child_collection(target, sub, "%s|%s|%s" % (tag, group, sub))
            for c in list(o.users_collection):
                c.objects.unlink(o)
            target.objects.link(o)
        counts[group] = counts.get(group, 0) + 1
    if do_rename:
        for o in objs:
            orig, group, label = results[o.name]
            o.name = NAME_FORMAT.format(label=label, orig=orig)[:63]
    log("named %d parts of an OBJ model (%s)" % (
        len(objs), ", ".join("%s %d" % kv for kv in sorted(counts.items()))))
    return len(objs)


def revert_names_obj(context, info):
    objs = [o for o in info["objs"] if "wt_name" in o]
    tags = {o["wt_tag"] for o in objs if "wt_tag" in o}
    for o in objs:                                   # back to the collection the import put them in
        dest = bpy.data.collections.get(o.get("wt_src_coll") or "")
        for c in list(o.users_collection):
            c.objects.unlink(o)
        (dest if dest is not None else context.scene.collection).objects.link(o)
    for o in objs:
        o.name = o["wt_name"]
    for o in objs:
        for k in ("wt_name", "wt_tag", "wt_src_coll", "label"):
            if k in o:
                del o[k]
    for c in [c for c in bpy.data.collections if (c.get("wt_key") or "").endswith("|parts")
              and c["wt_key"].split("|")[0] in tags]:
        drop_collection(c)
    log("reverted names of %d OBJ parts" % len(objs))
    return len(objs)


def revert_names(context, root):
    objs = [o for o in model_objects(root) if "wt_name" in o]
    coll = None
    if "wt_name" in root:
        coll = (find_parts_collection(root)
                or find_collection_by_key("%s|parts" % root["wt_name"])            # named by an older version
                or bpy.data.collections.get(parts_collection_name(root)))
    for o in objs:
        if coll is not None:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            context.scene.collection.objects.link(o)
    for o in objs:
        o.name = o["wt_name"]
    for o in objs:
        del o["wt_name"]
        if "label" in o:
            del o["label"]
    for k in ("wt_vehicle", "wt_model"):
        if k in root:
            del root[k]
    drop_if_empty(coll)                              # a collection another model still uses stays
    log("reverted names of %d parts" % len(objs))
    return len(objs)


# ----------------------------------------------------------------------------- strip unused empties
def is_loadout_name(orig):
    return classify(orig)[0] == "Loadout"


def strip_plan(root, remove_all, orig_of):
    """Empties that can go without touching anything visible: the empty and EVERYTHING under it
    must be empties (no mesh or other object below), and for the default scope (remove_all False)
    they must be loadout parts (pylon_*, pbs_pylon_*, weapons). A parent is never removed while
    one of its children stays, so no child is ever re-parented or moved."""
    objs = model_objects(root)
    cand = {o.as_pointer() for o in objs
            if o.type == "EMPTY" and o is not root and (remove_all or is_loadout_name(orig_of(o)))}
    memo = {}

    def ok(o):
        k = o.as_pointer()
        if k not in memo:
            memo[k] = k in cand and all(ok(c) for c in o.children)
        return memo[k]

    return [o for o in objs if ok(o)]


def strip_empties(context, root, remove_all):
    plan = strip_plan(root, remove_all, orig_name)
    n = len(plan)
    animated = sum(1 for o in plan if o.animation_data and (o.animation_data.action or o.animation_data.nla_tracks))
    for o in plan:
        bpy.data.objects.remove(o, do_unlink=True)
    log("removed %d empties without parts under them (%d of them animated) from %s" % (n, animated, root.name))
    return n, animated


# ----------------------------------------------------------------------------- smooth shading
def smooth_mesh_data(mesh):
    """Same as right-click > Shade Smooth on one mesh."""
    try:
        mesh.shade_smooth()                  # Blender 4.1+
    except AttributeError:
        for p in mesh.polygons:              # older Blender
            p.use_smooth = True
        mesh.update()


def smooth_objects(objs):
    """Shade Smooth on the mesh of every MESH object in objs. A mesh shared by several objects is done once."""
    seen, n = set(), 0
    for o in objs:
        if o.type != "MESH" or o.data is None:
            continue
        k = o.data.as_pointer()
        if k in seen:
            continue
        seen.add(k)
        smooth_mesh_data(o.data)
        n += 1
    log("smooth shading on %d meshes" % n)
    return n


def smooth_model(root):
    return smooth_objects(model_objects(root))


# ----------------------------------------------------------------------------- sliders
STATE = {"root": None}
CACHE = {}       # clip key -> list of (object, track name, strip name, frame start, frame end)
OBJ_CLIPS = {}   # object pointer -> set of clip keys that animate it
OBJ_ROWS = {}    # object pointer -> list of (track, strip, frame start, frame end)


def skipped(name):
    return any(w in name for w in SKIP_WORDS)


def orig_name(ob):
    """game name of an object: property wt_name, else the text in the last [...], else the name"""
    v = ob.get("wt_name")
    if v:
        return str(v)
    m = re.search(r"\[([^\[\]]+)\](?:\.\d+)?\s*$", ob.name)
    return m.group(1) if m else re.sub(r"\.\d{3}$", "", ob.name)


def extra_group(name):
    """Parts animated only by the full clip (no named clip contains them): slider by name."""
    n = re.sub(r"\.\d+$", "", name).lower()
    if n.startswith("airbrake"):
        return "airbrakes"
    if n.startswith("refuel"):
        return "refueling"
    if n.startswith("pylon_") or n.startswith("pbs_pylon"):
        return "pylon_launchers"
    return "other_full_clip"


def diagnose(objs):
    n_ad = n_act = n_trk = 0
    tracks = {}
    for ob in objs:
        ad = ob.animation_data
        if not ad:
            continue
        n_ad += 1
        n_act += 1 if ad.action else 0
        for tr in ad.nla_tracks:
            n_trk += 1
            tracks[tr.name] = tracks.get(tr.name, 0) + 1
    log("model objects with animation data: %d | active action: %d | NLA tracks: %d" % (n_ad, n_act, n_trk))
    return tracks


def _rows_objects():
    seen, out = set(), []
    for rows in CACHE.values():
        for r in rows:
            ob = r[0]
            if _valid(ob) and ob.as_pointer() not in seen:
                seen.add(ob.as_pointer())
                out.append(ob)
    return out


def apply_clip(clip, refresh=True):
    """strip_time on every strip of the objects of this clip. An object in several clips (the
    nozzle clip also holds the intake parts) follows the HIGHEST of its sliders: all named
    clips hold identical data on the same timeline."""
    sc = bpy.context.scene
    vals = {it.name: it.value for it in sc.wtair_clips}
    done = set()
    for row in CACHE.get(clip, ()):
        ob = row[0]
        if not _valid(ob) or not ob.animation_data:
            continue
        pid = ob.as_pointer()
        if pid in done:
            continue
        done.add(pid)
        v = max(vals.get(k, 0.0) for k in OBJ_CLIPS.get(pid, (clip,)))
        for tname, sname, fs, fe in OBJ_ROWS.get(pid, ()):
            tr = ob.animation_data.nla_tracks.get(tname)
            st = tr.strips.get(sname) if tr else None
            if st is not None:
                st.strip_time = fs + v * (fe - fs)
        ob.update_tag()
    if not refresh:
        return
    try:
        bpy.context.view_layer.update()
        sc.frame_set(sc.frame_current)
    except Exception:
        pass


RESTORING = [False]


def save_values(scene):
    """Remember the slider positions on the model root, so they survive switching models and saving the file."""
    r = STATE["root"]
    if r is None or not _valid(r):
        return
    r[VALUES_KEY] = json.dumps({it.name: round(float(it.value), 4) for it in scene.wtair_clips})


def on_slider(self, context):
    if RESTORING[0]:
        return
    apply_clip(self.name)
    save_values(context.scene)


class WTAIR_AnimClip(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty(name="", min=0.0, max=1.0, default=0.0, update=on_slider)


def stash_active_actions(objs):
    n = 0
    for ob in objs:
        ad = ob.animation_data
        if ad and ad.nla_tracks and ad.action:
            ob[STASH_KEY] = ad.action.name
            ad.action = None
            n += 1
    return n


def restore_active_actions(objs):
    n = 0
    for ob in objs:
        name = ob.get(STASH_KEY)
        if name and name in bpy.data.actions:
            ad = ob.animation_data_create()
            ad.action = bpy.data.actions[name]
            del ob[STASH_KEY]
            n += 1
    return n


def full_animation(scene):
    """Back to the full clip (space plays) for the model the sliders were made for."""
    objs = _rows_objects()
    for rows in CACHE.values():
        for ob, tname, sname, fs, fe in rows:
            if not _valid(ob) or not ob.animation_data:
                continue
            tr = ob.animation_data.nla_tracks.get(tname)
            st = tr.strips.get(sname) if tr else None
            if st:
                st.use_animated_time = False
    for ob in objs:
        for tr in ob.animation_data.nla_tracks:
            if skipped(tr.name):
                tr.mute = False
    n = restore_active_actions(objs)
    log("restored the active action on %d objects; space plays again" % n)


def setup_sliders(scene, root):
    # the model that had the sliders before keeps its pose and its slider values (saved on its root)
    CACHE.clear()
    OBJ_CLIPS.clear()
    OBJ_ROWS.clear()
    scene.wtair_clips.clear()
    STATE["root"] = root
    objs = model_objects(root)
    tracks = diagnose(objs)
    if not tracks:
        log("no NLA tracks on %s. Re-import the GLB with Animation ON and Animation mode = "
            "'Actions (NLA Tracks)'." % root.name)
        STATE["root"] = None
        return False
    for ob in objs:
        ad = ob.animation_data
        if not ad:
            continue
        ad.use_nla = True
        try:
            ad.use_tweak_mode = False
        except Exception:
            pass
        has_named = any(not skipped(t.name) for t in ad.nla_tracks)
        for tr in ad.nla_tracks:
            key = tr.name
            if skipped(tr.name):
                if has_named:
                    tr.mute = True       # the full clip would override the named clips
                    continue
                key = extra_group(orig_name(ob))
            tr.mute = False
            tr.is_solo = False
            for st in tr.strips:
                st.mute = False
                st.use_animated_time = True
                st.extrapolation = "HOLD"
                st.blend_type = "REPLACE"
                st.influence = 1.0
                fs, fe = st.action_frame_start, st.action_frame_end
                if fe <= fs:
                    fe = fs + 1
                CACHE.setdefault(key, []).append((ob, tr.name, st.name, fs, fe))
                OBJ_CLIPS.setdefault(ob.as_pointer(), set()).add(key)
                OBJ_ROWS.setdefault(ob.as_pointer(), []).append((tr.name, st.name, fs, fe))
    n = stash_active_actions(objs)
    log("took the active action off %d objects (sliders mode)" % n)
    try:
        saved = json.loads(root.get(VALUES_KEY) or "{}")
    except Exception:
        saved = {}
    RESTORING[0] = True
    try:
        for clip in sorted(CACHE):
            it = scene.wtair_clips.add()
            it.name = clip
            it.value = min(1.0, max(0.0, float(saved.get(clip, 0.0))))
    finally:
        RESTORING[0] = False
    if any(float(v) for v in saved.values()):           # put the model back in the pose the sliders were left in
        for clip in sorted(CACHE):
            apply_clip(clip, refresh=False)
        try:
            bpy.context.view_layer.update()
            scene.frame_set(scene.frame_current)
        except Exception:
            pass
    log("sliders:", ", ".join(sorted(CACHE)))
    report()
    return True


def report():
    root = STATE["root"]
    L = ["Blender " + bpy.app.version_string, "model: %s" % (root.name if root and _valid(root) else None), ""]
    objs = model_objects(root) if root and _valid(root) else []
    ads = [o for o in objs if o.animation_data]
    L.append("model objects: %d | with animation_data: %d" % (len(objs), len(ads)))
    L.append("active action: %d | NLA tracks: %d | stashed: %d" % (
        sum(1 for o in ads if o.animation_data.action), sum(1 for o in ads if o.animation_data.nla_tracks),
        sum(1 for o in objs if STASH_KEY in o)))
    L.append("clips in CACHE: %d" % len(CACHE))
    for clip in sorted(CACHE):
        rows = CACHE[clip]
        L.append("  %s: %d strips" % (clip, len(rows)))
    other = CACHE.get("other_full_clip", ())
    if other:
        L.append("parts under other_full_clip (no rule): " + ", ".join(sorted({orig_name(r[0]) for r in other if _valid(r[0])})[:40]))
    txt = bpy.data.texts.get("WT_Aircraft_report") or bpy.data.texts.new("WT_Aircraft_report")
    txt.clear()
    txt.write("\n".join(L))


# ----------------------------------------------------------------------------- operators / panel
def _model_or_fail(op, context):
    """The GLB model to work on; an OBJ target is refused (those only support naming)."""
    kind, data, err = find_target(context)
    if kind == "obj":
        op.report({"ERROR"}, "OBJ model: only 'Rename parts only' and 'Revert names' apply (no skeleton, no animation)")
        return None
    if kind is None:
        op.report({"ERROR"}, err)
        return None
    return data


class WTAIR_OT_prepare(bpy.types.Operator):
    bl_idname = "wtair.prepare"
    bl_label = "Prepare model (names + sliders)"
    bl_description = "1) readable part names and groups, 2) animation sliders, for the selected model only"

    def execute(self, context):
        root = _model_or_fail(self, context)
        if root is None:
            return {"CANCELLED"}
        sc = context.scene
        removed = 0
        if sc.wtair_strip:
            removed, _a = strip_empties(context, root, sc.wtair_strip_all)
        if sc.wtair_rename or sc.wtair_group:
            apply_names(context, root, sc.wtair_rename, sc.wtair_group)
        if sc.wtair_smooth:
            smooth_model(root)
        remember_vehicle(context, root)
        ok = setup_sliders(sc, root)
        self.report({"INFO"} if ok else {"WARNING"},
                    ("Prepared " if ok else "Named, but no animation found on ") + root.name
                    + (" (%d empties removed)" % removed if removed else ""))
        return {"FINISHED"}


class WTAIR_OT_strip(bpy.types.Operator):
    bl_idname = "wtair.strip"
    bl_label = "Remove unused empties"
    bl_description = ("Delete empties with nothing under them (armament / pylon nodes of weapons that were not "
                      "exported). Ctrl+Z undoes it")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = _model_or_fail(self, context)
        if root is None:
            return {"CANCELLED"}
        n, a = strip_empties(context, root, context.scene.wtair_strip_all)
        if CACHE and same_obj(STATE["root"], root):
            setup_sliders(context.scene, root)       # rebuild the sliders without the removed parts
        self.report({"INFO"}, "Removed %d empties (%d animated) from %s" % (n, a, root.name))
        return {"FINISHED"}


class WTAIR_OT_names(bpy.types.Operator):
    bl_idname = "wtair.names"
    bl_label = "Rename parts only"

    def execute(self, context):
        kind, data, err = find_target(context)
        if kind is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        sc = context.scene
        if kind == "obj":
            n = apply_names_obj(context, data, sc.wtair_rename, sc.wtair_group)
            self.report({"INFO"}, "Named %d parts of the OBJ model" % n)
        else:
            apply_names(context, data, sc.wtair_rename, sc.wtair_group)
        return {"FINISHED"}


class WTAIR_OT_smooth(bpy.types.Operator):
    bl_idname = "wtair.smooth"
    bl_label = "Smooth shading"
    bl_description = "Shade Smooth on every mesh of the model (like right-click > Shade Smooth). Ctrl+Z undoes it"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        kind, data, err = find_target(context)
        if kind is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        n = smooth_objects(data["objs"] if kind == "obj" else model_objects(data))
        self.report({"INFO"}, "Smooth shading on %d meshes" % n)
        return {"FINISHED"}


class WTAIR_OT_revert(bpy.types.Operator):
    bl_idname = "wtair.revert"
    bl_label = "Revert names"

    def execute(self, context):
        kind, data, err = find_target(context)
        if kind is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        if kind == "obj":
            n = revert_names_obj(context, data)
            self.report({"INFO"}, "Reverted %d OBJ parts" % n)
        else:
            revert_names(context, data)
        return {"FINISHED"}


class WTAIR_OT_sliders(bpy.types.Operator):
    bl_idname = "wtair.sliders"
    bl_label = "Sliders mode"

    def execute(self, context):
        root = _model_or_fail(self, context)
        if root is None:
            return {"CANCELLED"}
        remember_vehicle(context, root)
        setup_sliders(context.scene, root)
        return {"FINISHED"}


class WTAIR_OT_retarget(bpy.types.Operator):
    bl_idname = "wtair.retarget"
    bl_label = "Switch sliders to the selected model"
    bl_description = ("Build the sliders for the selected model. The model that had them keeps its pose, and "
                      "its slider positions come back when you switch to it again")

    def execute(self, context):
        root = _model_or_fail(self, context)
        if root is None:
            return {"CANCELLED"}
        if CACHE and same_obj(STATE["root"], root):
            self.report({"INFO"}, "The sliders already control " + model_title(root))
            return {"CANCELLED"}
        remember_vehicle(context, root)
        ok = setup_sliders(context.scene, root)
        self.report({"INFO"} if ok else {"WARNING"},
                    ("Sliders now control " if ok else "No animation found on ") + model_title(root))
        return {"FINISHED"} if ok else {"CANCELLED"}


class WTAIR_OT_full(bpy.types.Operator):
    bl_idname = "wtair.full"
    bl_label = "Full animation (space plays)"

    def execute(self, context):
        if not CACHE:
            self.report({"ERROR"}, "No sliders set up in this session")
            return {"CANCELLED"}
        full_animation(context.scene)
        return {"FINISHED"}


class WTAIR_OT_reset(bpy.types.Operator):
    bl_idname = "wtair.reset"
    bl_label = "All to 0"

    def execute(self, context):
        for it in context.scene.wtair_clips:
            it.value = 0.0
        save_values(context.scene)
        return {"FINISHED"}


class WTAIR_OT_report(bpy.types.Operator):
    bl_idname = "wtair.report"
    bl_label = "Write report"

    def execute(self, context):
        report()
        self.report({"INFO"}, "Report in the Text editor: WT_Aircraft_report")
        return {"FINISHED"}


class WTAIR_PT_panel(bpy.types.Panel):
    bl_label = "WT Aircraft"
    bl_idname = "WTAIR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WT Aircraft"

    def draw(self, context):
        lay = self.layout
        sc = context.scene
        kind, data, err = find_target(context)
        root = data if kind == "glb" else None
        box = lay.box()
        if kind is None:
            box.alert = True
            box.label(text=err, icon="ERROR")
        elif kind == "obj":
            box.label(text="Target: OBJ model, %d parts" % len(data["objs"]), icon="MESH_DATA")
        else:
            box.label(text="Target: " + model_title(root), icon="OUTLINER_OB_ARMATURE")
        lay.prop(sc, "wtair_vehicle", text="Vehicle")
        row = lay.row()
        row.prop(sc, "wtair_rename")
        row.prop(sc, "wtair_group")
        if kind == "obj":
            col = lay.column(align=True)
            col.operator("wtair.names")
            col.operator("wtair.revert")
            col.operator("wtair.smooth")
            return
        lay.prop(sc, "wtair_flat")
        row = lay.row()
        row.prop(sc, "wtair_strip")
        row.prop(sc, "wtair_strip_all")
        lay.prop(sc, "wtair_smooth")
        lay.operator("wtair.prepare", icon="PLAY")
        col = lay.column(align=True)
        col.operator("wtair.strip")
        col.operator("wtair.names")
        col.operator("wtair.revert")
        col.operator("wtair.smooth")
        col = lay.column(align=True)
        col.operator("wtair.sliders")
        col.operator("wtair.full")
        col.operator("wtair.reset")
        col.operator("wtair.report")
        if len(sc.wtair_clips):
            r = STATE["root"]
            r_ok = r is not None and _valid(r)
            lay.label(text="Sliders for: " + (model_title(r) if r_ok else "?"))
            if root is not None and not (r_ok and same_obj(r, root)):
                lay.operator("wtair.retarget", text="Switch sliders to " + model_title(root), icon="FILE_REFRESH")
            if not CACHE:
                lay.label(text="Session restarted: press Sliders mode", icon="ERROR")
            for it in sc.wtair_clips:
                lay.prop(it, "value", text=it.name, slider=True)


CLASSES = (WTAIR_AnimClip, WTAIR_OT_smooth, WTAIR_OT_prepare, WTAIR_OT_strip, WTAIR_OT_names, WTAIR_OT_revert, WTAIR_OT_sliders,
           WTAIR_OT_retarget, WTAIR_OT_full, WTAIR_OT_reset, WTAIR_OT_report, WTAIR_PT_panel)


def register():
    for c in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.wtair_clips = bpy.props.CollectionProperty(type=WTAIR_AnimClip)
    bpy.types.Scene.wtair_rename = bpy.props.BoolProperty(name="Rename", default=True)
    bpy.types.Scene.wtair_group = bpy.props.BoolProperty(name="Group", default=True)
    bpy.types.Scene.wtair_vehicle = bpy.props.StringProperty(
        name="Vehicle", default="",
        description="Name used in the collection title. Empty = read from the clip name (su_30sm2_anim)")
    bpy.types.Scene.wtair_flat = bpy.props.BoolProperty(
        name="Flat outliner", default=True,
        description="Turn off the outliner filter 'Object Children' so each collection lists only its own parts",
        update=lambda self, ctx: set_outliner_children(ctx, not self.wtair_flat))
    bpy.types.Scene.wtair_strip = bpy.props.BoolProperty(
        name="Strip empties", default=True,
        description="Prepare also removes empties with no part under them (loadout nodes of unexported weapons)")
    bpy.types.Scene.wtair_strip_all = bpy.props.BoolProperty(
        name="All kinds", default=False,
        description="Remove every empty with nothing under it, not only loadout (pylon / weapon) nodes")
    bpy.types.Scene.wtair_smooth = bpy.props.BoolProperty(
        name="Smooth shading", default=True,
        description="Prepare also does Shade Smooth on every mesh of the model")


def unregister():
    for c in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    for p in ("wtair_clips", "wtair_rename", "wtair_group", "wtair_vehicle", "wtair_flat", "wtair_strip", "wtair_strip_all", "wtair_smooth"):
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)


if __name__ == "__main__":
    register()