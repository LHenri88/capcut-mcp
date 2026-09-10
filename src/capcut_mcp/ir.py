"""The Timeline IR: a seconds-based, flat view of a CapCut draft.

The draft graph is deliberately indirect -- a segment carries a ``material_id``
that points into one of the ``materials.<category>[]`` arrays, plus a list of
``extra_material_refs`` for its speed, canvas, denoise and so on. Agents should
never have to walk that graph, so everything they read and address goes through
this module: times in seconds, clips addressed by a short id prefix.

This IR is the stable contract. If a CapCut update changes the underlying
schema, the compilers on either side change and this shape does not.
"""

from __future__ import annotations

from typing import Any

US = 1_000_000
ID_LEN = 8


def to_us(seconds: float) -> int:
    return int(round(float(seconds) * US))


def to_s(microseconds: int | float | None) -> float:
    return round((microseconds or 0) / US, 3)


def short(guid: str) -> str:
    return (guid or "")[:ID_LEN]


def _material_index(data: dict) -> dict[str, tuple[str, dict]]:
    """Map every material id to (category, object)."""
    index: dict[str, tuple[str, dict]] = {}
    for category, items in (data.get("materials") or {}).items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                index[item["id"]] = (category, item)
    return index


def _text_content(material: dict) -> str:
    """Pull the display string out of a text material's nested JSON payload."""
    import json

    raw = material.get("content")
    if not isinstance(raw, str):
        return material.get("base_content") or ""
    try:
        return json.loads(raw).get("text", "")
    except (json.JSONDecodeError, AttributeError):
        return raw


_KEYFRAME_FIELD = {
    "KFTypePositionX": ("position", "x"), "KFTypePositionY": ("position", "y"),
    "KFTypeScaleX": ("scale", "x"), "KFTypeScaleY": ("scale", "y"),
    "KFTypeRotation": ("rotation", None), "KFTypeAlpha": ("alpha", None),
}


def evaluate_keyframes(segment: dict, at: float) -> dict:
    """Interpolate every keyframe block on a segment at an absolute timeline
    instant. Returns {"position": {"x":.., "y":..}, "scale": {...}, ...} for
    whichever properties are keyframed; static ones are simply absent.

    Only linear interpolation is implemented, matching every curveType
    observed in local drafts ("Line"); a non-Line curve falls back to linear
    between its bracketing points rather than failing.
    """
    target = segment.get("target_timerange") or {}
    clip_start_s = to_s(target.get("start"))
    local_t = at - clip_start_s

    out: dict = {}
    for block in segment.get("common_keyframes") or []:
        field, axis = _KEYFRAME_FIELD.get(block.get("property_type", ""), (None, None))
        if field is None:
            continue
        points = sorted(
            ((to_s(p.get("time_offset")), (p.get("values") or [0.0])[0])
             for p in block.get("keyframe_list") or []),
            key=lambda p: p[0],
        )
        if not points:
            continue

        if local_t <= points[0][0]:
            value = points[0][1]
        elif local_t >= points[-1][0]:
            value = points[-1][1]
        else:
            value = points[-1][1]
            for (t0, v0), (t1, v1) in zip(points, points[1:]):
                if t0 <= local_t <= t1:
                    frac = (local_t - t0) / (t1 - t0) if t1 > t0 else 0.0
                    value = v0 + (v1 - v0) * frac
                    break

        if axis:
            out.setdefault(field, {})[axis] = value
        else:
            out[field] = value

    return out


def describe_clip(segment: dict, index: dict[str, tuple[str, dict]], at: float | None = None) -> dict:
    target = segment.get("target_timerange") or {}
    source = segment.get("source_timerange") or {}
    clip = segment.get("clip") or {}
    scale = clip.get("scale") or {}
    transform = clip.get("transform") or {}

    category, material = index.get(segment.get("material_id", ""), ("unknown", {}))

    if category == "videos" and material.get("type") == "photo":
        kind = "image"
    else:
        kind = category.rstrip("s") if category != "unknown" else "unknown"

    out: dict[str, Any] = {
        "id": short(segment.get("id", "")),
        "kind": kind,
        "start": to_s(target.get("start")),
        "end": to_s((target.get("start") or 0) + (target.get("duration") or 0)),
        "duration": to_s(target.get("duration")),
        "source_start": to_s(source.get("start")),
        "speed": round(float(segment.get("speed") or 1.0), 4),
        "volume": round(float(v) if (v := segment.get("volume")) is not None else 1.0, 4),
        "visible": segment.get("visible", True),
        "render_index": segment.get("render_index", 0),
    }

    if category == "texts":
        out["text"] = _text_content(material)
        out["font_size"] = material.get("font_size")
    else:
        out["name"] = material.get("material_name") or material.get("name")
        out["path"] = material.get("path")
        crop = material.get("crop")
        if crop and (crop.get("upper_left_x", 0.0), crop.get("upper_left_y", 0.0),
                     crop.get("lower_right_x", 1.0), crop.get("lower_right_y", 1.0)) != (0.0, 0.0, 1.0, 1.0):
            out["crop"] = {
                "x": crop.get("upper_left_x", 0.0), "y": crop.get("upper_left_y", 0.0),
                "width": crop.get("lower_right_x", 1.0) - crop.get("upper_left_x", 0.0),
                "height": crop.get("lower_right_y", 1.0) - crop.get("upper_left_y", 0.0),
            }

    # Only surface transforms that differ from the identity, so a plain cut
    # reads as a plain cut.
    if scale.get("x", 1.0) != 1.0 or scale.get("y", 1.0) != 1.0:
        out["scale"] = {"x": scale.get("x", 1.0), "y": scale.get("y", 1.0)}
    if transform.get("x", 0.0) or transform.get("y", 0.0):
        out["position"] = {"x": transform.get("x", 0.0), "y": transform.get("y", 0.0)}
    if clip.get("rotation"):
        out["rotation"] = clip["rotation"]
    if clip.get("alpha", 1.0) != 1.0:
        out["alpha"] = clip["alpha"]

    if at is not None and segment.get("common_keyframes"):
        resolved = evaluate_keyframes(segment, at)
        if "scale" in resolved:
            out["scale"] = {**out.get("scale", {"x": 1.0, "y": 1.0}), **resolved["scale"]}
        if "position" in resolved:
            out["position"] = {**out.get("position", {"x": 0.0, "y": 0.0}), **resolved["position"]}
        if "rotation" in resolved:
            out["rotation"] = resolved["rotation"]
        if "alpha" in resolved:
            out["alpha"] = resolved["alpha"]

    if segment.get("common_keyframes"):
        out["keyframes"] = [b.get("property_type") for b in segment["common_keyframes"]]

    for ref in segment.get("extra_material_refs") or []:
        cat, mat = index.get(ref, (None, None))
        if cat == "transitions":
            out["transition"] = mat.get("name")
        elif cat == "material_animations":
            kinds = [a.get("type") for a in (mat.get("animations") or [])]
            if kinds:
                out["animations"] = kinds
        elif cat == "audio_effects":
            out["audio_effect"] = mat.get("name")
        elif cat == "canvases" and (mat.get("blur") or mat.get("color")):
            out["background"] = {"type": mat.get("type"), "blur": mat.get("blur"),
                                 "color": mat.get("color")}

    return out


def build(data: dict, project_name: str = "", at: float | None = None) -> dict:
    """Render the whole draft as the IR an agent reads.

    Args:
        at: If given, keyframed properties (position/scale/rotation/alpha)
            are resolved to their interpolated value at this absolute
            timeline instant, instead of the clip's static transform. Used by
            capcut_frame so a single still reflects in-motion keyframes;
            capcut_timeline and capcut_preview leave it unset.
    """
    index = _material_index(data)
    canvas = data.get("canvas_config") or {}

    tracks = []
    for i, track in enumerate(data.get("tracks") or []):
        clips = [describe_clip(s, index, at=at) for s in (track.get("segments") or [])]
        clips.sort(key=lambda c: c["start"])
        tracks.append(
            {
                "index": i,
                "type": track.get("type"),
                "id": short(track.get("id", "")),
                "clips": clips,
            }
        )

    return {
        "project": project_name,
        "width": canvas.get("width"),
        "height": canvas.get("height"),
        "fps": data.get("fps"),
        "duration": to_s(data.get("duration")),
        "tracks": tracks,
    }


def find_segment(data: dict, clip_id: str) -> tuple[dict, dict]:
    """Resolve a short clip id to its (track, segment), or raise."""
    wanted = clip_id.strip().lower()
    hits = []
    for track in data.get("tracks") or []:
        for segment in track.get("segments") or []:
            sid = (segment.get("id") or "").lower()
            if sid.startswith(wanted) or short(sid) == wanted:
                hits.append((track, segment))

    if not hits:
        raise KeyError(f"No clip with id starting '{clip_id}'")
    if len(hits) > 1:
        raise KeyError(f"Clip id '{clip_id}' is ambiguous ({len(hits)} matches)")
    return hits[0]


def recompute_duration(data: dict) -> int:
    """Project duration is the furthest clip end across all tracks."""
    end = 0
    for track in data.get("tracks") or []:
        for segment in track.get("segments") or []:
            tr = segment.get("target_timerange") or {}
            end = max(end, (tr.get("start") or 0) + (tr.get("duration") or 0))
    data["duration"] = end
    return end
