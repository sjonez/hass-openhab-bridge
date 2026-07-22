# openHAB Bridge for Home Assistant

Expose selected openHAB items as native Home Assistant entities, so you can trigger
automations off openHAB state changes and control openHAB items from Home Assistant.

State arrives over the openHAB **event WebSocket**, so updates are pushed rather than polled,
and the connection is re-established automatically if it drops.

## Features

- Choose exactly which openHAB items to expose, as `switch`, `sensor`, `binary_sensor`,
  `light`, `number` or `text` — pre-selected to the closest match for the item's openHAB type.
- Add, edit and remove exposed items at any time from **Configure**, without redoing setup.
- **openHAB always wins**: state is read from openHAB on startup and after every reconnect,
  and entity state is never guessed.
- Actions for reading, updating and commanding *any* openHAB item, exposed or not.
- Diagnostic entities for connection health, plus Repairs for the things that silently break.
- Subscriptions are **per item**, server-side, so a large openHAB install does not push
  thousands of irrelevant events at Home Assistant.

## Requirements

- openHAB 4.0 or newer (the `/ws/events` endpoint is required; there is no polling fallback).
- An openHAB API token: **Settings → API Tokens → Create new token**.

## Installation

**HACS:** add this repository as a custom repository of type *Integration*, install, restart.

**Manual:** copy `custom_components/openhab_bridge` into your Home Assistant `config/custom_components`
directory and restart.

Then add the integration from **Settings → Devices & Services → Add Integration → openHAB Bridge**.

## Configuring items

Open **Configure** on the integration to reach a menu:

| Option | What it does |
| --- | --- |
| **Add items** | Pick from the openHAB items that are not exposed yet, then confirm the entity type for each |
| **Edit an item** | Change one item's entity type or its friendly name |
| **Remove items** | Stop exposing items; their entities are removed |
| **Connection settings** | Change the URL, token or TLS verification without touching your items |

Items are identified by their openHAB item name, so adding or removing one never disturbs the
others' entity IDs, history or automations.

## Naming

- Entity IDs are prefixed: `switch.openhab_kitchen_light`, `sensor.openhab_outdoor_temperature`.
- Friendly names use the openHAB label plus a suffix: **Kitchen Light (openHAB)**. The suffix
  marks entities that mirror an openHAB item; the bridge's own diagnostic entities do not carry
  it, since they belong to the openHAB device rather than standing in for an item.

Both are suggestions applied when the entity is first created. If you rename an entity yourself,
Home Assistant keeps your choice.

## Actions

```yaml
# Read any item, exposed or not
action: openhab_bridge.get_item_state
data:
  item: Kitchen_Light
response_variable: result

# Write a state directly (does not ask the bound thing to act)
action: openhab_bridge.post_update
data:
  item: Kitchen_Light
  state: "ON"

# Ask the bound thing to act
action: openhab_bridge.send_command
data:
  item: Kitchen_Light
  command: "ON"
```

`config_entry_id` is optional unless you have more than one openHAB server configured.

## Items with `autoupdate="false"`

In openHAB, an item with `autoupdate="false"` does **not** change state when it receives a
command — only an explicit update, or the bound thing reporting back, changes it.

This integration never guesses entity state after a command; it waits for openHAB to report the
change. That means an entity cannot drift out of sync with openHAB, but it also means a toggle
will appear to do nothing if the device did not actually act. When that happens:

- an `openhab_bridge_command_unconfirmed` event is fired on the Home Assistant bus, carrying
  `item`, `command` and `config_entry_id`, so you can retry or alert from an automation;
- the **Unconfirmed commands** diagnostic sensor counts it;
- after three in a row, a repair is raised pointing at the likely offline thing.

Commands to these items are also exempt from redundant-write suppression, so re-sending a value
the item already reads (a gate that reads `ON`, say) is passed through rather than swallowed.

## Loop safety

An HA write becomes an openHAB command, which becomes a state change, which updates the HA
entity. That round trip terminates by design: inbound events only ever write entity state, and
writing entity state never issues a command.

The remaining risk is a loop built *outside* this integration — an HA automation that commands an
item whenever it changes, or an openHAB rule doing the same. The integration will not amplify it:
redundant writes are skipped, and if an item is commanded repeatedly in a short window, outbound
commands to it are paused, the **Feedback loop detected** diagnostic turns on, and a
repair names the item. Inbound state keeps working throughout.

## Triggering on commands and changes

openHAB rules distinguish *received command* from *changed*, and Home Assistant's state machine
cannot express the difference — a command is an action, not a state. So both are reported on the
Home Assistant bus as `openhab_bridge_item_event`, told apart by the `type` field.

Verified against a real openHAB server:

| In openHAB | openHAB emits | Bus event |
| --- | --- | --- |
| Item goes OFF → ON | `ItemStateEvent` + `ItemStateUpdatedEvent` + `ItemStateChangedEvent` | `type: changed`, with `old_value` |
| Item is ON, updated to ON again | `ItemStateEvent` + `ItemStateUpdatedEvent` | none — nothing changed |
| Item commanded, any value | `ItemCommandEvent` | `type: command` |

A command is reported whatever value it carries, including commanding an item to the state it
already holds — the case that matters for scene switches, button items and `autoupdate="false"`
items, where openHAB emits a command event and nothing else.

An update that repeats the value an item already had is deliberately **not** reported.

Note openHAB emits *two* events for a single change; the integration reports it once, keyed off
`ItemStateChangedEvent`, so automations do not double-trigger.

Example:

```yaml
automation:
  - alias: Hallway scene switch pressed
    triggers:
      - trigger: event
        event_type: openhab_bridge_item_event
        event_data:
          item: Zigbee_SceneSwitch_Hallway_Trigger
          type: command
          origin: openhab
    actions:
      - action: light.toggle
        target:
          entity_id: light.hallway
```

Event data:

| Field | Meaning |
| --- | --- |
| `item` | openHAB item name |
| `type` | `command` or `changed` |
| `value` | the commanded value, or the new state |
| `old_value` | the previous state — only on `type: changed` |
| `origin` | `openhab`, or `home_assistant` if this is the echo of a command HA sent |
| `config_entry_id` | which openHAB server |

**Filter on `origin: openhab`** as in the example. Without it, an automation that commands an
item in response to this event will re-trigger on its own echo. The feedback-loop detector will
eventually break such a loop, but it is better not to build one.

**Filter on `type` too.** A real change produces both this event and an ordinary entity state
change, so an automation listening to both would fire twice. Use whichever suits: the entity
state trigger for normal automation, the bus event when you need the command/change distinction
or the old value.

### Entity attributes

Every exposed entity carries:

| Attribute | Meaning |
| --- | --- |
| `openhab_item` | the openHAB item name behind this entity |
| `openhab_type` | its openHAB type, e.g. `Number:Temperature` |
| `last_command` | when the item last received a command, ISO timestamp |
| `pending_command` | a command sent but not yet confirmed by openHAB (only while in flight) |
| `autoupdate` | present and `false` only for items with autoupdate disabled |

`last_command` fills the gap Home Assistant leaves. `last_changed` covers state changes — a
`postUpdate` that alters the value moves it, and one that repeats the existing value writes
nothing at all, which is correct. But a command changes no state, so nothing in Home Assistant
would otherwise move. Use it in templates:

```yaml
{{ state_attr('switch.openhab_garage_gate', 'last_command') }}
```

It updates on **every** command, including ones Home Assistant sent itself, and including
commands whose value matches the item's current state. Because only an attribute changes, the
entity's `last_updated` moves but `last_changed` does not — which is precisely the distinction:
something happened, but nothing changed.

## Recorder / history

Of the diagnostic entities, only **Last event** changes regularly — it tracks the
most recent openHAB event, so on a busy server it moves constantly. The others
(Last connected, Reconnect attempts, Unconfirmed commands, Connected, Feedback loop detected)
only change when something actually happens, so they cost almost nothing.

The diagnostics are polled every 5 minutes rather than the usual 30 seconds, which keeps
Last event to roughly 290 recorder rows a day instead of ~2,900. If you want none at all,
Home Assistant only supports this in `configuration.yaml` — there is no way for an
integration to exclude its own entities:

```yaml
recorder:
  exclude:
    entities:
      - sensor.openhab_last_event
```

Or drop the history for every diagnostic this integration creates:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.openhab_last_event
      - sensor.openhab_last_connected
      - sensor.openhab_reconnect_attempts
      - sensor.openhab_unconfirmed_commands
```

Excluding an entity from the recorder does not affect its live state, so automations and
alerts on these entities keep working.

## Repairs

Raised automatically, and cleared automatically once resolved:

- an exposed item was **deleted or renamed** in openHAB (offers to re-map, keeping entity history);
- an item's **type changed** to something the chosen entity type cannot represent;
- an item keeps sending **values the entity cannot parse**;
- openHAB has been **unreachable** for a long time;
- the server has **no event WebSocket**;
- a **feedback loop** was detected;
- **commands are not being acted on** by an `autoupdate="false"` item.

An expired or revoked API token instead triggers Home Assistant's standard reauthentication
prompt.

## Development

[`docs/DESIGN.md`](docs/DESIGN.md) records why the integration is built the way it is —
the openHAB protocol facts that were established empirically, the loop-safety layers,
and the decisions that look arbitrary without their reasoning. Read it before changing
event handling, the write path, or the repair flows.

Requires Python 3.13 (Home Assistant's minimum).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check .
```

### Testing against a real openHAB

Two optional layers, both driven by a gitignored `.env` in the repository root:

```
OPENHAB_URL=http://openhab.local:8080
OPENHAB_TOKEN=your-api-token
OPENHAB_VERIFY_SSL=0        # only for HTTPS with a self-signed cert
OPENHAB_TEST_ITEM=TestItem  # optional; see below
```

**Protocol smoke test** — strictly read-only. Checks the item list, autoupdate metadata,
item-type coverage, the WebSocket handshake, heartbeat, payload encoding, and measures how
much traffic the per-item topic filter actually saves:

```
.venv\Scripts\python scripts\smoke_test.py --seconds 60
```

**Live end-to-end tests** (`tests/test_live.py`) run real Home Assistant in-process against
your real openHAB: real config entries, entity registry, state machine and issue registry.
They skip themselves unless `OPENHAB_URL` and `OPENHAB_TOKEN` are set, so CI is unaffected.

Every live test is read-only **except** those that need a write, which only ever touch the
item named by `OPENHAB_TEST_ITEM` and skip entirely if it is unset. Use a dedicated scratch
item with nothing bound to it — ideally one with `autoupdate="false"`, which exercises the
unconfirmed-command path. Those tests take just over a minute, since that window is 60s.

### Running the tests on Windows

Home Assistant's test harness targets Linux, so three workarounds are needed. CI runs on
Linux without any of them, and that remains the authoritative result.

1. **`lru-dict` is pinned to a version with no Python 3.13 Windows wheel**, and building it
   from source needs the MSVC build tools. Install with the pin overridden instead:

   ```
   echo lru-dict==1.4.1 > overrides.txt
   .venv\Scripts\python -m pip install uv
   .venv\Scripts\python -m uv pip install -r requirements-test.txt --override overrides.txt
   ```

2. **`homeassistant.runner` imports the POSIX-only `fcntl` and `resource` modules** at import
   time, from a pytest plugin, so the session cannot start. Copy the stub into the venv:

   ```
   copy scripts\windows_sitecustomize.py .venv\Lib\site-packages\sitecustomize.py
   ```

   Both modules are used in exactly one place each — the single-instance lock and the file
   descriptor limit — neither of which unit tests reach.

3. **Socket blocking**: the harness blocks sockets but permits AF_UNIX ones, which covers the
   asyncio event loop's self-pipe on Linux. Windows has no AF_UNIX, so the loop's self-pipe is
   a TCP socket and gets blocked. `tests/conftest.py` disables the block on Windows only.
