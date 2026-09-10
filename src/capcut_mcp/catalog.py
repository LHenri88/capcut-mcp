"""A name -> resource_id catalog for transitions, effects and animations.

CapCut's effects, transitions and animations are not local assets: every one
of them is identified by an opaque numeric ``resource_id`` that CapCut
resolves against its own cloud catalog, backed by a local render cache at
``.../CapCut/User Data/Cache/effect/<resource_id>/...``. There is no offline
API to browse that catalog or mint a new resource_id for something never
downloaded.

What *is* available locally is every resource_id CapCut has already cached --
in practice, everything the human editor has ever applied by hand, across
every draft. This module mines that history into a name-searchable index, so
an agent can reapply anything already in the library. It cannot invent an
effect that was never used before; it can only be as good as the local
history it is built from.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths

CACHE_FILE = "effect_catalog.json"
SOURCE_CATEGORIES = {
    "transitions": "transition",
    "effects": "filter",
    "video_effects": "effect",
    "material_animations": "animation",
    "audio_effects": "audio_effect",
    "stickers": "sticker",
}


def _scan(draft_root: Path | None = None) -> dict:
    projects = paths.list_projects(draft_root)
    entries: dict[str, dict] = {}

    for proj in projects:
        if not proj["available"]:
            continue
        f = Path(proj["path"]) / "draft_content.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        materials = data.get("materials") or {}
        for bucket, kind in SOURCE_CATEGORIES.items():
            for m in materials.get(bucket) or []:
                if bucket == "material_animations":
                    for a in m.get("animations") or []:
                        _record(entries, a.get("resource_id"), a.get("name"), kind,
                                a.get("category_name"), proj["name"], template=a)
                else:
                    rid = m.get("resource_id") or m.get("effect_id")
                    name = m.get("name") or m.get("report_name")
                    _record(entries, rid, name, kind, m.get("category_name"),
                            proj["name"], template=m)

    return {"built_at": time.time(), "entries": entries}


def _record(entries: dict, resource_id, name, kind, category, project, template: dict) -> None:
    """Keep one instance's full material as a reusable template.

    Storing the exact object CapCut produced -- cache path, sub-parameters,
    category ids and all -- is far more reliable than trying to reconstruct a
    minimal version of it by hand.
    """
    if not resource_id or not name:
        return
    key = str(resource_id)
    if key not in entries:
        clean = {k: v for k, v in template.items() if k != "id"}
        entries[key] = {"resource_id": key, "name": name, "kind": kind,
                        "category": category or "", "seen_in": [], "template": clean}
    if project not in entries[key]["seen_in"]:
        entries[key]["seen_in"].append(project)


def _cache_path() -> Path:
    return Path(__import__("tempfile").gettempdir()) / "capcut-mcp" / CACHE_FILE


def build(force: bool = False, draft_root: Path | None = None) -> dict:
    """Load the cached catalog, or rebuild it by scanning every local draft."""
    cache = _cache_path()
    if not force and cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    catalog = _scan(draft_root)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(catalog), encoding="utf-8")
    return catalog


def _brief(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "template"}


def search(query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
    """Case-insensitive substring search over cached effect/transition names."""
    catalog = build()
    needle = query.strip().lower()
    hits = []
    for entry in catalog["entries"].values():
        if kind and entry["kind"] != kind:
            continue
        if needle and needle not in entry["name"].lower():
            continue
        hits.append(entry)
    hits.sort(key=lambda e: len(e["seen_in"]), reverse=True)
    return [_brief(h) for h in hits[:limit]]


def resolve(name_or_id: str, kind: str | None = None, anim_kind: str | None = None) -> dict:
    """Resolve a name or a raw resource_id to one catalog entry (with its
    full template), or raise.

    Args:
        anim_kind: For kind="animation" only -- restrict to entries whose
            recorded animation type ("in"/"out"/"loop") matches, since the
            same visual effect is usually a different resource_id per kind.
    """
    catalog = build()

    def _matches(entry: dict) -> bool:
        if kind and entry["kind"] != kind:
            return False
        if anim_kind and (entry.get("template") or {}).get("type") != anim_kind:
            return False
        return True

    if name_or_id in catalog["entries"]:
        entry = catalog["entries"][name_or_id]
        if not _matches(entry):
            raise ValueError(
                f"resource_id {name_or_id} does not match kind={kind!r} anim_kind={anim_kind!r}"
            )
        return entry

    needle = name_or_id.strip().lower()
    hits = [e for e in catalog["entries"].values()
            if _matches(e) and needle in e["name"].lower()]
    hits.sort(key=lambda e: len(e["seen_in"]), reverse=True)

    exact = [h for h in hits if h["name"].lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise KeyError(
            f"No cached {kind or 'effect'} named '{name_or_id}'"
            f"{f' ({anim_kind})' if anim_kind else ''}. It must be applied once by hand in "
            "CapCut before an agent can reapply it -- call capcut_catalog_search to see what "
            "is actually available."
        )
    names = ", ".join(f"{h['name']} ({h['resource_id']})" for h in hits[:5])
    raise KeyError(f"'{name_or_id}' is ambiguous, matches: {names}")
