"""
anm_clips.py - turn the parameter bindings of a Dagor animtree (see anm_graph.py) into named clip groups.

Background (verified on the F/A-18E animtree and the CDK sample sources):
  * an aircraft .a2d is ONE timeline ('start' -> 'finish'); gear, flaps, hatches ... are all stored on it.
  * the animtree says which scene nodes each game parameter scrubs on that timeline, e.g.
        p_gear_1 -> ^gear_l\\d*\\d*$        p_hook -> hook_catch        p_bomb_hatch -> ^hatch\\d*$
    (`nodeRE` = regex on the node name, `node` = exact node name).
  * so a "clip" for one mechanism is: the same timeline, restricted to the tracks whose node name matches the
    mechanism's bindings.

Several parameters drive the same mechanism (p_gear_1 .. p_gear_7, p_ailerons_rel1_l_anim ...). They are merged by
a naming rule (clip_name) into one group, so the result is "gear", "ailerons", "flaps", "bomb_hatch", "hook", ...
That is the unit a UI button would toggle.

Not used (yet): the remap curves ('remaps' in anm_graph's output), which hold the sub-range of the timeline in which
each mechanism actually moves. Every clip keeps the full timeline so frame numbers line up across clips.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Iterable

# clip names that the generic rule below gets "wrong" or splits too finely
_ALIASES = {
    "flaps_aoa_mach": "flaps",           # p_flaps_rel_aoa_mach drives flap_l, the same flap as p_flaps_rel
    "rudder_inteceptor": "rudder_interceptor",   # typo in the game data
}


def clip_name(param: str) -> str:
    """'p_gear_3' -> 'gear', 'p_ailerons_rel2_l_anim' -> 'ailerons', 'p_nozzle_angle_rel_4' -> 'nozzle_angle'."""
    n = param.strip().lower()
    n = re.sub(r"^p_", "", n)
    n = re.sub(r"_anim$", "", n)
    n = re.sub(r"_rel(?=\d|_|$)", "", n)         # '_rel' marker (relative 0..1 value), keeps the digit if glued to it
    n = re.sub(r"_[lrc]$", "", n)                # side suffix: one clip for both sides
    n = re.sub(r"_?\d+$", "", n)                 # index suffix: gear_1..gear_7, nozzle_3
    n = re.sub(r"_[lrc]$", "", n)                # side suffix that was hidden behind an index: p_flaps_rel_l_1
    n = re.sub(r"_rel$", "", n)                  # '_rel' that was hidden behind the side suffix
    n = n.strip("_") or param
    return _ALIASES.get(n, n)


class ClipGroup:
    __slots__ = ("name", "params", "exact", "patterns")

    def __init__(self, name: str):
        self.name = name
        self.params: list[str] = []
        self.exact: set[str] = set()
        self.patterns: list[re.Pattern] = []

    def matches(self, node_name: str) -> bool:
        return node_name in self.exact or any(p.search(node_name) for p in self.patterns)


def build_groups(graph: dict) -> "OrderedDict[str, ClipGroup]":
    """graph = anm_graph.extract(...) output. Returns {clip name: ClipGroup}, in file order, duplicates removed."""
    groups: "OrderedDict[str, ClipGroup]" = OrderedDict()
    for b in graph.get("bindings", []):
        param = b["param"]
        g = groups.setdefault(clip_name(param), ClipGroup(clip_name(param)))
        if param not in g.params:
            g.params.append(param)
        if "nodeRE" in b:
            if all(p.pattern != b["nodeRE"] for p in g.patterns):
                try:
                    g.patterns.append(re.compile(b["nodeRE"]))
                except re.error:                 # a bad pattern must not lose the other clips
                    pass
        elif "node" in b:
            g.exact.add(b["node"])
    return groups


def match_tracks(groups: "OrderedDict[str, ClipGroup]", track_names: Iterable[str]):
    """-> ({clip name: [track names]}, [tracks no group claims]). A track can belong to more than one clip."""
    tracks = list(track_names)
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    claimed: set[str] = set()
    for name, g in groups.items():
        hit = [t for t in tracks if g.matches(t)]
        if hit:
            out[name] = hit
            claimed.update(hit)
    return out, [t for t in tracks if t not in claimed]