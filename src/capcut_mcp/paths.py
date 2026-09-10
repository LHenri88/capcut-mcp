"""Locate the CapCut installation, its draft store, and report runtime state.

On Windows CapCut keeps ``User Data`` under %LOCALAPPDATA%\\CapCut as an NTFS
junction. That junction can go stale -- if the target volume is re-created the
recorded volume GUID stops resolving and every stat() against the path raises
OSError rather than simply returning False. Every probe here is therefore
defensive, and discovery falls back to scanning drive roots for a real store.
"""

from __future__ import annotations

import json
import os
import string
import subprocess
import winreg
from pathlib import Path

DRAFT_DIR_NAME = "com.lveditor.draft"
ENV_DRAFT_ROOT = "CAPCUT_MCP_DRAFT_ROOT"
META_FILE = "root_meta_info.json"

_REL = Path("User Data") / "Projects" / DRAFT_DIR_NAME
_APP_NAMES = ("CapCut", "Capcut", "JianyingPro")


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _candidate_roots():
    """Yield plausible draft-store locations, most authoritative first."""
    env = os.environ.get(ENV_DRAFT_ROOT)
    if env:
        yield Path(env)

    local = os.environ.get("LOCALAPPDATA")
    if local:
        for name in _APP_NAMES:
            yield Path(local) / name / _REL

    # The junction above is the documented location but breaks when its target
    # volume is replaced; real installs then live directly on a data drive.
    for letter in string.ascii_uppercase:
        for name in _APP_NAMES:
            yield Path(f"{letter}:/") / name / _REL


def find_draft_root(explicit: str | None = None) -> Path:
    """Return the folder holding root_meta_info.json, or raise."""
    if explicit:
        p = Path(explicit)
        if _is_file(p / META_FILE):
            return p
        raise FileNotFoundError(f"No {META_FILE} under {p}")

    seen: set[str] = set()
    for cand in _candidate_roots():
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if _is_file(cand / META_FILE):
            return cand

    raise FileNotFoundError(
        "Could not locate a CapCut draft store. Set "
        f"{ENV_DRAFT_ROOT} to the folder containing {META_FILE}."
    )


def installed_version() -> str | None:
    """Read the installed CapCut version from the uninstall registry keys."""
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                            name, _ = winreg.QueryValueEx(entry, "DisplayName")
                            if "capcut" in str(name).lower():
                                return winreg.QueryValueEx(entry, "DisplayVersion")[0]
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def running_pids() -> list[int]:
    """PIDs of live CapCut processes.

    A draft written while CapCut holds the project open is discarded when the
    app flushes its own in-memory state, so writes must be gated on this.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    pids: list[int] = []
    for line in out.splitlines():
        parts = [c.strip('" ') for c in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("capcut"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def list_projects(draft_root: Path | None = None) -> list[dict]:
    """Every project registered in the draft index, newest first."""
    root = draft_root or find_draft_root()
    meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))

    projects = []
    for entry in meta.get("all_draft_store", []):
        if entry.get("tm_draft_removed"):
            continue
        folder = (entry.get("draft_fold_path") or "").replace("\\", "/")
        projects.append(
            {
                "name": entry.get("draft_name") or Path(folder).name,
                "path": folder,
                "draft_id": entry.get("draft_id"),
                # CapCut stores these as microseconds since the epoch.
                "modified_us": entry.get("tm_draft_modified") or 0,
                "duration_s": round((entry.get("tm_duration") or 0) / 1_000_000, 3),
                "available": _is_file(Path(folder) / "draft_content.json"),
            }
        )

    projects.sort(key=lambda p: p["modified_us"], reverse=True)
    return projects


def resolve_project(name_or_path: str, draft_root: Path | None = None) -> Path:
    """Resolve a project name, or a direct folder path, to its draft folder."""
    direct = Path(name_or_path)
    if _is_file(direct / "draft_content.json"):
        return direct

    projects = list_projects(draft_root)
    wanted = name_or_path.strip().lower()

    exact = [p for p in projects if p["name"].lower() == wanted]
    if len(exact) == 1:
        return Path(exact[0]["path"])
    if len(exact) > 1:
        newest = exact[0]
        return Path(newest["path"])

    partial = [p for p in projects if wanted in p["name"].lower()]
    if len(partial) == 1:
        return Path(partial[0]["path"])
    if len(partial) > 1:
        names = ", ".join(p["name"] for p in partial[:8])
        raise ValueError(f"'{name_or_path}' matches several projects: {names}")

    raise FileNotFoundError(f"No CapCut project named '{name_or_path}'")


def find_denoise_model() -> str | None:
    """Locate the bundled real-time denoise model CapCut ships with itself.

    Every clip's `realtime_denoise` companion material points at this exact
    file (shared across every clip -- not a per-clip derived asset, unlike
    vocal separation), so a new clip we create needs the same reference to
    match what CapCut itself would have written.
    """
    import string as _string

    candidates: list[Path] = []
    env = os.environ.get(ENV_DRAFT_ROOT)
    roots = [Path(env).parent.parent.parent] if env else []
    for letter in _string.ascii_uppercase:
        for name in _APP_NAMES:
            roots.append(Path(f"{letter}:/") / name / "Apps")

    for root in roots:
        if not _is_dir(root):
            continue
        try:
            versions = sorted((p for p in root.iterdir() if _is_dir(p)), reverse=True)
        except OSError:
            continue
        for version_dir in versions:
            hits = list(version_dir.glob("Resources/audiosami/unet_denoise_*.model"))
            if hits:
                candidates.append(hits[0])
        if candidates:
            break

    return str(candidates[0]) if candidates else None


def probe(explicit_root: str | None = None) -> dict:
    """Full capability report -- run this before trusting any other tool."""
    report: dict = {
        "capcut_version": installed_version(),
        "running_pids": running_pids(),
        "draft_root": None,
        "projects": 0,
        "plaintext_drafts": None,
        "draft_schema_version": None,
        "ffmpeg": _ffmpeg_version(),
        "denoise_model": find_denoise_model(),
        "warnings": [],
    }

    try:
        root = find_draft_root(explicit_root)
    except FileNotFoundError as exc:
        report["warnings"].append(str(exc))
        return report

    report["draft_root"] = str(root)
    projects = list_projects(root)
    report["projects"] = len(projects)

    # Confirm drafts are readable JSON on this CapCut build. Versions from 6
    # onward were reported to encrypt draft_content.json, so this is checked
    # against a real file rather than assumed from the version number.
    for proj in projects:
        if not proj["available"]:
            continue
        try:
            data = json.loads(
                (Path(proj["path"]) / "draft_content.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report["plaintext_drafts"] = False
            report["warnings"].append(
                f"draft_content.json in '{proj['name']}' is not readable JSON: {exc}"
            )
            break
        report["plaintext_drafts"] = True
        report["draft_schema_version"] = data.get("version")
        report["app_version_in_draft"] = data.get("new_version")
        break

    if report["running_pids"]:
        report["warnings"].append(
            f"CapCut is running ({len(report['running_pids'])} processes). "
            "Close it before writing, or edits will be overwritten."
        )
    if not report["ffmpeg"]:
        report["warnings"].append("ffmpeg not found on PATH; preview rendering is unavailable.")

    return report


def _ffmpeg_version() -> str | None:
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None
