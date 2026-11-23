# AGENTS.md — WoT 0.8.2 Offline Battles 2.x

Client-side mod for the **World of Tanks 0.8.2** client (BigWorld engine, embedded
**Python 2.6**, 32-bit Windows). It fakes the server entirely: offline login and
garage with progression, plus 15v15 battles against AI bots on all 33 maps. There
is no server process; everything runs inside `WorldOfTanks.exe`. GPLv3, based on
`mod_offhangar_legacy`.

`README.md` is the user-facing description (EN/RU). This file is the working guide.
Sources are synced to release **2.1 beta 1** (version strings in code still say "2.0").

## Repo layout

```
src/                      everything that ships (assembled into res_mods/0.8.2 by scripts/build.py)
  bootstrap/              -> scripts/client/            CameraNode.py (mod loader), OfflineEntity.py, mods/__init__.py
  mods/                   -> scripts/client/gui/mods/   mod_offhangar.py (ENTRY POINT), mod_replserver.py, __init__.py
  offhangar/              -> scripts/client/gui/mods/offhangar/   THE PACKAGE (code + data + .pyd)
  gui/                    -> gui/                       flash/*.swf (header, FinalStatistic, OffhangarMapPicker, common), maps/icons
  native/                 C sources of the native binaries; only offh_math.dll ships (-> offhangar/native/)
tools/                    host-side dev tools, Python 3, never shipped
  repo_env.py             shared: repo paths, .env loader, game_dir(), install_mod_imports()
  packed_xml.py           the one BigWorld packed-XML read/write codec
  navedit/                NavEdit route & OBB editor (http://127.0.0.1:8765/)
  tactical_map/           navgraph -> PNG renderer (build/tactical_maps/)
  worker/                 offline_worker_starter.py — PROTOTYPE launcher for the headless authority worker
scripts/
  build.py                stage build/res_mods/0.8.2, pack the zip, optional --deploy
  build_offh_math.sh      rebuild offh_math.dll (+ host .so for selftest) with MinGW, run from WSL
.env.example              copy to .env (gitignored): WOT_082_DIR=<WoT 0.8.2 client folder>, optional PY26_EXE
requirements.txt          host deps for tools (numpy, matplotlib); build.py and NavEdit need only stdlib
```

**Setup:** `py -3.12 -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt`.
Portable **Python 2.6.6** (same branch the client embeds) lives in
`.tools/python26/` (gitignored): download `python-2.6.6.msi` from python.org
(md5 `80b1ef074a3b86f34a2e6b454a05c8eb`) and extract without installing:
`msiexec /a python-2.6.6.msi /qn TARGETDIR=<repo>\.tools\python26`.

**Build / deploy:** `.venv\Scripts\python scripts/build.py` →
`build/res_mods/0.8.2/` (exact client layout) + `build/Offline_Battles_<VERSION>_res_mods.zip`.
With Python 2.6 present it first **compiles every `src/**/*.py` under 2.6**
(fails the build on 2.7+ syntax) and **rebuilds `src/bootstrap/*.pyc`**; without
it, the committed `.pyc` are shipped as-is. `--deploy` copies the staged tree
into `WOT_082_DIR` (never deletes anything). Bump `VERSION` in `build.py` per
release. To add a new shipped top-level thing, add a line to `LAYOUT`.

**The engine imports `CameraNode` and `OfflineEntity` only as `.pyc`** — `.py`
next to them is ignored. Without `CameraNode.pyc` no mod loads at all (stock
login with e-mail field). So `src/bootstrap/*.pyc` are committed; after editing
`CameraNode.py`/`OfflineEntity.py`, rebuild (needs Python 2.6) and commit the
`.pyc`. Everything else is loaded from `.py` via `execfile`.

**Importing an author release zip:** extract to a temp dir, take only live paths
(`scripts/client/{CameraNode,OfflineEntity}.py`, `scripts/client/mods/`,
`scripts/client/gui/mods/{*.py,offhangar/}`, `gui/flash`, `gui/maps`), map them
back into `src/` per the table above, then verify with
`diff -rq --strip-trailing-cr build/res_mods/0.8.2 <release>/res_mods/0.8.2`.
Author zips contain junk that is deliberately **not** kept: `scripts/client/mod_offhangar.py`
+ `scripts/client/offhangar/` (stale never-loaded copies), `gui/mods/offhangar/mod_offhangar.py`,
`gui/scaleform/` (0.8.2 loads swf only from `gui/flash`), `offhangar/offhangar_user/`,
`offhangar/config.json`, `offhangar/tools/`, `scripts/offhangar_tools/`, `tools/`,
root-level `offh_mt.pyd`/`offhangar_native_seed.pyd` duplicates, `engine_config.xml`,
`version.xml`, `0.8.2.txt`, `.pyc`.

At runtime: user progress lives in `<WoT>/offhangar_user/` (`offhangar_state.dat`,
profiles, `replays/`, navcache) — paths are relative to the game CWD, see
`core/paths.py`. `engine_config.xml` is written by `mod_offhangar.py` itself
(`_xml_out` template). Logs: client `python.log`, mod lines tagged
`[OFFHANGAR] [C]` (visible client) or `[W]` (headless worker).

## Hard constraints (mod code in src/)

- **Python 2.6 syntax only.** No dict/set comprehensions, no `{1, 2}` set
  literals, no `'{}'.format()` auto-numbering (use `'{0}'` or `%`), no
  `with a, b:`, no f-strings, no `print()` with kwargs. `unicode`/`long` exist.
  (`tools/` and `scripts/` are Python 3 — the rule doesn't apply there.)
- **No third-party packages.** Stdlib + the game's own modules (`BigWorld`,
  `ResMgr`, `Math`, `Account`, `gui.*`, `debug_utils`, ...).
- **Everything is fail-safe.** Code is wrapped in `try/except Exception`
  almost everywhere: a raised exception in a hook or `BigWorld.callback` can
  break the client for the rest of the session. Keep that style for new hooks.
- **Native code is version-locked** to one `WorldOfTanks.exe`
  (TimeDateStamp `0x50B8ECCF`) with hard-coded RVAs/struct offsets. Never
  "generalise" those addresses.
- Match the indentation of the file you edit: tabs in `mod_offhangar.py`,
  `offline_battle.py` and most of `offhangar/`; 4 spaces in some files
  (`OfflineEntity.py`, `core/logging.py`, `ai/bot_subengine.py`...).
- `.gitattributes` keeps LF in the working tree (releases ship LF).

## Stock game scripts (reference)

Decompiled 0.8.2 client scripts (~1180 `.py`, read-only, outside the repo):
**`D:\Work\projects\WoT_0.8.2_decompiled`**

- `res/scripts/client/` — `avatar.py`, `vehicle.py`, `account.py`, `game.py`,
  `connectionmanager.py`, `battlereplay.py`, `avatarinputhandler/`,
  `gui/scaleform/` (hangar, battle, battleresults...), `account_helpers/`, `messenger/`
- `res/scripts/common/` — `constants.py`, `accountcommands.py`, `arenatype.py`,
  `items/` (vehicles, shells, crew), `physics_shared.py`, `chat_shared.py`
- `res_bw/scripts/` — BigWorld engine-side Python (`keys.py`, `fx/`, `scaleform/`)

File names are lowercased by the decompiler; real module names are CamelCase
(`import Avatar`, `Vehicle`, `gui.Scaleform`). Before hooking or monkey-patching
anything, read the stock implementation — signatures, name-mangled
`_Class__attr` attributes and call order matter. Mod comments cite it as
`dev_ref/stock_082/...`.

## Boot sequence

1. Engine imports `CameraNode.py`, whose `load_mods()` globs
   `scripts/client/gui/mods/mod_*` and `execfile`s each `.py`.
2. `mod_offhangar.py` cannot use normal imports (`gui.mods` isn't a real
   package in the embedded client). It builds `gui.mods` / `gui.mods.offhangar`
   modules by hand, then `_safe_import_offhangar()` **`execfile`s every module
   in the `submodules` list** and registers it under a **flat name**
   `gui.mods.offhangar.<name>` (plus subpackage aliases from `_OFFH_ALIASES`,
   e.g. `bot_ai_v2` -> `gui.mods.offhangar.ai.director_v2`). It also deletes
   stale `.pyc` files.
3. It then monkey-patches Account/ConnectionManager/Shop/hosts etc. so login
   and the garage work offline (`FakeServer` in `ui/server.py` answers
   `doCmd*` calls through `ui/command_router.py` + `ui/command_handlers.py`).
4. Pressing Battle! -> `offline_battle.begin_offline_battle_queue` ->
   `_try_spawn_battle_avatar_stub` builds the arena, bots and the whole battle.

**Adding a module:** put it in the right `src/offhangar/` subfolder, import
siblings as `from gui.mods.offhangar.<flat_name> import X` (never
`...offhangar.ai.x` unless it's an alias), and **add its name to `submodules`
in `mod_offhangar.py`** — otherwise it is never loaded. Order matters loosely
(the loader retries failed modules until no progress).

## Package map (`src/offhangar/`)

| Folder | What lives there |
|---|---|
| `core/` | `paths.py` (user dir, config load), `_constants.py` (`CONFIG_OPTIONS`), `state.py` (garage save/load), `flags.py` (per-battle config reader), `logging.py`, `session_guards.py`, `offline_battle_stack.py`, `cbt_profiles_and_locale.py` (login profiles, nicknames, RU/EN) |
| `ai/` | `bot_ai.py` (v1 director, huge), `bot_ai_v2.py` (active director), `bot_ai_driver.py`, `bot_ai_navigation.py`, `prebaked_navigation.py`, `bot_ai_maps*.py` (per-map tactics), `bot_blackboard.py`, `bot_capabilities.py`, `bot_subengine.py` (OBB vision + live radar on `http://127.0.0.1:8768/`; NavEdit reuses it) |
| `combat/` | `projectile_runtime.py` (ballistics), `device_damage.py`, `internal_*` (armour/module layouts), `spotting.py`, `foliage.py`, `capture_rules.py`, `bot_loadout.py` |
| `physics/` | `physics_core.py`, `vehicle_collision.py`, `vehicle_pose.py`, `destructibles_authority.py`, `spawn_streaming_bootstrap.py` |
| `native/` | bridges + all binaries: `offh_multithread.py` (`offh_mt.pyd`), `native_filter_bridge.py` (`offhangar_native_seed.pyd`), `offh_native_math.py` (`offh_math.dll`, optional, `native_math` off by default), probes |
| `ui/` | `server.py` (FakeServer), `command_router.py`/`command_handlers.py` (garage commands), `data.py` (hangar data), `battle_feedback.py`, `ingame_menu.py`, `spectator_minimap.py` |
| `network/` | `network_battle.py` (LAN mode, off by default: `network_mode`), `lan_*`, `replay_machinima.py` (`.wotreplay` recording) |
| data | `navgraphs/*.json` + `manifest.json`, `navmesh/*.grid` (OFFHNAV1 relief grids), `foliage/*.json` + `manifest.json`, `maps_obb.json`, `config_defaults.json` |

### `offline_battle.py` — the monolith

~45.5k lines. `_try_spawn_battle_avatar_stub` alone is ~24.8k lines (L19832–L44647)
of nested closures that hold the battle loop. Shared battle state is kept in
module `globals()` (`g_offh_battle_gen`, `g_offline_enemies`, ...; hundreds of
`globals()` reads/writes). Consequences:

- **Don't move a function out of this file** unless it touches no `globals()`:
  in another module `globals()` silently resolves to a different dict
  (see the docstring in `core/flags.py`, which is the extraction pattern).
- Every async callback must check the battle generation
  (`g_offh_battle_gen`) so a stale callback can't act after the battle ends.
- Use `grep -n` to navigate; don't read it whole.

## Configuration

- Shipped defaults: `offhangar/config_defaults.json`, loaded into
  `_constants.CONFIG_OPTIONS` by `core/paths.load_config()`.
- **`load_config()` hard-overrides ~40 keys** (wreck_*, los_*, bot_ai_v2,
  hangar_push_*, physics_tuning multipliers, ...). Changing those in JSON does
  nothing — change them in `paths.py`.
- `<WoT>/offhangar_user/config.json` is deliberately ignored/purged; users
  can't override engine/AI settings.
- Per-battle flag reads: use `flags.battle_value(name, default, gen)` instead
  of writing another ad-hoc reader.
- Native movement is forced on (`_offh_native_mode_enabled()` always True);
  there is intentionally no Python-physics fallback for bots.

## Prebaked data

`navgraphs/*.json`, `foliage/*.json` each have a `manifest.json` listing all
33 maps with `sha256`. The loaders (`ai/prebaked_navigation.py`,
`combat/prebaked_foliage.py`) require every map to be listed and its file to
exist, but **don't actually verify the hash** (`_sha256()` is never called).
Still update `sha256`/`bytes` when you edit a map file (NavEdit does it).
Known stale hash: `foliage/04_himmelsdorf.json`. Map keys are `NN_name`.

## Tools

- `python tools/navedit/navedit_editor.py` — opens `http://127.0.0.1:8765/`.
  Imports the mod's `bot_subengine` from `src/` via `repo_env.install_mod_imports()`
  and saves edits into `src/offhangar/` (`maps_obb.json`, `navgraphs/`, manifest).
  Map textures come from `<WOT_082_DIR>/res/packages` (set `.env`) or
  `tools/navedit/maps3d/<map>/color.jpg`.
- `python tools/tactical_map/render_tactical_map.py [map ...]` — needs numpy +
  matplotlib; writes `build/tactical_maps/<map>_tactical_v12.png`.
- New tools: put them under `tools/<name>/`, get paths and the game folder from
  `repo_env` (never hard-code `res_mods/...` or machine paths).

## Native code

- `offh_mt.pyd` — multithreaded physics sub-engine + in-memory crash guards and
  direct calls into `WorldOfTanks.exe`. Freestanding C (own typedefs, GCC
  `__attribute__` calling conventions, `offh_mt.def` exports `initoffh_mt`),
  32-bit MinGW. Source: `src/native/offh_mt.c` — this is the newest copy (it
  carried 2.1's ground-probe fixes); the 2.0 binary's source is only in git
  history (`src_c/offh_mt.c`). There's no build script for the `.pyd` files.
- `offhangar_native_seed.pyd` — WGVehicleFilter2 pose bridge
  (`src/native/offhangar_native_seed.c`); validates exe, vtables and pointers
  before touching memory.
- `offh_math.dll` — optional math helper, `scripts/build_offh_math.sh` (WSL,
  `i686-w64-mingw32-gcc`) rebuilds it + runs `src/native/selftest.py`.
- Rebuilding is out of scope unless asked; treat shipped binaries as given.

## Gotchas

- The entry point still shows "CBT"/"ЗБТ" version and host strings — that is
  what releases 2.0/2.1b1 actually run.
- `offhangar/physics/__init__.py` == `offhangar/physics/physics.py` — both
  ship; the flat loader replaces the `physics` subpackage with the `physics`
  module in `sys.modules`. Keep them in sync, don't delete one.
- `mod_replserver.py` ships and opens a debug REPL on 127.0.0.1:2222 in every
  client.
- `Installation-Linux` refers to the old 0.4 zip; the Proton steps still apply.

## Verifying changes

No tests, no CI, the game can't run here. Minimum checks:

1. `.venv\Scripts\python scripts/build.py` succeeds — with `.tools/python26`
   present that includes the Python 2.6 syntax check of all `src/`. The plain
   `python` here is Python 3 and rejects valid Py2 code — don't "fix" Py2
   syntax to satisfy it.
2. JSON stays valid; manifests updated.
3. Real test = `python scripts/build.py --deploy`, run the client, check
   `python.log` for `[OFFHANGAR]` errors (`Failed to import submodule` means
   the loader broke). Say clearly when a change is untested in-game.

## Conventions

- Prefix new private helpers in `offline_battle.py` with `_offh_`.
- Log with `LOG_NOTE` / `LOG_ERROR` / `LOG_DEBUG` from
  `gui.mods.offhangar.logging`; keep hot-path logging off (synchronous file
  I/O into `python.log`).
- Comments explain *why* (many reference past log evidence); keep that style.
- RU/EN UI strings: locale via `_offh_is_ru_locale()` / `cbt_profiles_and_locale.py`.
- Don't keep dead copies, backups or sample data in the repo — git history has them.
