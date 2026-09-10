"""Render the timeline with ffmpeg so an agent can see what it just edited.

This is the verification half of the loop. CapCut itself cannot be asked to
render headlessly, so every edit would otherwise be blind until a human opens
the app. Compiling the same IR to ffmpeg gives a proxy the agent can actually
look at, and keeps the pipeline useful even when CapCut is closed or busy.

The proxy is an approximation, not a match for CapCut's renderer: transforms,
trims, speed, opacity and audio levels are honoured, while CapCut's proprietary
effects, filters, transitions and animation presets are not. It answers "is the
edit structurally right", not "is the grade right".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

VISUAL_KINDS = {"video", "photo", "image"}
AUDIO_KINDS = {"audio", "video"}


class PreviewError(RuntimeError):
    pass


def _escape_drawtext(text: str) -> str:
    out = text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    return out.replace("%", r"\%").replace(",", r"\,")


def _visual_clips(doc: dict) -> list[dict]:
    """Visual clips in draw order: lower tracks first, then render_index."""
    clips = []
    for track in doc["tracks"]:
        if track["type"] not in {"video", "text"}:
            continue
        for clip in track["clips"]:
            if not clip.get("visible", True):
                continue
            if track["type"] == "text" or clip["kind"] in VISUAL_KINDS:
                clips.append({**clip, "_track": track["index"], "_type": track["type"]})
    clips.sort(key=lambda c: (c["_track"], c.get("render_index", 0)))
    return clips


def _audio_clips(doc: dict) -> list[dict]:
    clips = []
    for track in doc["tracks"]:
        for clip in track["clips"]:
            if clip["kind"] in AUDIO_KINDS and clip.get("path") and clip.get("volume", 1.0) > 0:
                clips.append(clip)
    return clips


def build_command(
    doc: dict,
    out_path: Path,
    *,
    height: int = 360,
    start: float = 0.0,
    duration: float | None = None,
    with_audio: bool = True,
    font: str | None = None,
) -> list[str]:
    src_w, src_h = doc.get("width") or 1920, doc.get("height") or 1080
    fps = doc.get("fps") or 30
    scale = height / src_h
    width = int(round(src_w * scale / 2)) * 2

    total = doc.get("duration") or 0.0
    end = total if duration is None else min(start + duration, total)
    span = max(end - start, 0.04)

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters: list[str] = []

    args += ["-f", "lavfi", "-t", f"{span:.3f}", "-i", f"color=c=black:s={width}x{height}:r={fps}"]
    base = "[0:v]"
    inputs = 1
    audio_labels: list[str] = []

    for clip in _visual_clips(doc):
        # Timeline positions are absolute; the proxy window may start later.
        clip_start = clip["start"] - start
        clip_end = clip["end"] - start
        if clip_end <= 0 or clip_start >= span:
            continue
        visible_from, visible_to = max(clip_start, 0.0), min(clip_end, span)

        if clip["_type"] == "text":
            label = f"[t{inputs}]"
            size = int(round((clip.get("font_size") or 15) * scale * 3.2))
            x = int(round(width / 2 + clip.get("position", {}).get("x", 0.0) * width / 2))
            y = int(round(height / 2 - clip.get("position", {}).get("y", 0.0) * height / 2))
            draw = (
                f"drawtext=text='{_escape_drawtext(clip.get('text', ''))}'"
                f":fontsize={size}:fontcolor=white:borderw={max(1, size // 18)}"
                f":bordercolor=black:x={x}-text_w/2:y={y}-text_h/2"
                f":enable='between(t,{visible_from:.3f},{visible_to:.3f})'"
            )
            if font:
                draw += f":fontfile='{font.replace(chr(92), '/').replace(':', chr(92) + ':')}'"
            filters.append(f"{base}{draw}{label}")
            base = label
            inputs += 1
            continue

        path = clip.get("path")
        if not path or not Path(path).is_file():
            continue

        speed = clip.get("speed") or 1.0
        seek = clip.get("source_start", 0.0) + max(0.0, -clip_start) * speed
        take = (visible_to - visible_from) * speed

        if clip["kind"] in {"photo", "image"}:
            args += ["-loop", "1", "-t", f"{visible_to - visible_from:.3f}", "-i", path]
        else:
            args += ["-ss", f"{seek:.3f}", "-t", f"{take:.3f}", "-i", path]

        idx = inputs
        inputs += 1

        chain = f"[{idx}:v]"
        steps = [f"scale={width}:{height}:force_original_aspect_ratio=decrease"]
        if (crop := clip.get("crop")):
            steps.insert(0, f"crop=iw*{crop['width']}:ih*{crop['height']}:"
                           f"iw*{crop['x']}:ih*{crop['y']}")
        if speed != 1.0:
            steps.insert(0, f"setpts=PTS/{speed}")
        if (s := clip.get("scale")):
            steps.append(f"scale=iw*{s['x']}:ih*{s['y']}")
        if (alpha := clip.get("alpha", 1.0)) != 1.0:
            steps.append(f"format=rgba,colorchannelmixer=aa={alpha}")
        steps.append(f"setpts=PTS-STARTPTS+{visible_from:.3f}/TB")

        label = f"[v{idx}]"
        filters.append(f"{chain}{','.join(steps)}{label}")

        pos = clip.get("position", {"x": 0.0, "y": 0.0})
        ox = f"(W-w)/2+{pos.get('x', 0.0)}*W/2"
        oy = f"(H-h)/2-{pos.get('y', 0.0)}*H/2"
        out = f"[c{idx}]"
        filters.append(
            f"{base}{label}overlay=x={ox}:y={oy}"
            f":enable='between(t,{visible_from:.3f},{visible_to:.3f})'{out}"
        )
        base = out

        if with_audio and clip.get("volume", 1.0) > 0 and clip["kind"] == "video":
            alabel = f"[a{idx}]"
            delay = int(round(visible_from * 1000))
            filters.append(
                f"[{idx}:a?]atrim=0:{take:.3f},asetpts=PTS-STARTPTS,"
                f"volume={clip.get('volume', 1.0)},adelay={delay}|{delay}{alabel}"
            )
            audio_labels.append(alabel)

    if with_audio:
        for clip in _audio_clips(doc):
            if clip["kind"] != "audio":
                continue
            clip_start, clip_end = clip["start"] - start, clip["end"] - start
            if clip_end <= 0 or clip_start >= span:
                continue
            visible_from, visible_to = max(clip_start, 0.0), min(clip_end, span)
            seek = clip.get("source_start", 0.0) + max(0.0, -clip_start)

            args += ["-ss", f"{seek:.3f}", "-t", f"{visible_to - visible_from:.3f}", "-i", clip["path"]]
            idx = inputs
            inputs += 1
            alabel = f"[a{idx}]"
            delay = int(round(visible_from * 1000))
            filters.append(
                f"[{idx}:a?]asetpts=PTS-STARTPTS,volume={clip.get('volume', 1.0)},"
                f"adelay={delay}|{delay}{alabel}"
            )
            audio_labels.append(alabel)

    if audio_labels and with_audio:
        filters.append(
            f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:dropout_transition=0:"
            f"normalize=0[aout]"
        )

    args += ["-filter_complex", ";".join(filters) if filters else f"{base}null[vout]"]
    args += ["-map", base if filters else "[vout]"]
    if audio_labels and with_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]

    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", "-t", f"{span:.3f}", str(out_path)]
    return args


def render(doc: dict, out_path: str | Path, **kwargs) -> dict:
    """Render a proxy of the timeline and return where it landed."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(doc, out, **kwargs)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("Preview render timed out after 30 minutes.") from exc
    except OSError as exc:
        raise PreviewError(f"Could not launch ffmpeg: {exc}") from exc

    if result.returncode != 0 or not out.is_file():
        raise PreviewError(f"ffmpeg failed: {(result.stderr or '').strip()[:1500]}")

    return {"path": str(out), "bytes": out.stat().st_size,
            "seconds": kwargs.get("duration") or doc.get("duration")}


def frame(doc: dict, at: float, out_path: str | Path, *, height: int = 540,
          font: str | None = None) -> dict:
    """Composite a single instant of the timeline as a PNG.

    Rendering one frame only touches the clips live at that moment, so this is
    cheap enough to use as the agent's normal way of looking at its own work.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_command(doc, out.with_suffix(".mp4"), height=height, start=at,
                        duration=1.0 / (doc.get("fps") or 30), with_audio=False, font=font)
    # Swap the video encode for a single still.
    cmd = [a for a in cmd if a not in {"-c:v", "libx264", "-preset", "veryfast",
                                       "-crf", "26", "-pix_fmt", "yuv420p"}]
    cmd[-1] = str(out)
    cmd.insert(-1, "-frames:v")
    cmd.insert(-1, "1")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreviewError(f"Frame grab failed: {exc}") from exc

    if result.returncode != 0 or not out.is_file():
        raise PreviewError(f"ffmpeg failed: {(result.stderr or '').strip()[:1500]}")

    return {"path": str(out), "at": at, "bytes": out.stat().st_size}
