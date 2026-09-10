"""Run this first after installing. Safe on a brand-new install with zero
CapCut projects -- it only checks that the pieces are found, never asserts on
your own project content.
"""

import sys

from capcut_mcp import paths


def main() -> int:
    report = paths.probe()
    ok = True

    print("CapCut version:      ", report.get("capcut_version") or "NOT FOUND")
    print("Running processes:   ", report.get("running_pids") or "none")
    print("Draft store:         ", report.get("draft_root") or "NOT FOUND")
    print("Projects found:      ", report.get("projects"))
    print("Drafts are plaintext:", report.get("plaintext_drafts"))
    print("ffmpeg:              ", report.get("ffmpeg") or "NOT FOUND")
    print("Denoise model:       ", report.get("denoise_model") or "not found (denoise toggle will still self-heal a path)")

    if not report.get("draft_root"):
        ok = False
        print("\n  -> Draft store not found. If CapCut is installed somewhere unusual,")
        print("     set CAPCUT_MCP_DRAFT_ROOT to the folder containing root_meta_info.json.")
    if report.get("plaintext_drafts") is False:
        ok = False
        print("\n  -> Your CapCut build's drafts are not plain JSON (encrypted or a newer")
        print("     schema). This tool cannot read/write them. See README's Known Limits.")
    if not report.get("ffmpeg"):
        ok = False
        print("\n  -> ffmpeg not found on PATH. Install it (e.g. `winget install ffmpeg` or")
        print("     https://ffmpeg.org/download.html) -- capcut_frame/capcut_preview need it.")

    for w in report.get("warnings", []):
        print(f"\nWarning: {w}")

    print("\n" + ("Setup looks good." if ok else "Setup has issues -- see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
