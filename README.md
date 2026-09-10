# capcut-mcp

An MCP server that lets an LLM agent read and edit local **CapCut** desktop
projects directly — cutting, timing, transforms, keyframed motion, text,
transitions, filters, stickers, audio — with an ffmpeg-based preview so the
agent can see the result of an edit before committing it.

## Requirements

- **Windows only.** CapCut's desktop draft store, process detection and this
  tool's discovery logic (registry, `tasklist`) are Windows-specific. There is
  no macOS/Linux support.
- Python 3.11+
- [CapCut](https://www.capcut.com/) desktop, installed and used at least once
- [ffmpeg](https://ffmpeg.org/download.html) on `PATH` (`winget install ffmpeg`
  or download a build and add it to PATH) — needed for `capcut_frame` /
  `capcut_preview`

## Why this exists

CapCut has no public API, but it stores each project as `draft_content.json`
next to the media. On recent CapCut builds (tested on 9.3.x) that file is
plain UTF-8 JSON, so the timeline can be read and rewritten directly —
`capcut_probe` confirms this against a real file on your install rather than
assuming it from the version number, since this could change in a future
CapCut release.

Most CapCut automation projects are write-only draft builders: a handful of
`add_*` tools, no way to read the timeline back, and no way to look at the
result. That makes incremental editing impossible (you can only rebuild from
scratch) and leaves every edit unverified until a human opens the app. This
server adds the two missing halves:

- **Read.** `capcut_timeline` returns the whole project as a flat,
  seconds-based document. Clips carry short ids used to address them in every
  edit tool.
- **See.** `capcut_frame` and `capcut_preview` compile that same timeline to
  ffmpeg, so the agent can look at its own work before saving.

## Architecture

```
draft_content.json  <->  Timeline IR  ->  ffmpeg proxy (preview / frame)
   (CapCut's schema)     (this package's)
```

The IR is the stable contract. Agents never walk CapCut's material graph,
where a segment points at a `material_id` plus a list of
`extra_material_refs` for its speed, canvas, channel mapping and so on. If a
future CapCut update changes that schema, the compiler changes and the tool
surface does not — and the ffmpeg proxy keeps working regardless.

| Module | Role |
| --- | --- |
| `paths.py` | Find the CapCut install, the draft store and running processes |
| `draft.py` | Load/save `draft_content.json`, backups, write gating |
| `ir.py` | The seconds-based Timeline IR, plus keyframe interpolation |
| `catalog.py` | Name -> resource_id index mined from your own draft history |
| `edits.py` | Every mutation: clips, text, transitions, effects, keyframes... |
| `preview.py` | Compile the IR to an ffmpeg proxy |
| `server.py` | The 35 MCP tools |

## Session model

Open, edit, look, save — like an editor:

```
capcut_probe            check the setup
capcut_projects         find the project
capcut_timeline         read it
capcut_split_clip ...   edits accumulate in memory
capcut_frame            look at the result
capcut_save             write to disk, backing up first
```

Nothing touches disk until `capcut_save`, so a bad edit costs nothing.
`capcut_discard` drops in-memory edits; `capcut_revert` rolls back to a backup.

## Two safety rules that matter

**CapCut must be closed to save.** The app holds its own copy of the project
in memory and flushes it on close, which silently discards anything written
underneath it. `capcut_save` refuses while CapCut is running; `force=True`
overrides at your own risk.

**Every save backs up first.** Timestamped copies land in `.mcp_backups/`
inside the project folder, 40 kept. `capcut_revert` restores from one.

## Tool reference

**Project / timeline**
| Tool | Does |
| --- | --- |
| `capcut_probe` | Install, draft-store, ffmpeg and CapCut-running state in one call — call this first |
| `capcut_projects` | List CapCut projects, newest first |
| `capcut_timeline` | Read a project's tracks/clips/timings/transforms |
| `capcut_save` / `capcut_discard` / `capcut_revert` / `capcut_backups` | Commit, drop, roll back, or list backups |

**Clips**
| Tool | Does |
| --- | --- |
| `capcut_add_clip` | Place a video/image/audio file on the timeline |
| `capcut_add_text` | Add a text overlay |
| `capcut_update_clip` | Transform, opacity, volume, speed, visibility |
| `capcut_move_clip` / `capcut_trim_clip` / `capcut_split_clip` / `capcut_delete_clip` | Timeline surgery |
| `capcut_set_crop` | Rectangular crop — split-screen, PiP, rectangular masking |
| `capcut_set_background` | Blur/color fill behind footage that doesn't cover the frame |
| `capcut_speed_ramp` | Freeze-frames, speed ramps, slow-mo punches |

**Keyframes** (pure geometry, works on any project — see below)
| Tool | Does |
| --- | --- |
| `capcut_set_keyframes` / `capcut_clear_keyframes` | Animate position/scale/rotation/opacity over time |
| `capcut_add_fade` | Video/image opacity fade in/out |
| `capcut_add_audio_fade` | Audio volume fade in/out |

**Catalog-bound** (only reapplies what you've used before in CapCut — see below)
| Tool | Does |
| --- | --- |
| `capcut_catalog_search` / `capcut_catalog_refresh` | Find / re-scan what's reapplicable |
| `capcut_add_transition` | Transition between a clip and the next one |
| `capcut_add_effect` | Video effect overlay (its own effect track) |
| `capcut_add_filter` | Color filter/adjustment (its own adjust track) |
| `capcut_add_animation` | Entrance/exit/loop animation on a clip |
| `capcut_add_sticker` | Image/sticker overlay (its own sticker track) |
| `capcut_add_audio_effect` | Denoise/voice-character/EQ preset on an audio clip |

**Native AI tools**
| Tool | Does |
| --- | --- |
| `capcut_set_denoise` | Toggle CapCut's built-in real-time noise reduction |

**Tracks**
| Tool | Does |
| --- | --- |
| `capcut_set_track_muted` / `capcut_set_track_visible` | Mute/hide every clip on a track at once |
| `capcut_reorder_track` | Change stacking order (z-order) |

**Preview**
| Tool | Does |
| --- | --- |
| `capcut_frame` | Render one instant to a PNG (interpolates keyframes) |
| `capcut_preview` | Render a proxy video of the timeline or a span of it |

## Keyframes: fully general, no catalog needed

Keyframes are pure geometry CapCut's schema already describes in full (a
property name, timed points, a curve) — not tied to anything CapCut-cloud, so
`capcut_set_keyframes` works on **any** project regardless of history.
`KFTypePositionX`, `KFTypePositionY` and `KFTypeScaleX` are confirmed against
real CapCut drafts; `KFTypeScaleY`, `KFTypeRotation` and `KFTypeAlpha` (used by
`capcut_add_fade`) follow the same naming convention but were not individually
observed — verify with `capcut_frame`, and once in CapCut itself, before
depending on them in production. `capcut_frame` interpolates keyframes for the
requested instant, so a Ken Burns pan/zoom is visible in the proxy before you
ever save.

No volume-keyframe type was found anywhere, so `capcut_add_audio_fade`
approximates a fade with several tiny sub-clips of graduated flat `volume`
instead of guessing a shape that might not exist. Same story for
`capcut_speed_ramp`: no populated smooth speed-curve was found, so it splits
the clip into flat-speed pieces instead — no visible seam on screen, but it
does show up as separate clips in `capcut_timeline`.

## Catalog-bound tools: reapply, not invent

CapCut resolves every transition, effect, filter, animation, audio effect and
sticker against its own **cloud catalog** by an opaque numeric `resource_id`
— there is no offline API to browse that catalog or mint an id for something
never used before. What *is* available locally is every resource_id CapCut
has already cached on your machine, because you (or whoever uses this
install) applied it by hand at least once. `catalog.py` mines that out of
every local draft into a searchable name index, and the catalog-bound tools
deep-copy the exact material CapCut itself produced for that past use — cache
path, sub-parameters and all — substituting only a fresh id.

**Consequence: these tools can only reapply what has already been used —
never invent something new.** `capcut_catalog_search` shows what's actually
available on your machine; `capcut_catalog_refresh` re-scans after applying
something new by hand in CapCut. On a fresh CapCut install with an empty
history, the catalog starts empty — use CapCut's own UI to apply a handful of
transitions/effects/filters/stickers you like once, and they become
reapplicable by the agent from then on.

## Native AI tools: what does and doesn't work

CapCut's built-in AI tools split into two very different architectures, and
this was verified against real local drafts rather than assumed:

- **Noise reduction works** (`capcut_set_denoise`). It references a model file
  CapCut ships with itself — the same file for every clip, not a per-clip
  derived output — so there's nothing to precompute; flipping the flag is what
  CapCut's own "Reduce noise" toggle does. `is_denoise: true` is CapCut's own
  default on nearly every clip observed, so an explicit off-to-on toggle in a
  real project hasn't been directly observed — confirm audibly (or in CapCut)
  on anything noise-critical.
- **Vocal/music separation and background removal (matting) do not.** A real
  example of vocal separation was found in local drafts, and it requires
  CapCut to have already run its own AI pipeline and written a derived audio
  file into the project's own `Resources/` folder before the setting means
  anything — editing the JSON flag alone, without that file, leaves a broken
  reference. The same architecture was confirmed for background removal
  (`matting`, always an empty, unprocessed stub in every local example). This
  package does not fake either of these.
  - **Workaround:** run vocal separation or background removal with a
    dedicated tool outside CapCut (e.g. Demucs for stem separation, or any
    matting model), then bring the result in as a normal file via
    `capcut_add_clip` — often better quality and more controllable than
    driving CapCut's own black-box pipeline anyway.
- **AI image/video generation is not reachable at all.** CapCut's AI asset
  generation is a cloud service tied to your CapCut/Jianying account (real
  `aigc_config`/prompt/seed/model fields were found in the schema, confirming
  server-side generation, not a local model this package could drive). This
  is intentionally not implemented — it would mean reverse-engineering a
  private, authenticated, undocumented API of a consumer product. Generate
  assets with a tool you control instead, then import the result the same way
  as above.
- **Caption/text style templates and full downloadable project templates**
  use the same resource_id-catalog architecture as transitions/effects (the
  schema fields — `preset_id`, `text_preset_resource_id`, `template_id` — are
  there), but starts empty until you've applied at least one by hand in
  CapCut, same as any other catalog-bound feature.

## Preview fidelity

The ffmpeg proxy honours layout, trims, speed, opacity, volume, crop,
background fill, text position/z-order and keyframed motion. It does **not**
render CapCut's own transitions, filters, effects, stickers or audio effects
at all — a cached resource's file path was confirmed to sometimes be a
*directory* of resource-type-specific mixed content (JSON algorithm configs,
sub-hashed folders) rather than one directly renderable file, so this was not
something to guess at wiring up.

In practice: `capcut_frame`/`capcut_preview` are fully reliable for verifying
layout, crop, keyframes and background before saving, but currently blind to
whether a catalog-reapplied effect/filter/transition/sticker looks right —
that still needs opening the saved project in CapCut once.

## Known limits

- Reading drafts depends on them staying plaintext JSON on your CapCut
  version. `capcut_probe` checks this directly rather than trusting the
  version number.
- **Shaped masks** (circle, custom path, feathered edge) are not supported —
  no write schema was found; the closest boolean-looking field is just a
  generic toggle, not a real mask reference. `capcut_set_crop` gives
  rectangular crops only.
- **No dedicated LUT bucket exists** in the schema; color grading beyond a
  catalog `capcut_add_filter` (true LUT files, curves/wheels/HSL) is
  read-only via `capcut_timeline`.
- Export is not automated. Render the ffmpeg proxy here, or open CapCut and
  export normally.

## Install

```bash
uv venv --python 3.13
uv pip install -e .
```

Register with Claude Code:

```bash
claude mcp add capcut -s user -- "<path-to-repo>/.venv/Scripts/capcut-mcp.exe"
```

(On Windows/PowerShell, run this from a shell where `--` isn't swallowed —
Git Bash works; if using PowerShell directly, use `claude --%` escaping or
call the venv's `capcut-mcp.exe` path directly as shown.)

If the draft store isn't found automatically (`capcut_probe` reports it
missing), set `CAPCUT_MCP_DRAFT_ROOT` to the folder containing
`root_meta_info.json` (normally under
`%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`, but this can
be on a different drive if CapCut was reinstalled after a drive change).

## Verify your install

```bash
.venv/Scripts/python.exe scripts/verify_setup.py    # install/paths/ffmpeg — safe on an empty install
.venv/Scripts/python.exe scripts/verify_tools.py    # prints all 35 tools — safe on an empty install
.venv/Scripts/python.exe scripts/verify_render.py   # needs at least one real CapCut project with a clip
```

`verify_render.py` works on a scratch copy of your first project; it never
modifies your real projects.

## Contributing

Issues and PRs welcome. If you're adding a tool that touches a new part of
CapCut's schema, please base it on a real example found in an actual local
draft (as every tool in this repo was) rather than a guessed shape — CapCut's
draft format is undocumented, and a plausible-looking but wrong field can
silently corrupt a project.

## License

MIT — see [LICENSE](LICENSE).
