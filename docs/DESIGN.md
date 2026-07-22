# Design notes

Why this integration is built the way it is. The README covers *how to use it*; this
file covers *why it works this way*, so that the reasoning behind the non-obvious
decisions survives independently of whoever wrote them.

Anything recorded here as "verified" was checked against a real openHAB instance
(~2,900 items) and a real Home Assistant, not inferred from documentation.

---

## 1. Core principle: openHAB always wins

The single invariant everything else follows from:

> A Home Assistant entity only ever shows a value that openHAB has reported.

We never optimistically write entity state after issuing a command. A write goes out;
the entity changes only when the resulting event arrives back over the WebSocket.

This costs a little perceived latency and buys three things:

- **No drift.** The entity cannot disagree with openHAB, because it has no independent
  opinion to disagree with.
- **`autoupdate="false"` works for free.** For those items a command may change the
  state late, or never. Optimistic updates would desync on *every single command*;
  waiting for the echo means the honest failure mode is "the toggle appears to do
  nothing", which is the truth — the device did not act. See §6.
- **Loop safety.** The inbound and outbound paths are disjoint (§5).

Startup and every reconnect re-read all item states over REST before resuming the
stream, so nothing missed during an outage is lost.

We deliberately do **not** set `assumed_state = True`. It renders the two-button
"assumed" UI and would be a lie: we *can* read the real state, it just arrives late.
Users who want in-flight feedback get the `pending_command` attribute instead.

---

## 2. openHAB protocol facts

These were established empirically. Several contradict what a first reading of the
docs suggests, and getting any of them wrong produces silent, hard-to-diagnose failure.

### WebSocket

- URL: `ws[s]://{host}/ws/events?accessToken={token}`.
- **Payloads are double-encoded** — `payload` is a JSON *string* containing JSON. It
  must be parsed twice.
- **Idle timeout is 10 s.** Without traffic the server closes the socket. We send a
  heartbeat every 5 s (`HEARTBEAT_INTERVAL`) and treat the missing PONG as a dead
  connection.
- **A new filter message fully replaces the previous one.** Type and topic filters are
  separate messages, but sending a topic filter does not preserve an earlier one — both
  are re-sent on every connect.

### Event semantics (verified by `scripts/probe_repeat_events.py`)

| Action in openHAB | Events emitted |
|---|---|
| Update to a **different** value | `ItemStateEvent` + `ItemStateUpdatedEvent` + `ItemStateChangedEvent` |
| Update to the **same** value | `ItemStateEvent` + `ItemStateUpdatedEvent` only |
| Command (same or different value) | `ItemCommandEvent` only |

Two consequences drive the code:

1. A real change emits **two** events we subscribe to. Firing our bus event on both
   would double-trigger every automation, so `openhab_bridge_item_event` with
   `type: changed` is keyed off `ItemStateChangedEvent` **only**.
2. A same-value update is not a change and deliberately produces **no** triggerable
   event — matching the user's expectation that a no-op post-update is a no-op. A
   command, however, always fires `type: command`, even when the value is unchanged.
   Scene switches depend on this: they emit *only* `ItemCommandEvent`.

### REST

- `GET /rest/items?metadata=autoupdate` — item list including the autoupdate flag.
- `PUT /rest/items/{name}/state` — postUpdate.
- `POST /rest/items/{name}` — sendCommand.
- **`GET /rest/items/{name}/state` returns plain text and rejects
  `Accept: application/json` with a 400.** This is a real trap: it sits in the resync
  path, where a silent failure just looks like stale state.
- Labels use openHAB's `Label [pattern]` syntax, e.g. `Heart Rate [%d bpm]`. The
  pattern is stripped by `_clean_label()` in `api.py` before it becomes a friendly name.
- If the metadata query fails (older openHAB, restricted token) we fall back to a
  metadata-less list and assume `autoupdate=False` for everything — the more patient,
  safer behaviour rather than the snappier wrong one.

---

## 3. Per-item WebSocket subscriptions

The obvious implementation is a wildcard topic filter, `openhab/items/*`. On this
user's instance that delivers **~15 events/second** continuously, of which essentially
all are discarded — they expose 50–100 items out of ~2,900.

So `websocket.py` builds one topic filter per exposed item, falling back to the
wildcard only above `MAX_TOPIC_FILTERS = 500`, where the filter list itself would
become the larger cost. The filter set is rebuilt on every connect from the current
item set, so adding an item starts streaming without an HA restart.

---

## 4. Type mapping and naming

`const.py` maps openHAB item types to default HA platforms; the user can override per
item in the options flow, so the mapping only needs to be a good default.

| openHAB type | Default platform |
|---|---|
| Switch | switch |
| Contact | binary_sensor |
| Dimmer, Color | light |
| Number, Number:* | sensor |
| Rollershutter | number |
| String, DateTime, Location, Player, Image | sensor |
| Group | delegates to `groupType`, else sensor |

Naming decisions:

- **Entity ID** `{platform}.openhab_{item}`. Set via `async_generate_entity_id`, which
  is a *suggestion at first registration only* — a user's later manual rename is kept.
- **Friendly name** `{openHAB label} ({NAME_SUFFIX})`, e.g. `Kitchen Light (openHAB)`.
- `_attr_has_entity_name = False`, because we want the full self-describing name rather
  than HA's device-prefixed scheme, which would duplicate the suffix against the device.
- **Diagnostic entities get no suffix.** The suffix exists to mark *exposed openHAB
  items*; the built-in diagnostics are entities of this integration, not of openHAB.
- **Item identity is the openHAB item name.** Options are keyed by it, so adding or
  removing items never disturbs another entity's unique ID — history, dashboards and
  automations survive edits.

---

## 5. Loop and echo safety

An HA write produces `sendCommand` → openHAB changes → `ItemStateChangedEvent` → we
update the entity. That round trip must terminate.

**The base case cannot loop, by construction.** Writes originate only from HA service
calls (`async_turn_on`, …); the inbound handler only ever calls
`async_write_ha_state()`. Writing entity state does not invoke command methods, so the
echo dies on arrival. This is a design invariant, and a test asserts that handling an
inbound event issues **zero** HTTP requests.

Four layers sit on top, in `coordinator.py`:

1. **No-op suppression.** Skip a write whose value already equals the current state.
   The subtlety, and a bug found by the unit tests: the comparison must be against the
   **in-flight pending command**, not the state cache. Comparing against the cache made
   a rapid double-toggle silently drop the second command, because the cache still held
   the pre-first-command value.
2. **Pending-write tracking.** `(item → expected value, seq, timestamp)`, letting us
   tell *our* echo from a genuine external change, and letting bus events carry
   `origin: home_assistant`.
3. **Stale-echo rejection.** A fast double-toggle can deliver command #1's echo after
   command #2 was sent. Echoes matching a superseded write are dropped rather than
   flipping the UI backwards. Capped at `MAX_SUPERSEDED = 4` — an early unbounded
   version swallowed genuine events.
4. **Oscillation detector.** The one way to build a true infinite loop is *outside* this
   integration — a user automation, or a mirroring rule in openHAB. We cannot prevent
   that, but we refuse to be the amplifier: >10 writes in 30 s for one item stops
   outbound writes for that item for 60 s and lights the "Feedback loop detected"
   diagnostic. Inbound state keeps flowing throughout; only the outbound leg breaks.

**Deliberately not used:** openHAB's WebSocket `source` filter. The
`ItemStateChangedEvent` produced downstream of a REST command does not reliably carry
our client's source, so filtering on it would drop the very confirmations we depend on.

---

## 6. Items with `autoupdate="false"`

With `autoupdate="false"` a command does not change item state — only an explicit
update or a binding report does. §1 already handles the sync problem; three specific
adjustments remain:

1. **Detected per item** from the metadata query, refreshed on every resync.
2. **Exempt from no-op suppression.** This contradicts layer 1 of §5, and the exemption
   wins. For these items the cached state reflects *the device*, not the last command,
   so "requested equals current" does not mean redundant — re-sending `ON` to a gate
   that reads `ON` is legitimate and often necessary. Loop safety here rests on the
   oscillation detector alone.
3. **Longer, louder pending window.** `PENDING_TIMEOUT_NO_AUTOUPDATE = 60 s` versus 5 s.
   Expiry is not silent: it increments a diagnostic counter, logs the item and command,
   and fires `openhab_bridge_command_unconfirmed` so automations can retry or alert.
   State is still resynced from REST at expiry.

---

## 7. Repairs

Silent breakage is the characteristic failure of a bridge: an item is renamed in
openHAB and the HA entity is simply unavailable forever, with no explanation. Seven
issue kinds are raised through the issue registry, each naming the specific item, and
each **auto-deleted** when its condition clears. Three have guided fix flows.

| Issue | Fixable | Notes |
|---|---|---|
| Item removed from openHAB | ✓ | Remove from the integration, or **re-map** to another item preserving the unique ID — so a rename in openHAB costs no history |
| Item type changed incompatibly | ✓ | Offers platforms valid for the new type |
| Repeated state parse failures | ✓ | Same platform-change flow; threshold 5 |
| Persistently unreachable | — | Backoff at its ceiling for >15 min |
| openHAB too old / no WebSocket | — | Explains the minimum version; no polling fallback exists |
| Feedback loop detected | — | Names the item, points at the likely automation |
| Commands not being acted on | — | ≥3 unconfirmed commands: almost certainly an offline Thing, not a bridge fault |

Two decisions worth keeping:

- **Auth failure is not a repair.** A 401/403 raises `ConfigEntryAuthFailed`, which
  triggers HA's standard reauth flow — a better-fitting mechanism than a repair.
- **`NULL`/`UNDEF` is not a repair.** It is a normal state for an uninitialised openHAB
  item and would generate noise on every restart. It surfaces as unavailability instead.

Two implementation traps, both of which caused HTTP 500s in the real UI and are now
covered by regression tests going through `async_create_fix_flow`:

- **HA opens a repair flow by calling the init step with a *populated* dict** — the
  issue context — not `None`. Guards must test for a specific key
  (`if user_input and "platform" in user_input`), never mere truthiness.
- **A re-map picker cannot be a plain dropdown** at 2,887 items: unsearchable, and it
  preselects the first entry. It is a two-step search-then-pick flow, capped at
  `MAX_REMAP_MATCHES = 30`.

---

## 8. Bus events

`openhab_bridge_item_event` carries `{item, type, value, config_entry_id, origin}`,
plus `old_value` when `type: changed`.

- `type` is `command` or `changed` — automations need to distinguish "someone pressed
  it" from "the value moved". See §2 for why `changed` keys off
  `ItemStateChangedEvent` alone.
- `origin` is `home_assistant` when the event matches a pending write, otherwise
  openHAB — so an automation can avoid reacting to its own actions.
- `openhab_bridge_command_unconfirmed` is separate, and specific to §6.

Writable entities also expose a `last_command` timestamp attribute, updated on every
`ItemCommandEvent`. HA's built-in `last_changed` covers state changes; it does *not*
cover a command that changed nothing, which is exactly the gap this fills.

---

## 9. Diagnostics

Six diagnostic entities per config entry: Connected, Last connected, Reconnect
attempts, Last event, Feedback loop detected, Unconfirmed commands.

"Last event" needs `_attr_should_poll = True` with a 5-minute `SCAN_INTERVAL` —
diagnostics otherwise only re-render on connect/disconnect, so the timestamp appeared
frozen.

**Recorder exclusion is not possible from integration code.** Recorder filtering comes
only from `entity_filter` in `configuration.yaml`; there is no integration-side API.
The README documents the YAML for users who want to exclude the busy ones.

---

## 10. Testing strategy

Three layers, and each one found bugs the others could not:

| Layer | What it is | Example of what only it caught |
|---|---|---|
| Mocked unit tests | `pytest-homeassistant-custom-component`, real HA core in-process | Rapid toggle silently dropping the second command |
| Live E2E (`tests/test_live.py`, `scripts/`) | In-process, against the real openHAB | `Accept: application/json` breaking `/state` with a 400 |
| Real HA in a browser | HA in WSL, driven through the UI | Both platform fix flows returning HTTP 500 |
| CI | hassfest, hacs, ruff, pytest on a clean checkout | The suite not importing at all outside the author's invocation |

The last row is the sharpest lesson: the tests passed locally only because
`python -m pytest` puts the CWD on `sys.path`. A clean checkout running bare `pytest`
got `ModuleNotFoundError`. Fixed with `pythonpath = ["."]` in `pyproject.toml` — and
the general point is that "tests pass" is a claim about one invocation until CI says
otherwise.

Live tests and scripts skip themselves without credentials, which live in a gitignored
`.env` (`OPENHAB_URL`, `OPENHAB_TOKEN`, `OPENHAB_VERIFY_SSL`, `OPENHAB_TEST_ITEM`).
**Write operations only ever target `OPENHAB_TEST_ITEM`** — a scratch Switch with
`autoupdate="false"` and nothing bound to it.

Other environment notes:

- `ruff` ignores D401/D403 — D403 would "correct" openHAB to OpenHAB throughout.
- Windows needs `scripts/windows_sitecustomize.py` (fcntl/resource stubs) and a
  pytest-socket disable to run the HA test harness at all.
- The HACS CI check runs with `ignore: brands`, since no brand has been submitted to
  the home-assistant/brands repo.

---

## 11. Open threads

- **Icon.** No brand submitted. HACS checks `custom_components/{domain}/brand/icon.png`
  locally *before* falling back to `brands.home-assistant.io` — verified by byte
  comparison against another HACS integration — so a local file should work without a
  PR. Untested here.
- **Production rollout.** Recommended path is 2–3 unimportant items first, watching the
  diagnostics for a day, before exposing anything that matters.
