"""MCP surface for CapCut.

The session model mirrors how an editor actually works: open a project, make
edits against the in-memory draft, look at a preview, then save. Nothing
touches disk until ``capcut_save``, so a bad edit costs nothing, and every save
takes a timestamped backup first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import catalog
from . import draft as draft_mod
from . import edits, ir, paths, preview

mcp = MCPServer(
    "capcut",
    instructions=(
        "Read and edit local CapCut projects. Call capcut_probe first: it "
        "reports whether drafts are readable on this build and whether CapCut "
        "is running, which blocks saving. Edits accumulate in memory until "
        "capcut_save, and every save backs up the previous file. Use "
        "capcut_frame to look at the result of an edit before saving."
    ),
)

_open: dict[str, draft_mod.Draft] = {}
_scratch = Path(os.environ.get("CAPCUT_MCP_SCRATCH") or Path(tempfile.gettempdir()) / "capcut-mcp")


def _get(project: str) -> draft_mod.Draft:
    """Return the in-session draft, loading it on first use."""
    key = project.strip().lower()
    if key not in _open:
        _open[key] = draft_mod.Draft.open(project)
    return _open[key]


def _doc(d: draft_mod.Draft) -> dict:
    return ir.build(d.data, d.folder.name)


# ----------------------------------------------------------------- discovery


@mcp.tool()
def capcut_probe() -> dict:
    """Check the local CapCut setup before editing anything.

    Reports the installed version, where drafts live, whether they are readable
    plaintext JSON on this build, whether CapCut is currently running (which
    blocks saving), and whether ffmpeg is available for previews.
    """
    return paths.probe()


@mcp.tool()
def capcut_projects(limit: int = 20, contains: str = "") -> dict:
    """List CapCut projects, newest first.

    Args:
        limit: How many to return.
        contains: Only include projects whose name contains this text.
    """
    projects = paths.list_projects()
    if contains:
        needle = contains.lower()
        projects = [p for p in projects if needle in p["name"].lower()]
    return {"total": len(projects), "projects": projects[: max(1, limit)]}


@mcp.tool()
def capcut_timeline(project: str) -> dict:
    """Read a project's timeline: tracks, clips, timings and transforms.

    Times are in seconds. Each clip carries a short id used to address it in
    every edit tool.
    """
    return _doc(_get(project))


# --------------------------------------------------------------------- edits


@mcp.tool()
def capcut_add_clip(
    project: str,
    path: str,
    start: float,
    duration: float | None = None,
    source_start: float = 0.0,
    track_index: int | None = None,
    volume: float = 1.0,
    speed: float = 1.0,
) -> dict:
    """Place a video, image or audio file on the timeline.

    Args:
        path: Absolute path to the media file.
        start: Where the clip begins on the timeline, in seconds.
        duration: Clip length in seconds. Defaults to the remainder of the
            source for video/audio, or 5s for a still image.
        source_start: In-point within the source file, in seconds.
        track_index: Existing track to place it on. Omit to use the first
            track of the matching type, creating one if none exists.
    """
    d = _get(project)
    return edits.add_clip(d.data, path, start=start, duration=duration,
                          source_start=source_start, track_index=track_index,
                          volume=volume, speed=speed)


@mcp.tool()
def capcut_add_text(
    project: str,
    text: str,
    start: float,
    duration: float,
    font_size: float = 15.0,
    color: list[float] | None = None,
    position: list[float] | None = None,
    stroke_width: float = 0.06,
    track_index: int | None = None,
) -> dict:
    """Add a text overlay.

    Args:
        color: RGB in 0..1, e.g. [1, 1, 0] for yellow. Defaults to white.
        position: [x, y] in CapCut's normalised space where [0, 0] is the
            centre and y is positive upward. Defaults to [0, -0.7], lower third.
        stroke_width: Outline thickness; 0 disables the outline.
    """
    d = _get(project)
    return edits.add_text(
        d.data, text, start=start, duration=duration, font_size=font_size,
        color=tuple(color) if color else (1.0, 1.0, 1.0),
        position=tuple(position) if position else (0.0, -0.7),
        stroke=(0.0, 0.0, 0.0) if stroke_width > 0 else None,
        stroke_width=stroke_width, track_index=track_index,
    )


@mcp.tool()
def capcut_update_clip(
    project: str,
    clip_id: str,
    scale: float | None = None,
    position: list[float] | None = None,
    rotation: float | None = None,
    alpha: float | None = None,
    volume: float | None = None,
    speed: float | None = None,
    visible: bool | None = None,
) -> dict:
    """Change a clip's transform, opacity, volume, speed or visibility.

    Only the arguments you pass are applied. Changing speed rescales the clip's
    timeline length to match.
    """
    changes: dict[str, Any] = {
        "scale": scale, "rotation": rotation, "alpha": alpha,
        "volume": volume, "speed": speed, "visible": visible,
    }
    if position is not None:
        changes["position"] = {"x": position[0], "y": position[1]}
    return edits.update_clip(_get(project).data, clip_id,
                             **{k: v for k, v in changes.items() if v is not None})


@mcp.tool()
def capcut_move_clip(project: str, clip_id: str, start: float) -> dict:
    """Move a clip to a new start time, keeping its length and in-point."""
    return edits.move_clip(_get(project).data, clip_id, start)


@mcp.tool()
def capcut_trim_clip(
    project: str, clip_id: str, start: float | None = None, end: float | None = None
) -> dict:
    """Change a clip's in and out points on the timeline.

    Trimming the head also advances the source in-point, so the visible content
    stays aligned with the frame it was on.
    """
    return edits.trim_clip(_get(project).data, clip_id, start=start, end=end)


@mcp.tool()
def capcut_split_clip(project: str, clip_id: str, at: float) -> dict:
    """Cut a clip in two at an absolute timeline position, in seconds."""
    return edits.split_clip(_get(project).data, clip_id, at)


@mcp.tool()
def capcut_delete_clip(project: str, clip_id: str) -> dict:
    """Remove a clip and clean up any materials it alone was using."""
    return edits.delete_clip(_get(project).data, clip_id)


# --------------------------------------------------------- catalog / effects
#
# CapCut resolves every transition, filter/effect and entrance/exit animation
# against its own cloud catalog by an opaque id. There is no offline API to
# browse or mint that catalog, so these tools can only reapply something a
# human has already applied by hand at least once in CapCut -- see catalog.py.


@mcp.tool()
def capcut_catalog_search(query: str = "", kind: str = "") -> dict:
    """Find a transition, effect or animation you can reapply, by name.

    This only lists what is already cached from your own CapCut history --
    everything you or someone using this install has applied by hand at least
    once. It cannot discover an effect that has never been used before.

    Args:
        query: Substring to match, case-insensitive. Empty lists everything.
        kind: Restrict to "transition", "effect", "animation", "audio_effect",
            "filter" or "sticker". Empty searches all kinds.
    """
    hits = catalog.search(query, kind=kind or None, limit=40)
    return {"total": len(hits), "results": hits}


@mcp.tool()
def capcut_catalog_refresh() -> dict:
    """Rebuild the effect catalog from every local draft.

    Call this after applying a new effect by hand in CapCut, so
    capcut_catalog_search can find it. The catalog is otherwise cached and
    does not pick up new drafts automatically.
    """
    c = catalog.build(force=True)
    return {"entries": len(c["entries"])}


@mcp.tool()
def capcut_add_transition(project: str, clip_id: str, name: str, duration: float | None = None) -> dict:
    """Apply a transition between a clip and the next one on its track.

    `name` must match something in capcut_catalog_search(kind="transition").
    """
    return edits.add_transition(_get(project).data, clip_id, name, duration=duration)


@mcp.tool()
def capcut_add_effect(
    project: str, name: str, start: float, duration: float, track_index: int | None = None
) -> dict:
    """Overlay a video effect/filter over a time range (its own effect track).

    `name` must match something in capcut_catalog_search(kind="effect").
    """
    return edits.add_effect(_get(project).data, name, start, duration, track_index=track_index)


@mcp.tool()
def capcut_add_filter(
    project: str, name: str, start: float, duration: float, track_index: int | None = None
) -> dict:
    """Overlay a color filter/adjustment over a time range (its own adjust track).

    `name` must match something in capcut_catalog_search(kind="filter").
    Applying a second filter at the exact same [start, duration] stacks onto
    the same adjust segment, matching CapCut's own multi-filter behaviour.
    """
    return edits.add_filter(_get(project).data, name, start, duration, track_index=track_index)


@mcp.tool()
def capcut_set_crop(project: str, clip_id: str, x: float, y: float, width: float, height: float) -> dict:
    """Crop a clip to an axis-aligned rectangle.

    Useful for split-screen, picture-in-picture framing, or a rectangular
    mask -- CapCut's shaped-mask feature (circle, feathered edges, custom
    path) is not supported; this is rectangles only.

    Args:
        x, y, width, height: Normalised [0, 1], origin at the source frame's
            top-left. (0, 0, 1, 1) is the full, uncropped frame.

    Crop lives on the underlying media, not the clip on the timeline: if the
    same source file backs more than one clip, cropping one crops all of them.
    """
    return edits.set_crop(_get(project).data, clip_id, x, y, width, height)


@mcp.tool()
def capcut_add_animation(
    project: str, clip_id: str, name: str, kind: str = "", duration: float | None = None
) -> dict:
    """Apply an entrance/exit/loop animation to a clip.

    `name` must match something in capcut_catalog_search(kind="animation"). At
    most one animation per kind (in/out/loop) applies at a time; a second call
    of the same kind replaces the first rather than stacking.

    Args:
        kind: "in", "out" or "loop" to disambiguate when the name matches
            catalog entries of more than one kind.
    """
    return edits.add_clip_animation(_get(project).data, clip_id, name,
                                    kind=kind or None, duration=duration)


# --------------------------------------------------------------- keyframes
#
# Pure geometry CapCut's schema already exposes fully -- no catalog lookup,
# works for any project regardless of history.


@mcp.tool()
def capcut_set_keyframes(project: str, clip_id: str, property_type: str, points: list[dict]) -> dict:
    """Animate a clip's position, scale, rotation or opacity over time.

    Args:
        property_type: One of KFTypePositionX, KFTypePositionY, KFTypeScaleX,
            KFTypeScaleY, KFTypeRotation, KFTypeAlpha. The first three are
            confirmed against real CapCut drafts; the rest follow the same
            naming convention but are not individually confirmed -- check
            capcut_frame, and CapCut itself, before relying on them.
        points: At least two {"time": seconds_from_clip_start, "value": float}
            points. Position values are normalised [-1, 1] (matching
            capcut_update_clip's `position`); scale and alpha match its
            `scale`/`alpha`; rotation is in degrees. Linear interpolation
            between points.
    """
    return edits.set_keyframes(_get(project).data, clip_id, property_type, points)


@mcp.tool()
def capcut_clear_keyframes(project: str, clip_id: str, property_type: str = "") -> dict:
    """Remove keyframes from a clip: one property, or all of them if omitted."""
    return edits.clear_keyframes(_get(project).data, clip_id, property_type or None)


@mcp.tool()
def capcut_add_fade(project: str, clip_id: str, fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
    """Fade a video/image clip's opacity in and/or out, in seconds.

    Video/image only -- use capcut_add_audio_fade for audio clips.
    Implemented as KFTypeAlpha keyframes -- see capcut_set_keyframes's caveat
    about that specific property name not being individually confirmed.
    """
    return edits.add_fade(_get(project).data, clip_id, fade_in=fade_in, fade_out=fade_out)


@mcp.tool()
def capcut_add_audio_fade(
    project: str, clip_id: str, fade_in: float = 0.0, fade_out: float = 0.0, steps: int = 8
) -> dict:
    """Fade an audio clip's volume in and/or out, in seconds.

    No volume-keyframe type was found in any local draft (unlike position,
    scale and alpha, which share a confirmed naming convention), so this
    doesn't guess one. It approximates the fade with `steps` tiny sub-clips of
    graduated flat volume instead -- built entirely from mechanisms already
    confirmed elsewhere (split + volume). 8+ steps sound smooth; fewer gets
    audibly steppy on a slow fade.
    """
    return edits.add_audio_fade(_get(project).data, clip_id, fade_in=fade_in,
                                fade_out=fade_out, steps=steps)


@mcp.tool()
def capcut_set_denoise(project: str, clip_id: str, enabled: bool) -> dict:
    """Toggle CapCut's built-in real-time noise reduction on a clip.

    Unlike vocal separation (which needs CapCut to precompute a derived audio
    file first -- not supported here), denoise references a model file bundled
    with CapCut itself, the same one for every clip, so flipping this flag
    matches what CapCut's own "Reduce noise" toggle does. is_denoise:true is
    CapCut's default on nearly every clip; this has not been seen actually
    toggled in a real project, so confirm audibly on anything noise-critical.
    """
    return edits.set_denoise(_get(project).data, clip_id, enabled)


@mcp.tool()
def capcut_add_audio_effect(project: str, clip_id: str, name: str) -> dict:
    """Apply a cached audio effect (denoise, voice character, EQ preset) to a clip.

    `name` must match something in capcut_catalog_search(kind="audio_effect").
    """
    return edits.add_audio_effect(_get(project).data, clip_id, name)


@mcp.tool()
def capcut_add_sticker(
    project: str, name: str, start: float, duration: float,
    position: list[float] | None = None, scale: float = 1.0, track_index: int | None = None,
) -> dict:
    """Place a cached sticker/image overlay on its own sticker track.

    `name` must match something in capcut_catalog_search(kind="sticker").

    Args:
        position: [x, y] in the same normalised [-1, 1] space as
            capcut_update_clip's `position`. Defaults to centre.
    """
    return edits.add_sticker(_get(project).data, name, start, duration,
                             position=tuple(position) if position else (0.0, 0.0),
                             scale=scale, track_index=track_index)


@mcp.tool()
def capcut_speed_ramp(project: str, clip_id: str, points: list[dict]) -> dict:
    """Vary a clip's speed over time -- freeze frames, ramps, slow-mo punches.

    No smooth speed-curve shape was found in any local draft, so this
    achieves the effect by splitting the clip into flat-speed pieces instead
    (confirmed mechanism only) -- the cuts show up as separate clips in
    capcut_timeline but produce no visible seam on screen.

    Args:
        points: [{"time": seconds_from_clip_start, "speed": float}, ...], at
            least one point, in the clip's ORIGINAL (pre-ramp) timing. A point
            at time 0 sets the first piece's speed; later points are where
            the speed changes. E.g. a punch-in freeze: [{"time":0,"speed":1},
            {"time":2,"speed":0.05},{"time":2.3,"speed":1}].
    """
    return edits.speed_ramp(_get(project).data, clip_id, points)


@mcp.tool()
def capcut_set_background(
    project: str, clip_id: str, mode: str, color: str = "", blur: float | None = None
) -> dict:
    """Set a clip's canvas background fill -- what shows through when the clip
    doesn't cover the whole frame (e.g. 16:9 footage centred in a 9:16 canvas).

    Args:
        mode: "blur" (a blurred copy of the footage, CapCut's common default;
            `blur` 0-1, defaults to 0.375), "color" (solid `color` hex like
            "#101018"), or "none" to clear it back to plain black.
    """
    return edits.set_background(_get(project).data, clip_id, mode,
                                color=color or None, blur=blur)


@mcp.tool()
def capcut_set_track_muted(project: str, track_index: int, muted: bool) -> dict:
    """Mute/unmute every clip on a track at once.

    There is no track-level mute field in CapCut's schema -- this sets
    volume 0 (or restores each clip's last nonzero volume) across every
    segment on the track.
    """
    return edits.set_track_muted(_get(project).data, track_index, muted)


@mcp.tool()
def capcut_set_track_visible(project: str, track_index: int, visible: bool) -> dict:
    """Show/hide every clip on a track at once (sets `visible` on each segment)."""
    return edits.set_track_visible(_get(project).data, track_index, visible)


@mcp.tool()
def capcut_reorder_track(project: str, track_index: int, new_index: int) -> dict:
    """Move a track to a new stacking position -- later tracks draw on top.

    Recomputes render order across the whole project so it matches
    capcut_timeline's `tracks` order afterward.
    """
    return edits.reorder_track(_get(project).data, track_index, new_index)


# ---------------------------------------------------------------- verify/save


@mcp.tool()
def capcut_frame(project: str, at: float, height: int = 540) -> dict:
    """Render one instant of the timeline to a PNG and return its path.

    Read the returned image to check an edit landed as intended. Only clips
    live at that moment are composited, so this is fast enough for routine use.
    Keyframed position/scale/rotation/alpha are interpolated to this instant.

    The proxy honours layout, trims, speed, opacity, volume and keyframes, but
    not CapCut's own effects, filters, transitions or animation presets --
    those are visible once saved and opened in CapCut, not in this proxy.
    """
    d = _get(project)
    out = _scratch / d.folder.name / f"frame_{at:.2f}.png"
    return preview.frame(ir.build(d.data, d.folder.name, at=at), at, out, height=height)


@mcp.tool()
def capcut_preview(
    project: str,
    start: float = 0.0,
    duration: float | None = None,
    height: int = 360,
    with_audio: bool = True,
) -> dict:
    """Render a proxy video of the timeline, or a span of it, and return its path.

    Use a short span while iterating; a full-length render of a long project is
    slow. Same fidelity caveats as capcut_frame.
    """
    d = _get(project)
    span = "full" if duration is None else f"{start:.1f}-{start + duration:.1f}"
    out = _scratch / d.folder.name / f"preview_{span}_{height}p.mp4"
    return preview.render(_doc(d), out, start=start, duration=duration,
                          height=height, with_audio=with_audio)


@mcp.tool()
def capcut_save(project: str, force: bool = False) -> dict:
    """Write pending edits to the project, after backing up the current file.

    Refuses while CapCut is running, because the app flushes its own copy of the
    project on close and would discard the write. Close CapCut first, or pass
    force=True to accept that risk.
    """
    return _get(project).save(force=force)


@mcp.tool()
def capcut_revert(project: str, backup: str = "") -> dict:
    """Roll the project back to a backup, newest one unless you name a file.

    Also drops any unsaved in-session edits.
    """
    d = _get(project)
    result = d.restore(backup or None)
    _open.pop(project.strip().lower(), None)
    return result


@mcp.tool()
def capcut_backups(project: str) -> dict:
    """List available backups for a project, newest first."""
    return {"backups": _get(project).backups()}


@mcp.tool()
def capcut_discard(project: str) -> dict:
    """Drop unsaved in-session edits and reload the project from disk."""
    _open.pop(project.strip().lower(), None)
    return {"reloaded": _doc(_get(project))}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
