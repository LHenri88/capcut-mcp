"""Verify the ffmpeg proxy renderer actually honours alpha and scale.

Needs at least one existing CapCut project with a clip in it -- works on a
scratch copy, never touches your real project.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from capcut_mcp import draft as draft_mod
from capcut_mcp import edits, ir, paths, preview

SCRATCH = Path(tempfile.gettempdir()) / "capcut-mcp-verify-render"


def brightness(png: Path) -> float:
    """Mean luma, read by downscaling the frame to a single grey pixel.

    Averaging a 16x16 reduction in Python is more trustworthy than asking the
    scaler for a single pixel, which samples rather than averages.
    """
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(png), "-vf",
         "format=gray,scale=16:16:flags=area", "-f", "rawvideo", "-"],
        capture_output=True, timeout=120,
    )
    if out.returncode != 0 or len(out.stdout) < 256:
        raise RuntimeError(f"could not measure {png}: {out.stderr.decode()[:300]}")
    pixels = out.stdout[:256]
    return sum(pixels) / len(pixels)


def main() -> int:
    projects = [p for p in paths.list_projects() if p["available"]]
    if not projects:
        print("No CapCut projects found -- create at least one project with a clip "
              "in CapCut first, then re-run this.")
        return 1

    source = projects[0]
    work = SCRATCH / "tf"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy2(Path(source["path"]) / "draft_content.json", work / "draft_content.json")

    d = draft_mod.Draft.open(str(work))
    doc = ir.build(d.data)
    track_with_clips = next((t for t in doc["tracks"] if t["clips"]), None)
    if track_with_clips is None:
        print(f"Project '{source['name']}' has no clips on any track -- pick a "
              "project with at least one clip.")
        return 1

    clip = track_with_clips["clips"][0]
    at = clip["start"] + min(1.0, clip["duration"] / 2)

    # A real project can stack several video tracks; an opaque clip on another
    # track can fully cover this one at the same instant, which would make an
    # alpha change on THIS clip invisible in the composite for reasons that
    # have nothing to do with whether alpha itself is honoured. Hide every
    # other track so the test isolates the one clip it's actually checking.
    for t in doc["tracks"]:
        if t["index"] != track_with_clips["index"]:
            edits.set_track_visible(d.data, t["index"], False)

    base = preview.frame(ir.build(d.data), at, work / "a_full.png", height=240)
    full = brightness(Path(base["path"]))

    edits.update_clip(d.data, clip["id"], alpha=0.35)
    dim = brightness(Path(preview.frame(ir.build(d.data), at, work / "b_dim.png", height=240)["path"]))

    edits.update_clip(d.data, clip["id"], alpha=1.0, scale=0.5)
    half = Path(preview.frame(ir.build(d.data), at, work / "c_half.png", height=240)["path"])

    print(f"  project: {source['name']}")
    print(f"  alpha 1.00 -> mean luma {full:.2f}")
    print(f"  alpha 0.35 -> mean luma {dim:.2f}")
    alpha_ok = dim < full * 0.6
    print(f"  alpha honoured: {alpha_ok}")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(half)],
        capture_output=True, text=True, timeout=60).stdout.strip()
    print(f"  scale 0.5 frame: {probe} (canvas stays fixed; clip is inset)")
    print(f"  frames written to {work}")

    return 0 if alpha_ok else 1


if __name__ == "__main__":
    sys.exit(main())
