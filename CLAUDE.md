# CLAUDE.md

A Home Assistant custom integration exposing openHAB items as HA entities.
Installed via HACS from https://github.com/sjonez/hass-openhab-bridge.

## Read the design notes first

**[`docs/DESIGN.md`](docs/DESIGN.md) records why this integration works the way it
does.** Read it in full before changing anything non-trivial — not just the section
matching the task. Several decisions look like bugs in isolation and are not:

- The `autoupdate="false"` exemption (§6) deliberately contradicts the no-op
  suppression rule (§5). The exemption wins.
- Repair-flow guards test `if user_input and "platform" in user_input`, not mere
  truthiness, because HA opens a fix flow by calling the init step with a *populated*
  dict. Simplifying that guard produces an HTTP 500 in the real UI.
- The `changed` bus event fires on `ItemStateChangedEvent` only, because openHAB emits
  two subscribed events per real change. "Fixing" this double-triggers every automation.

## The core invariant

**openHAB always wins.** An entity only ever shows a value openHAB has reported; we
never optimistically write state after a command. Nearly everything else follows from
this. Do not add optimistic updates as a latency fix — §1 explains what breaks.

## Verify against real systems

Mocked tests have missed real bugs in **event handling, the write path, and the repair
flows** — every layer of testing here caught something the others could not (§10).
For changes in those three areas, mocked tests passing is not evidence the change works.

- Live tests: `tests/test_live.py` and `scripts/`, driven by a gitignored `.env`.
  They skip themselves without credentials.
- **Write operations may only ever target `OPENHAB_TEST_ITEM`** — a scratch item with
  nothing bound to it. Never write to any other item on the live instance; it is the
  user's real home.
- A throwaway HA for UI testing lives at `/opt/hass` in WSL. It must run in an attached
  session — a backgrounded `nohup` dies when WSL closes the last session.

## Conventions

- Run tests with `.venv\Scripts\pytest` (or `python -m pytest`); `pythonpath = ["."]`
  in `pyproject.toml` is what makes a bare `pytest` work.
- `ruff` ignores D403 deliberately — it would "correct" openHAB to OpenHAB.
- Keep the README in sync when behaviour changes; it is the integration's public
  documentation, and users reach it through HACS.
