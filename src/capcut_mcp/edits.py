"""Mutations on a CapCut draft graph: add, update, move, split, trim, delete.

Every segment in a draft needs a set of companion materials alongside its own
media -- a speed, a canvas, a channel mapping and so on -- referenced through
``extra_material_refs``. CapCut will not display a segment whose companions are
missing, so the builders here always emit the full set observed in real drafts:

    video/image  speed, placeholder_info, realtime_denoise, canvas,
                 sound_channel_mapping, material_color, vocal_separation
    audio        speed, placeholder_info, realtime_denoise, beat,
                 sound_channel_mapping, vocal_separation
    text         material_animation

``realtime_denoise`` references a model file CapCut ships with itself (the
same file for every clip -- not a per-clip derived asset), so its presence
and default ``is_denoise: true`` here matches what CapCut itself writes for
essentially every clip. ``vocal_separation`` stays a genuine no-op stub
(``choice: 0``) -- unlike denoise, actually separating vocals/music requires
CapCut to run its own AI pipeline and write a derived audio file into the
project's own Resources folder first; there is nothing this module can fake
its way into for that one.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from . import catalog, ir, paths

IDENTITY_CLIP = {
    "alpha": 1.0,
    "flip": {"horizontal": False, "vertical": False},
    "rotation": 0.0,
    "scale": {"x": 1.0, "y": 1.0},
    "transform": {"x": 0.0, "y": 0.0},
}


def guid() -> str:
    return str(uuid.uuid4()).upper()


# --------------------------------------------------------------------- media


def probe_media(path: str) -> dict:
    """Read duration and dimensions via ffprobe."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=width,height,codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffprobe failed for {path}: {exc}") from exc

    if out.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path}: {out.stderr.strip()}")

    info = json.loads(out.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration") or 0.0)

    width = height = 0
    has_audio = False
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video" and not width:
            width, height = stream.get("width") or 0, stream.get("height") or 0
        if stream.get("codec_type") == "audio":
            has_audio = True

    return {
        "duration_us": int(round(duration * ir.US)),
        "width": width,
        "height": height,
        "has_audio": has_audio,
    }


# ---------------------------------------------------------------- companions


def _bucket(data: dict, name: str) -> list:
    return (data.setdefault("materials", {})).setdefault(name, [])


def _companions(data: dict, kind: str, duration_us: int) -> list[str]:
    """Create the companion materials a segment of this kind requires."""
    refs: list[str] = []

    def add(bucket: str, obj: dict) -> None:
        _bucket(data, bucket).append(obj)
        refs.append(obj["id"])

    add("speeds", {"id": guid(), "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None})
    add("placeholder_infos", {
        "id": guid(), "type": "placeholder_info", "meta_type": "none",
        "res_path": "", "res_text": "", "error_path": "", "error_text": "",
    })

    if kind == "audio":
        add("beats", {
            "id": guid(), "type": "beats", "mode": 404, "gear": 404, "gear_count": 0,
            "enable_ai_beats": False, "user_beats": [], "user_delete_ai_beats": None,
            "ai_beats": {
                "beat_speed_infos": [], "beats_path_v2": "", "beats_url": "",
                "melody_path": "", "melody_percents": [0.0], "melody_url": "",
            },
        })
    else:
        add("canvases", {
            "id": guid(), "type": "canvas_color", "color": "", "blur": 0.0, "image": "",
            "album_image": "", "image_id": "", "image_name": "", "source_platform": 0,
            "team_id": "",
        })

    model_path = paths.find_denoise_model() or ""
    add("realtime_denoises", {
        "id": guid(), "type": "realtime_denoise", "is_denoise": True, "denoise_mode": 1.0,
        "denoise_rate": 0.85, "path": model_path.replace("\\", "/"), "sami_name": "denoise_v2",
        "sami_version": "1.0", "sami_type": 2, "is_from_hd_sounds": False,
    })

    add("sound_channel_mappings", {
        "id": guid(), "type": "none", "audio_channel_mapping": 0, "is_config_open": False,
    })

    if kind != "audio":
        add("material_colors", {
            "id": guid(), "is_color_clip": False, "is_gradient": False, "solid_color": "",
            "gradient_colors": [], "gradient_percents": [], "gradient_angle": 90.0,
            "width": 0.0, "height": 0.0,
        })

    add("vocal_separations", {
        "id": guid(), "type": "vocal_separation", "choice": 0, "removed_sounds": [],
        "time_range": None, "production_path": "", "final_algorithm": "", "enter_from": "",
    })

    return refs


# -------------------------------------------------------------------- tracks


def _track_type_for(kind: str) -> str:
    return {"video": "video", "image": "video", "audio": "audio", "text": "text"}[kind]


def find_or_create_track(data: dict, kind: str, track_index: int | None = None) -> dict:
    """Return the requested track, creating one of the right type if needed."""
    tracks = data.setdefault("tracks", [])

    if track_index is not None:
        if 0 <= track_index < len(tracks):
            return tracks[track_index]
        raise IndexError(f"No track at index {track_index} (project has {len(tracks)})")

    ttype = _track_type_for(kind)
    for track in tracks:
        if track.get("type") == ttype:
            return track

    track = {
        "attribute": 0,
        "flag": 0,
        "id": guid(),
        "is_default_name": True,
        "name": "",
        "segments": [],
        "type": ttype,
    }
    tracks.append(track)
    return track


def _render_index(data: dict, track: dict) -> int:
    """CapCut layers by render_index; later tracks draw on top."""
    position = data["tracks"].index(track)
    return position * 1000


# --------------------------------------------------------------- add / clone


def add_clip(
    data: dict,
    path: str,
    *,
    start: float,
    duration: float | None = None,
    source_start: float = 0.0,
    track_index: int | None = None,
    volume: float = 1.0,
    speed: float = 1.0,
) -> dict:
    """Place a media file on the timeline."""
    media_path = Path(path)
    if not media_path.is_file():
        raise FileNotFoundError(f"Media not found: {media_path}")

    info = probe_media(str(media_path))
    suffix = media_path.suffix.lower()
    if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        kind = "audio"
    elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
        kind = "image"
    else:
        kind = "video"

    source_start_us = ir.to_us(source_start)
    if duration is not None:
        duration_us = ir.to_us(duration)
    elif kind == "image":
        duration_us = ir.to_us(5.0)
    else:
        duration_us = max(info["duration_us"] - source_start_us, 0)

    if duration_us <= 0:
        raise ValueError("Clip duration resolves to zero; check source_start and duration.")

    material_id = guid()
    bucket = "audios" if kind == "audio" else "videos"
    material: dict = {
        "id": material_id,
        "path": str(media_path).replace("\\", "/"),
        "material_name": media_path.name,
        "duration": info["duration_us"] or duration_us,
    }

    if kind == "audio":
        material.update({"type": "extract_music", "music_id": guid(), "local_material_id": ""})
    else:
        material.update({
            "type": "photo" if kind == "image" else "video",
            "width": info["width"],
            "height": info["height"],
            "has_audio": info["has_audio"],
            "crop": {
                "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                "upper_right_x": 1.0, "upper_right_y": 0.0,
            },
            "crop_ratio": "free",
            "crop_scale": 1.0,
            "category_name": "local",
            "check_flag": 63487,
        })

    _bucket(data, bucket).append(material)

    track = find_or_create_track(data, kind, track_index)
    segment = {
        "id": guid(),
        "material_id": material_id,
        "extra_material_refs": _companions(data, kind, duration_us),
        "clip": None if kind == "audio" else json.loads(json.dumps(IDENTITY_CLIP)),
        "common_keyframes": [],
        "keyframe_refs": [],
        "enable_adjust": kind != "audio",
        "enable_color_curves": True,
        "enable_color_wheels": True,
        "enable_lut": kind != "audio",
        "enable_video_mask": True,
        "group_id": "",
        "intensifies_audio": False,
        "is_loop": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "last_nonzero_volume": volume,
        "render_index": _render_index(data, track),
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": {"start": source_start_us, "duration": duration_us},
        "target_timerange": {"start": ir.to_us(start), "duration": ir.to_us(duration_us / ir.US / speed)},
        "speed": float(speed),
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": data["tracks"].index(track),
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": float(volume),
    }
    if kind == "audio":
        segment.pop("clip")

    track["segments"].append(segment)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    ir.recompute_duration(data)

    return {"clip_id": ir.short(segment["id"]), "kind": kind,
            "track_index": data["tracks"].index(track),
            "start": start, "duration": ir.to_s(segment["target_timerange"]["duration"])}


# ------------------------------------------------------------------- mutate


def update_clip(data: dict, clip_id: str, **changes) -> dict:
    """Adjust transform, opacity, volume, speed or visibility of one clip."""
    _, segment = ir.find_segment(data, clip_id)
    applied: dict = {}

    clip = segment.get("clip")
    if clip is None and any(k in changes for k in ("scale", "position", "rotation", "alpha")):
        raise ValueError("Audio clips have no visual transform.")

    if (scale := changes.get("scale")) is not None:
        value = {"x": float(scale), "y": float(scale)} if isinstance(scale, (int, float)) else \
                {"x": float(scale["x"]), "y": float(scale["y"])}
        clip["scale"] = value
        applied["scale"] = value

    if (position := changes.get("position")) is not None:
        value = {"x": float(position["x"]), "y": float(position["y"])}
        clip["transform"] = value
        applied["position"] = value

    if (rotation := changes.get("rotation")) is not None:
        clip["rotation"] = float(rotation)
        applied["rotation"] = float(rotation)

    if (alpha := changes.get("alpha")) is not None:
        clip["alpha"] = float(alpha)
        applied["alpha"] = float(alpha)

    if (volume := changes.get("volume")) is not None:
        segment["volume"] = float(volume)
        segment["last_nonzero_volume"] = float(volume) or segment.get("last_nonzero_volume", 1.0)
        applied["volume"] = float(volume)

    if (visible := changes.get("visible")) is not None:
        segment["visible"] = bool(visible)
        applied["visible"] = bool(visible)

    if (speed := changes.get("speed")) is not None:
        speed = float(speed)
        if speed <= 0:
            raise ValueError("speed must be positive")
        source = segment["source_timerange"]
        segment["speed"] = speed
        segment["target_timerange"]["duration"] = int(round(source["duration"] / speed))
        applied["speed"] = speed
        # Keep the companion speed material in step with the segment.
        for ref in segment.get("extra_material_refs", []):
            for material in data["materials"].get("speeds", []):
                if material["id"] == ref:
                    material["speed"] = speed

    ir.recompute_duration(data)
    return {"clip_id": ir.short(segment["id"]), "applied": applied}


def move_clip(data: dict, clip_id: str, start: float) -> dict:
    track, segment = ir.find_segment(data, clip_id)
    segment["target_timerange"]["start"] = ir.to_us(start)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    ir.recompute_duration(data)
    return {"clip_id": ir.short(segment["id"]), "start": start}


def trim_clip(data: dict, clip_id: str, *, start: float | None = None,
              end: float | None = None) -> dict:
    """Change a clip's in/out points, keeping its position on the timeline."""
    _, segment = ir.find_segment(data, clip_id)
    target, source = segment["target_timerange"], segment["source_timerange"]
    speed = float(segment.get("speed") or 1.0)

    head = target["start"]
    tail = head + target["duration"]
    new_head = ir.to_us(start) if start is not None else head
    new_tail = ir.to_us(end) if end is not None else tail

    if new_tail <= new_head:
        raise ValueError("Trim would leave a clip of zero or negative length.")

    # Trimming the head also advances the source in-point.
    source["start"] += int(round((new_head - head) * speed))
    source["duration"] = int(round((new_tail - new_head) * speed))
    target["start"], target["duration"] = new_head, new_tail - new_head

    ir.recompute_duration(data)
    return {"clip_id": ir.short(segment["id"]),
            "start": ir.to_s(new_head), "end": ir.to_s(new_tail)}


def split_clip(data: dict, clip_id: str, at: float) -> dict:
    """Cut one clip in two at an absolute timeline position."""
    track, segment = ir.find_segment(data, clip_id)
    target, source = segment["target_timerange"], segment["source_timerange"]
    cut = ir.to_us(at)

    head_len = cut - target["start"]
    if head_len <= 0 or head_len >= target["duration"]:
        raise ValueError(
            f"Split point {at}s is outside the clip "
            f"({ir.to_s(target['start'])}s..{ir.to_s(target['start'] + target['duration'])}s)."
        )

    speed = float(segment.get("speed") or 1.0)
    source_head = int(round(head_len * speed))

    tail = json.loads(json.dumps(segment))
    tail["id"] = guid()
    kind = "audio" if segment.get("clip") is None else "video"
    tail["extra_material_refs"] = _companions(data, kind, target["duration"] - head_len)
    tail["target_timerange"] = {"start": cut, "duration": target["duration"] - head_len}
    tail["source_timerange"] = {"start": source["start"] + source_head,
                                "duration": source["duration"] - source_head}

    target["duration"] = head_len
    source["duration"] = source_head

    track["segments"].append(tail)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])

    return {"head": ir.short(segment["id"]), "tail": ir.short(tail["id"]), "at": at}


def delete_clip(data: dict, clip_id: str) -> dict:
    """Remove a clip, and any materials left with no other referent."""
    track, segment = ir.find_segment(data, clip_id)
    track["segments"].remove(segment)

    orphans = {segment.get("material_id"), *segment.get("extra_material_refs", [])}
    orphans.discard(None)

    still_used: set[str] = set()
    for other_track in data.get("tracks") or []:
        for other in other_track.get("segments") or []:
            still_used.add(other.get("material_id"))
            still_used.update(other.get("extra_material_refs") or [])

    removable = orphans - still_used
    removed = 0
    for items in (data.get("materials") or {}).values():
        if not isinstance(items, list):
            continue
        keep = [m for m in items if not (isinstance(m, dict) and m.get("id") in removable)]
        removed += len(items) - len(keep)
        items[:] = keep

    ir.recompute_duration(data)
    return {"deleted": clip_id, "materials_removed": removed}


# ---------------------------------------------------------------------- text


def add_text(
    data: dict,
    text: str,
    *,
    start: float,
    duration: float,
    font_size: float = 15.0,
    color: tuple[float, float, float] = (1.0, 1.0, 1.0),
    position: tuple[float, float] = (0.0, -0.7),
    stroke: tuple[float, float, float] | None = (0.0, 0.0, 0.0),
    stroke_width: float = 0.06,
    font_path: str | None = None,
    track_index: int | None = None,
) -> dict:
    """Add a styled text overlay.

    CapCut stores the rendered string plus its styling as a JSON document inside
    the material's ``content`` field, which is why it is serialised here rather
    than written as plain columns.
    """
    style: dict = {
        "fill": {"content": {"render_type": "solid", "solid": {"color": list(color)}}},
        "size": float(font_size),
        "range": [0, len(text)],
        "useLetterColor": True,
    }
    if font_path:
        style["font"] = {"path": font_path.replace("\\", "/"), "id": ""}
    if stroke:
        style["strokes"] = [{
            "content": {"render_type": "solid", "solid": {"color": list(stroke)}},
            "width": float(stroke_width),
            "mode": 0,
        }]

    material_id = guid()
    _bucket(data, "texts").append({
        "id": material_id,
        "type": "text",
        "content": json.dumps({"text": text, "styles": [style]}, ensure_ascii=False),
        "base_content": text,
        "alignment": 1,
        "font_size": float(font_size),
        "font_path": (font_path or "").replace("\\", "/"),
        "text_color": "",
        "text_alpha": 1.0,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "typesetting": 0,
        "underline": False,
        "italic_degree": 0,
        "bold_width": 0.0,
        "has_shadow": False,
        "border_alpha": 1.0,
        "border_width": float(stroke_width),
        "background_alpha": 1.0,
        "global_alpha": 1.0,
        "group_id": "",
        "is_rich_text": False,
        "check_flag": 7,
        "add_type": 0,
        "sub_type": 0,
        "language": "",
        "layer_weight": 1,
        "recognize_type": 0,
        "shadow_alpha": 0.9,
        "shadow_angle": -45.0,
        "shadow_color": "",
        "shadow_distance": 5.0,
        "shadow_smoothing": 0.45,
        "style_name": "",
        "text_curve": None,
        "words": {"end_time": [], "start_time": [], "text": []},
    })

    animation_id = guid()
    _bucket(data, "material_animations").append({
        "id": animation_id,
        "type": "sticker_animation",
        "animations": [],
        "multi_language_current": "none",
    })

    track = find_or_create_track(data, "text", track_index)
    clip = json.loads(json.dumps(IDENTITY_CLIP))
    clip["transform"] = {"x": float(position[0]), "y": float(position[1])}

    segment = {
        "id": guid(),
        "material_id": material_id,
        "extra_material_refs": [animation_id],
        "clip": clip,
        "common_keyframes": [],
        "keyframe_refs": [],
        "enable_adjust": False,
        "enable_color_curves": True,
        "enable_color_wheels": True,
        "enable_lut": False,
        "enable_video_mask": True,
        "group_id": "",
        "intensifies_audio": False,
        "is_loop": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "last_nonzero_volume": 1.0,
        "render_index": _render_index(data, track) + 14000,
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": None,
        "target_timerange": {"start": ir.to_us(start), "duration": ir.to_us(duration)},
        "speed": 1.0,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": data["tracks"].index(track),
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": 1.0,
    }

    track["segments"].append(segment)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    ir.recompute_duration(data)

    return {"clip_id": ir.short(segment["id"]), "text": text,
            "track_index": data["tracks"].index(track), "start": start, "duration": duration}


# ------------------------------------------------------- catalog-backed edits
#
# Transitions, video effects/filters and entrance/exit animations are not
# assets we can construct from scratch: CapCut resolves them by an opaque
# resource_id against its own cloud catalog, cached locally only once a human
# has applied that effect by hand in the app (see catalog.py). Every builder
# below deep-copies the exact material CapCut itself produced for a past use,
# so the cache path, sub-parameters and category ids all stay correct -- only
# a fresh id (and, where it means something, a new duration) is substituted.


def add_transition(
    data: dict, clip_id: str, name_or_resource_id: str, duration: float | None = None
) -> dict:
    """Apply a transition between one clip and the next one on its track.

    CapCut renders the transition by overlapping the shared boundary at
    playback time; the two clips' own timerange stay untouched.
    """
    track, segment = ir.find_segment(data, clip_id)
    idx = track["segments"].index(segment)
    if idx == len(track["segments"]) - 1:
        raise ValueError("This is the last clip on its track; a transition needs a next clip.")

    entry = catalog.resolve(name_or_resource_id, kind="transition")
    material = json.loads(json.dumps(entry["template"]))
    material["id"] = guid()
    if duration is not None:
        material["duration"] = ir.to_us(duration)

    _bucket(data, "transitions").append(material)
    segment.setdefault("extra_material_refs", []).append(material["id"])

    return {"clip_id": ir.short(segment["id"]), "transition": entry["name"],
            "resource_id": entry["resource_id"], "duration": ir.to_s(material["duration"])}


def add_effect(
    data: dict, resource_id_or_name: str, start: float, duration: float,
    track_index: int | None = None,
) -> dict:
    """Overlay a video effect/filter over a time range, as its own track.

    This mirrors CapCut's own "effect" track: whatever is visually composited
    below it in that time range is affected, independent of clip boundaries.
    """
    entry = catalog.resolve(resource_id_or_name, kind="effect")
    material = json.loads(json.dumps(entry["template"]))
    material["id"] = guid()

    _bucket(data, "video_effects").append(material)

    track = next((t for t in data.get("tracks") or [] if t.get("type") == "effect"), None)
    if track is None or track_index is not None:
        tracks = data.setdefault("tracks", [])
        if track_index is not None and 0 <= track_index < len(tracks):
            track = tracks[track_index]
        else:
            track = {"attribute": 0, "flag": 0, "id": guid(), "is_default_name": True,
                     "name": "", "segments": [], "type": "effect"}
            tracks.append(track)

    segment = {
        "id": guid(),
        "material_id": material["id"],
        "extra_material_refs": [],
        "clip": None,
        "common_keyframes": [],
        "keyframe_refs": [],
        "enable_adjust": False,
        "enable_color_curves": True,
        "enable_color_wheels": True,
        "enable_lut": False,
        "enable_video_mask": True,
        "group_id": "",
        "intensifies_audio": False,
        "is_loop": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "last_nonzero_volume": 1.0,
        "render_index": _render_index(data, track) + 11000,
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
                              "target_follow": "", "vertical_pos_layout": 0},
        "reverse": False,
        "source_timerange": None,
        "target_timerange": {"start": ir.to_us(start), "duration": ir.to_us(duration)},
        "speed": 1.0,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": data["tracks"].index(track),
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": 1.0,
    }
    track["segments"].append(segment)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    ir.recompute_duration(data)

    return {"clip_id": ir.short(segment["id"]), "effect": entry["name"],
            "resource_id": entry["resource_id"], "start": start, "duration": duration}


def add_filter(
    data: dict, resource_id_or_name: str, start: float, duration: float,
    track_index: int | None = None,
) -> dict:
    """Overlay a color filter/adjustment over a time range.

    CapCut renders filters on their own "adjust" track, separate from the
    "effect" track used by capcut_add_effect. Multiple filters at the exact
    same [start, duration] window stack onto one adjust segment (CapCut's own
    "add another filter here" behaviour); a different window gets a new one.
    """
    entry = catalog.resolve(resource_id_or_name, kind="filter")
    material = json.loads(json.dumps(entry["template"]))
    material["id"] = guid()
    _bucket(data, "effects").append(material)

    tracks = data.setdefault("tracks", [])
    track = None
    if track_index is not None and 0 <= track_index < len(tracks):
        track = tracks[track_index]
    else:
        track = next((t for t in tracks if t.get("type") == "adjust"), None)
    if track is None:
        track = {"attribute": 0, "flag": 0, "id": guid(), "is_default_name": True,
                 "name": "", "segments": [], "type": "adjust"}
        tracks.append(track)

    start_us, duration_us = ir.to_us(start), ir.to_us(duration)
    segment = next(
        (s for s in track["segments"]
         if s["target_timerange"]["start"] == start_us
         and s["target_timerange"]["duration"] == duration_us),
        None,
    )
    if segment is None:
        placeholder_id = guid()
        _bucket(data, "placeholders").append({
            "id": placeholder_id, "name": f"Ajuste{len(track['segments']) + 1}",
            "type": "adjust", "material_resource_id": "",
        })
        segment = {
            "id": guid(),
            "material_id": placeholder_id,
            "extra_material_refs": [],
            "clip": None,
            "common_keyframes": [],
            "keyframe_refs": [],
            "enable_adjust": True,
            "enable_color_curves": True,
            "enable_color_wheels": True,
            "enable_lut": True,
            "enable_video_mask": True,
            "group_id": "",
            "intensifies_audio": False,
            "is_loop": False,
            "is_placeholder": False,
            "is_tone_modify": False,
            "last_nonzero_volume": 1.0,
            "render_index": _render_index(data, track),
            "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                                  "size_layout": 0, "target_follow": "",
                                  "vertical_pos_layout": 0},
            "reverse": False,
            "source_timerange": None,
            "target_timerange": {"start": start_us, "duration": duration_us},
            "speed": 1.0,
            "template_id": "",
            "template_scene": "default",
            "track_attribute": 0,
            "track_render_index": data["tracks"].index(track),
            "uniform_scale": {"on": True, "value": 1.0},
            "visible": True,
            "volume": 1.0,
        }
        track["segments"].append(segment)
        track["segments"].sort(key=lambda s: s["target_timerange"]["start"])

    segment["extra_material_refs"].append(material["id"])
    ir.recompute_duration(data)

    return {"clip_id": ir.short(segment["id"]), "filter": entry["name"],
            "resource_id": entry["resource_id"], "start": start, "duration": duration,
            "stacked_on_existing": len(segment["extra_material_refs"]) > 1}


def set_crop(data: dict, clip_id: str, x: float, y: float, width: float, height: float) -> dict:
    """Crop a clip to an axis-aligned rectangle -- split-screen, PiP framing, or
    a rectangular mask, without CapCut's shaped-mask feature (not yet
    supported -- see README).

    Args:
        x, y, width, height: Normalised [0, 1], origin at the top-left of the
            source frame. (0, 0, 1, 1) is the full, uncropped frame.

    Crop lives on the underlying media, not the clip: if the same source file
    backs more than one clip on the timeline, cropping one crops all of them.
    """
    if not (0 <= x < x + width <= 1 and 0 <= y < y + height <= 1):
        raise ValueError("x/y/width/height must describe a rectangle within [0, 1].")

    _, segment = ir.find_segment(data, clip_id)
    if segment.get("clip") is None:
        raise ValueError("Audio clips have no crop.")

    material = None
    for items in data.get("materials", {}).get("videos", []):
        if items.get("id") == segment.get("material_id"):
            material = items
            break
    if material is None:
        raise ValueError("Could not find this clip's underlying media material.")

    x2, y2 = x + width, y + height
    material["crop"] = {
        "upper_left_x": x, "upper_left_y": y, "upper_right_x": x2, "upper_right_y": y,
        "lower_left_x": x, "lower_left_y": y2, "lower_right_x": x2, "lower_right_y": y2,
    }
    material["crop_ratio"] = "free"

    return {"clip_id": ir.short(segment["id"]), "crop": {"x": x, "y": y, "width": width, "height": height}}


def add_clip_animation(
    data: dict, clip_id: str, name_or_resource_id: str,
    kind: str | None = None, duration: float | None = None,
) -> dict:
    """Apply an entrance ("in"), exit ("out") or looping animation to a clip.

    CapCut allows at most one animation of each kind per clip; applying a
    second "in" animation replaces the first rather than stacking.

    Args:
        kind: Restrict catalog resolution to this animation kind when the
            name is ambiguous. The chosen entry's own recorded kind is what
            actually gets written, since "in" and "out" variants of the same
            visual effect are normally distinct catalog resource_ids.
    """
    _, segment = ir.find_segment(data, clip_id)
    entry = catalog.resolve(name_or_resource_id, kind="animation", anim_kind=kind)
    anim = json.loads(json.dumps(entry["template"]))
    anim["id"] = guid()
    anim_type = anim.get("type", "in")

    clip_duration_us = (segment.get("target_timerange") or {}).get("duration", 0)
    if duration is not None:
        anim["duration"] = ir.to_us(duration)
    anim["duration"] = min(anim.get("duration") or 0, clip_duration_us) or anim.get("duration")

    if anim_type == "out":
        anim["start"] = max(clip_duration_us - anim["duration"], 0)
    elif anim_type == "loop":
        anim["start"], anim["duration"] = 0, clip_duration_us
    else:
        anim["start"] = 0

    container = None
    for ref in segment.get("extra_material_refs", []):
        for m in data.get("materials", {}).get("material_animations", []):
            if m["id"] == ref:
                container = m
                break
        if container:
            break

    if container is None:
        container = {"id": guid(), "type": "sticker_animation", "animations": [],
                    "multi_language_current": "none"}
        _bucket(data, "material_animations").append(container)
        segment.setdefault("extra_material_refs", []).append(container["id"])

    container["animations"] = [a for a in container["animations"] if a.get("type") != anim_type]
    container["animations"].append(anim)

    return {"clip_id": ir.short(segment["id"]), "animation": entry["name"], "kind": anim_type,
            "resource_id": entry["resource_id"], "duration": ir.to_s(anim["duration"])}


# --------------------------------------------------------------- keyframes
#
# Unlike effects/transitions, keyframes are pure geometry CapCut's schema
# already exposes in full: a property_type name, a list of (time, value)
# points and a curve type. Nothing here depends on CapCut's cloud catalog, so
# this is fully general -- an agent can animate any of these properties
# without anything having been applied by hand first.
#
# Empirically confirmed against real local drafts: KFTypePositionX,
# KFTypePositionY, KFTypeScaleX. KFTypeScaleY, KFTypeRotation and KFTypeAlpha
# follow the same naming convention and the same per-property static fields
# on a segment's own `clip` (scale.x/y, rotation, alpha), but have not been
# individually confirmed -- open the project in CapCut once after using them
# to check the motion looks right before relying on it in production.

KEYFRAME_TYPES = {
    "KFTypePositionX", "KFTypePositionY", "KFTypeScaleX", "KFTypeScaleY",
    "KFTypeRotation", "KFTypeAlpha",
}


def set_keyframes(
    data: dict, clip_id: str, property_type: str, points: list[dict], curve: str = "Line",
) -> dict:
    """Replace a clip's keyframe track for one property.

    Args:
        property_type: One of KEYFRAME_TYPES. Position is in the same
            normalised [-1, 1] space as capcut_update_clip's `position`;
            scale and alpha match its `scale`/`alpha`; rotation is in degrees.
        points: [{"time": seconds_from_clip_start, "value": float}, ...],
            at least two points, sorted or not -- they are sorted by time.
        curve: "Line" for linear interpolation (the only kind seen in local
            drafts; CapCut's editor also offers eased curves under the hood
            but their curveType strings were not present in any local sample).
    """
    if property_type not in KEYFRAME_TYPES:
        raise ValueError(f"Unknown property_type '{property_type}'. Use one of: "
                         f"{', '.join(sorted(KEYFRAME_TYPES))}")
    if len(points) < 2:
        raise ValueError("Need at least two points to animate a property over time.")

    _, segment = ir.find_segment(data, clip_id)
    ordered = sorted(points, key=lambda p: p["time"])

    block = {
        "id": guid(),
        "material_id": "",
        "property_type": property_type,
        "keyframe_list": [
            {
                "id": guid(),
                "curveType": curve,
                "time_offset": ir.to_us(p["time"]),
                "left_control": {"x": 0.0, "y": 0.0},
                "right_control": {"x": 0.0, "y": 0.0},
                "values": [float(p["value"])],
                "string_value": "",
                "graphID": "",
            }
            for p in ordered
        ],
    }

    segment["common_keyframes"] = [
        b for b in segment.get("common_keyframes", []) if b.get("property_type") != property_type
    ]
    segment["common_keyframes"].append(block)

    return {"clip_id": ir.short(segment["id"]), "property_type": property_type,
            "points": len(ordered)}


def clear_keyframes(data: dict, clip_id: str, property_type: str | None = None) -> dict:
    """Remove keyframes from a clip: one property, or all of them."""
    _, segment = ir.find_segment(data, clip_id)
    before = len(segment.get("common_keyframes", []))
    if property_type:
        segment["common_keyframes"] = [
            b for b in segment.get("common_keyframes", [])
            if b.get("property_type") != property_type
        ]
    else:
        segment["common_keyframes"] = []
    removed = before - len(segment["common_keyframes"])
    return {"clip_id": ir.short(segment["id"]), "removed": removed}


def add_fade(data: dict, clip_id: str, fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
    """Fade a video/image clip's opacity in and/or out, via KFTypeAlpha keyframes.

    Video/image only -- audio clips have no `clip.alpha` at all; use
    add_audio_fade for those. KFTypeAlpha is inferred from CapCut's naming
    convention (confirmed: KFTypePositionX/Y, KFTypeScaleX) but not itself
    individually confirmed -- check the result with capcut_frame, and
    visually in CapCut, before relying on it.
    """
    _, segment = ir.find_segment(data, clip_id)
    if segment.get("clip") is None:
        raise ValueError("This is an audio clip -- it has no opacity. Use add_audio_fade instead.")
    duration_s = ir.to_s((segment.get("target_timerange") or {}).get("duration"))
    base_alpha = (segment.get("clip") or {}).get("alpha", 1.0)

    points: list[dict] = []
    if fade_in > 0:
        points += [{"time": 0.0, "value": 0.0}, {"time": fade_in, "value": base_alpha}]
    if fade_out > 0:
        start = max(duration_s - fade_out, points[-1]["time"] if points else 0.0)
        points += [{"time": start, "value": base_alpha}, {"time": duration_s, "value": 0.0}]

    if not points:
        raise ValueError("Pass fade_in and/or fade_out greater than zero.")

    return set_keyframes(data, clip_id, "KFTypeAlpha", points)


def add_audio_fade(data: dict, clip_id: str, fade_in: float = 0.0, fade_out: float = 0.0,
                   steps: int = 8) -> dict:
    """Fade an audio clip's volume in and/or out.

    No audio-volume keyframe type was found in any local draft (unlike
    position/scale/alpha, which follow a confirmed naming convention), so
    this does not guess one. Instead it splits the fade region into `steps`
    tiny segments with a graduated flat `volume` -- a stepped approximation
    built entirely from confirmed, already-used mechanisms (split + volume),
    audible as a fade rather than a click at normal step counts (8+).
    """
    _, segment = ir.find_segment(data, clip_id)
    if segment.get("clip") is not None:
        raise ValueError("This is a video/image clip -- use add_fade for opacity instead.")
    if fade_in <= 0 and fade_out <= 0:
        raise ValueError("Pass fade_in and/or fade_out greater than zero.")

    target = segment["target_timerange"]
    clip_start_s, clip_dur_s = ir.to_s(target["start"]), ir.to_s(target["duration"])
    if fade_in + fade_out > clip_dur_s:
        raise ValueError(
            f"fade_in ({fade_in}s) + fade_out ({fade_out}s) exceeds this clip's own "
            f"duration ({clip_dur_s}s)."
        )
    base_volume = segment.get("volume", 1.0)
    result: dict = {"clip_id": ir.short(segment["id"]), "steps": steps}

    def _ramp(region_start: float, region_end: float, ascending: bool) -> str:
        region_dur = region_end - region_start
        cursor = clip_id
        for i in range(steps):
            frac_lo, frac_hi = i / steps, (i + 1) / steps
            cut_at = region_start + region_dur * frac_hi
            level = frac_hi if ascending else 1.0 - frac_hi
            if i < steps - 1:
                pieces = split_clip(data, cursor, cut_at)
                head_id = pieces["head"]
                cursor = pieces["tail"]
            else:
                head_id = cursor
            update_clip(data, head_id, volume=base_volume * level)
        return cursor

    if fade_in > 0:
        clip_id = _ramp(clip_start_s, clip_start_s + fade_in, ascending=True)
        result["fade_in_end_clip"] = clip_id
    if fade_out > 0:
        # Use the current piece's own start, not the pre-fade-in clip_start_s
        # -- fade_in above may have carved the front off, moving it forward.
        _, seg_now = ir.find_segment(data, clip_id)
        tr_now = seg_now["target_timerange"]
        start_now_s = ir.to_s(tr_now["start"])
        end_s = ir.to_s(tr_now["start"] + tr_now["duration"])
        region_start = max(end_s - fade_out, start_now_s)
        _ramp(region_start, end_s, ascending=False)

    return result


def add_audio_effect(data: dict, clip_id: str, name_or_resource_id: str) -> dict:
    """Apply a cached audio effect (denoise, voice character, EQ preset...) to a clip.

    `name_or_resource_id` must match something in
    capcut_catalog_search(kind="audio_effect") -- catalog-bound like
    transitions/filters/animations, not a general audio processing tool.
    """
    _, segment = ir.find_segment(data, clip_id)
    if segment.get("clip") is not None:
        raise ValueError("Audio effects apply to audio clips.")

    entry = catalog.resolve(name_or_resource_id, kind="audio_effect")
    material = json.loads(json.dumps(entry["template"]))
    material["id"] = guid()
    _bucket(data, "audio_effects").append(material)
    segment.setdefault("extra_material_refs", []).append(material["id"])

    return {"clip_id": ir.short(segment["id"]), "audio_effect": entry["name"],
            "resource_id": entry["resource_id"]}


def add_sticker(
    data: dict, name_or_resource_id: str, start: float, duration: float,
    position: tuple[float, float] = (0.0, 0.0), scale: float = 1.0,
    track_index: int | None = None,
) -> dict:
    """Place a cached sticker/image overlay on its own sticker track.

    `name_or_resource_id` must match something in
    capcut_catalog_search(kind="sticker"). Sticker segments carry a full
    transform (position/scale/rotation/alpha), same as capcut_update_clip.
    """
    entry = catalog.resolve(name_or_resource_id, kind="sticker")
    material = json.loads(json.dumps(entry["template"]))
    material["id"] = guid()
    _bucket(data, "stickers").append(material)

    tracks = data.setdefault("tracks", [])
    track = None
    if track_index is not None and 0 <= track_index < len(tracks):
        track = tracks[track_index]
    else:
        track = next((t for t in tracks if t.get("type") == "sticker"), None)
    if track is None:
        track = {"attribute": 0, "flag": 0, "id": guid(), "is_default_name": True,
                 "name": "", "segments": [], "type": "sticker"}
        tracks.append(track)

    clip = json.loads(json.dumps(IDENTITY_CLIP))
    clip["scale"] = {"x": float(scale), "y": float(scale)}
    clip["transform"] = {"x": float(position[0]), "y": float(position[1])}

    segment = {
        "id": guid(),
        "material_id": material["id"],
        "extra_material_refs": [],
        "clip": clip,
        "common_keyframes": [],
        "keyframe_refs": [],
        "enable_adjust": False,
        "enable_color_curves": True,
        "enable_color_wheels": True,
        "enable_lut": False,
        "enable_video_mask": True,
        "group_id": "",
        "intensifies_audio": False,
        "is_loop": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "last_nonzero_volume": 1.0,
        "render_index": _render_index(data, track) + 15000,
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
                              "target_follow": "", "vertical_pos_layout": 0},
        "reverse": False,
        "source_timerange": None,
        "target_timerange": {"start": ir.to_us(start), "duration": ir.to_us(duration)},
        "speed": 1.0,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": data["tracks"].index(track),
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": 1.0,
    }
    track["segments"].append(segment)
    track["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    ir.recompute_duration(data)

    return {"clip_id": ir.short(segment["id"]), "sticker": entry["name"],
            "resource_id": entry["resource_id"], "start": start, "duration": duration}


def speed_ramp(data: dict, clip_id: str, points: list[dict]) -> dict:
    """Vary a clip's speed over time by splitting it into flat-speed pieces.

    No populated `curve_speed` (CapCut's smooth speed-ramp curve) was found in
    any local draft, so this does not guess that shape. Instead it achieves
    the same creative effect -- freeze frames, speed ramps, slow-mo punches --
    with confirmed mechanisms only: splitting the clip at each point's time
    and setting a flat `speed` on each resulting piece. The cuts are real
    (visible in capcut_timeline as separate clips) but produce no visible seam
    on screen since it is the same continuous source underneath.

    Args:
        points: [{"time": seconds_from_clip_start, "speed": float}, ...] in
            the clip's ORIGINAL (pre-ramp) timing, at least one point. A point
            at time 0 sets the first segment's speed; later points are where
            the speed changes.
    """
    if not points:
        raise ValueError("Need at least one {time, speed} point.")
    ordered = sorted(points, key=lambda p: p["time"])
    if ordered[0]["time"] > 0:
        ordered.insert(0, {"time": 0.0, "speed": 1.0})

    _, segment = ir.find_segment(data, clip_id)
    clip_start_s = ir.to_s(segment["target_timerange"]["start"])
    clip_dur_s = ir.to_s(segment["target_timerange"]["duration"])
    ordered = [p for p in ordered if p["time"] < clip_dur_s]

    # Split first, at absolute positions on the *original* timeline -- once a
    # piece's speed changes its duration shifts everything after it, so every
    # cut must be made before any speed is touched.
    pieces = []
    cursor = clip_id
    for point in ordered[1:]:
        result = split_clip(data, cursor, clip_start_s + point["time"])
        pieces.append(result["head"])
        cursor = result["tail"]
    pieces.append(cursor)

    applied = []
    for piece_id, point in zip(pieces, ordered):
        update_clip(data, piece_id, speed=point["speed"])
        applied.append({"clip_id": piece_id, "speed": point["speed"]})

    return {"original_clip_id": clip_id, "pieces": applied}


def set_background(
    data: dict, clip_id: str, mode: str, color: str | None = None, blur: float | None = None,
) -> dict:
    """Set a clip's canvas background fill -- the blurred/coloured backdrop
    that shows through when the clip doesn't fill the frame (e.g. landscape
    footage centred in a portrait canvas).

    Args:
        mode: "blur" (use `blur`, 0-1, default 0.375 to match CapCut's own
            default), "color" (use `color`, hex like "#101018"), or "none" to
            clear it back to plain black.
    """
    _, segment = ir.find_segment(data, clip_id)
    if segment.get("clip") is None:
        raise ValueError("Audio clips have no canvas background.")

    canvas = None
    for ref in segment.get("extra_material_refs", []):
        for m in data.get("materials", {}).get("canvases", []):
            if m["id"] == ref:
                canvas = m
                break
        if canvas:
            break
    if canvas is None:
        raise ValueError("This clip has no canvas companion material (unexpected for a video/image clip).")

    if mode == "blur":
        canvas.update({"type": "canvas_blur", "blur": blur if blur is not None else 0.375,
                       "color": "", "image": ""})
    elif mode == "color":
        if not color:
            raise ValueError("mode='color' needs a hex color, e.g. '#101018'.")
        canvas.update({"type": "canvas_color", "color": color, "blur": 0.0, "image": ""})
    elif mode == "none":
        canvas.update({"type": "canvas_color", "color": "", "blur": 0.0, "image": ""})
    else:
        raise ValueError("mode must be 'blur', 'color' or 'none'.")

    return {"clip_id": ir.short(segment["id"]), "mode": mode}


def set_denoise(data: dict, clip_id: str, enabled: bool) -> dict:
    """Toggle CapCut's built-in real-time noise reduction on a clip.

    Unlike vocal separation, denoise references a model file CapCut ships
    with itself -- the same file for every clip, not a per-clip derived
    output -- so there is nothing to precompute; this flag is what CapCut
    itself flips when you toggle "Reduce noise" in the UI. `is_denoise: true`
    is the default on ~every real clip observed locally (2210 of 2211
    samples), so this has not been seen actually toggled off-then-on in a
    real draft -- confirm audibly (or in CapCut) on anything noise-critical.

    Repairs a clip missing its realtime_denoise companion (e.g. one from an
    older version of this tool) by creating one rather than failing.
    """
    _, segment = ir.find_segment(data, clip_id)

    material = None
    for ref in segment.get("extra_material_refs", []):
        for m in data.get("materials", {}).get("realtime_denoises", []):
            if m["id"] == ref:
                material = m
                break
        if material:
            break

    if material is None:
        model_path = (paths.find_denoise_model() or "").replace("\\", "/")
        material = {
            "id": guid(), "type": "realtime_denoise", "is_denoise": bool(enabled),
            "denoise_mode": 1.0, "denoise_rate": 0.85, "path": model_path,
            "sami_name": "denoise_v2", "sami_version": "1.0", "sami_type": 2,
            "is_from_hd_sounds": False,
        }
        _bucket(data, "realtime_denoises").append(material)
        segment.setdefault("extra_material_refs", []).append(material["id"])
        repaired = True
    else:
        material["is_denoise"] = bool(enabled)
        repaired = False

    return {"clip_id": ir.short(segment["id"]), "denoise": bool(enabled), "repaired_companion": repaired}


# ------------------------------------------------------------------- tracks


def set_track_muted(data: dict, track_index: int, muted: bool) -> dict:
    """Mute/unmute every clip on a track (there is no track-level mute field
    in CapCut's schema -- this applies volume 0, or restores each segment's
    last nonzero volume, across every segment on the track)."""
    tracks = data.get("tracks") or []
    if not (0 <= track_index < len(tracks)):
        raise IndexError(f"No track at index {track_index}")
    track = tracks[track_index]

    changed = 0
    for seg in track["segments"]:
        if muted:
            if seg.get("volume", 1.0) > 0:
                seg["last_nonzero_volume"] = seg["volume"]
            seg["volume"] = 0.0
        else:
            seg["volume"] = seg.get("last_nonzero_volume") or 1.0
        changed += 1

    return {"track_index": track_index, "muted": muted, "clips_changed": changed}


def set_track_visible(data: dict, track_index: int, visible: bool) -> dict:
    """Show/hide every clip on a track (there is no track-level visibility
    field -- this sets `visible` on every segment on the track)."""
    tracks = data.get("tracks") or []
    if not (0 <= track_index < len(tracks)):
        raise IndexError(f"No track at index {track_index}")
    track = tracks[track_index]
    for seg in track["segments"]:
        seg["visible"] = visible
    return {"track_index": track_index, "visible": visible, "clips_changed": len(track["segments"])}


def reorder_track(data: dict, track_index: int, new_index: int) -> dict:
    """Move a track to a new stacking position (later tracks draw on top).

    Recomputes every segment's render_index across the whole project to match
    the new order, preserving each track type's existing offset convention
    (text +14000, sticker +15000, effect +11000) so relative layering within
    a track type is unaffected.
    """
    tracks = data.get("tracks") or []
    if not (0 <= track_index < len(tracks) and 0 <= new_index < len(tracks)):
        raise IndexError(f"Track index out of range (project has {len(tracks)} tracks)")

    track = tracks.pop(track_index)
    tracks.insert(new_index, track)

    offsets = {"text": 14000, "sticker": 15000, "effect": 11000}
    for i, t in enumerate(tracks):
        base = i * 1000 + offsets.get(t.get("type"), 0)
        for seg in t["segments"]:
            seg["render_index"] = base
            seg["track_render_index"] = i

    return {"moved_from": track_index, "moved_to": new_index,
            "order": [t.get("type") for t in tracks]}
