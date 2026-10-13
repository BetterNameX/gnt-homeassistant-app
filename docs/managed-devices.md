# Managed Devices - Home Assistant Guide

> **Note:** This document is intended as context for AI-assisted development. For user-facing documentation, see https://docs.myzendo.com

A managed device is a phone, tablet or wall panel running the Zendo app with Always-On Display (Kiosk) on. It registers with Home Assistant so automations can control it: wake the screen, set the brightness or volume, lock it, and so on. Each managed device is a Home Assistant device with its own sensor entities (battery, screen, volume, locked and so on). A wall panel can also have auto-brightness, wake on approach and presence entities (see [Wall Panel Settings and Presence](#wall-panel-settings-and-presence)).

The code lives in `custom_components/zendo/managed_devices.py`. The services are registered in `__init__.py`.

---

## Records and Managed Device IDs

The integration owns each managed device. It keeps one **record** per managed device, keyed by a **managed device ID**:

- The managed device ID is also the Home Assistant device identifier `(zendo, <managed device ID>)`, and the entities' unique IDs are `md_<managed device ID>_<property>`. Automations point at the Home Assistant device, so they follow the record.
- The record holds the Zendo device that is **attached** to it right now: its `profile_id` and `device_id`, plus its name, model, operating system and its version, app version, last state, whether it's online, why it's offline and when it was last seen.
- The attached device can change. A replacement tablet can take over a record (see [Replacing a Device](#replacing-a-device)), and the record keeps its managed device ID, Home Assistant device, entity IDs, area and the name the user gave it.

**Managed device IDs are opaque.** Never build one from a profile ID and device ID, and never parse one. Records made before 1.9.0 have the ID `<profile_id>_<device_id>`, and new ones may have that form too, but a record's ID says nothing about the device attached to it now.

Records are persisted in the config entry under `managed_devices`, keyed by managed device ID. The persisted fields are the attached ids, the device info (including `device_os_version`), the app version, `offline_reason`, `last_seen` and, from 1.10.0, `optional_entities` (see [Created When Reported](#created-when-reported)). The state and `online` are not persisted: after a restart every record starts offline, with its entities unavailable until the device sends its first heartbeat. See [Online Status](#online-status) for the reason a restored record gets.

`last_seen` is a Unix timestamp in seconds: when the device was last in touch with Home Assistant.

- Registering and every state update set it to now. Unregistering sets it to now if the record was online, and leaves it alone if it was already offline.
- A device that times out keeps the time of its last heartbeat. The offline check doesn't move it.
- It is written to the config entry when a device registers, is replaced or is deleted, when it goes online or offline, when Home Assistant stops and when the integration is unloaded, not on every heartbeat. Writing it at stop is what keeps the last heartbeat across a restart. Without it, a device that was online would come back with the time it last came online.
- A write at stop lands: the config entry store saves at Home Assistant's final write, which runs after every stop listener.

---

## Online Status

A record is online from when its device registers or sends a state update until it unregisters or misses its heartbeats. The app sends a heartbeat every 60 seconds. The offline check runs every 60 seconds and marks a record offline once its device has gone 180 seconds without registering or sending a state update.

Every offline record has an `offline_reason`. It is `null` while the record is online.

| `offline_reason` | Set by | Meaning |
|---|---|---|
| `timeout` | The offline check | The device stopped sending heartbeats: it lost its connection, the app stopped, or the device is off. |
| `unregistered` | `unregister_managed_device` | Always-On Display was turned off on the device, or it signed out of Zendo. The app shows the device as not monitored. |

- The reason is a plain string on the wire, not an enum, so a hub can add one without an app release. The registry only writes the values in `OFFLINE_REASONS`, and the app and the Homey app know the same values. Keep the three in sync.
- `unregister_managed_device` records `unregistered` even when the record has already timed out, so a device whose Always-On Display was turned off after it lost its connection reads as not monitored rather than lost.
- After a restart, a record comes back with the reason it was persisted with. A record that was online when Home Assistant stopped has no reason, and nor does one persisted by an older version: both come back as `timeout`, as does any value the integration doesn't know.

---

## Finding a Record

`register_managed_device`, `unregister_managed_device` and `update_managed_device_state` find the record by its attached `profile_id` + `device_id`, by scanning every record. If two records match, the first one wins and a warning is logged.

When the app calls `unregister_managed_device` or `update_managed_device_state` and no record has that device attached, the service succeeds with `registered: false`. That happens after another device has taken over the record, or after the record was deleted. The app registers again when it sees it.

---

## Choosing a Managed Device ID

A new ID is chosen only when `register_managed_device` finds no record attached to the calling device. The first of these that applies wins:

1. **The app's hint**, the optional `managed_device_id` field, if it's valid and free. Valid means a non-empty string of at most 255 characters that isn't `service` (the identifier of the Zendo service device). Free means no record has it. An invalid hint is ignored, never an error.
2. **`<profile_id>_<device_id>`**, if it's free.
3. **`md_<uuid4 hex>`**.

The ID is chosen before the Home Assistant device is created or looked up. Creating the device from `<profile_id>_<device_id>` first would leave a stray device behind whenever the record has a different ID.

A hint that matches a deleted Home Assistant device brings it back with the same device ID, because Home Assistant restores a deleted device, and the registry entries of its deleted entities, when they are created again with the same identifiers and unique IDs. The app stores the managed device ID it gets back and sends it as the hint, so a device reconnecting after its record was deleted, or after the integration was reinstalled, gets its old Home Assistant device back.

A device that signs in again gets a new Zendo `device_id`. Its old record is still attached to the old `device_id`, so it gets a new record and takes its old one back with **Replace Existing Device** in the app.

---

## Services

All of these are internal services, called by the Zendo app. Field names and response keys are snake_case. Expected failures come back in the response as `success: false` plus an `error` code, and are never raised.

| Service | Fields | Response mode | Response |
|---|---|---|---|
| `register_managed_device` | `profile_id`, `device_id`, `device_name`, `device_brand`, `device_type`, `device_model_name`, `device_os`, optional `device_os_version`, `app_version`, optional `managed_device_id` | optional | `{success: true, managed_device_id}` |
| `unregister_managed_device` | `profile_id`, `device_id` | optional | `{success: true, registered}` |
| `update_managed_device_state` | `profile_id`, `device_id`, `state` | optional | `{success: true, registered}` |
| `list_managed_devices` | none | only | `{managed_devices: [...]}` |
| `replace_managed_device` | `profile_id`, `device_id`, `managed_device_id` | only | `{success: true, managed_device_id, previous_device_name}` or `{success: false, error}` |
| `delete_managed_device` | `managed_device_id` | optional | `{success: true, deleted}` or `{success: false, error: "target_online"}` |

`device_os_version` is the version of the device's operating system (for example `18.5`), or `null`. An app that doesn't send it leaves it `null`.

`unregister_managed_device` keeps the record, its Home Assistant device and its entities, and marks them unavailable with the offline reason `unregistered`. Turning off Always-On Display or signing out of the app does this.

### State

`update_managed_device_state` takes `state`, a JSON-stringified object. The app sends it every 60 seconds as its heartbeat, and whenever a value changes. Every key is optional, and the integration stores the object as it comes, so a key it doesn't know is kept and listed too.

| Key | Type | Entity |
|---|---|---|
| `battery_level` | number, percent | Battery |
| `battery_state` | `charger_unplugged`, `charging` or `plugged_not_charging` | Battery state |
| `screen_brightness` | number, percent: the level the user set (see [State Keys](#state-keys) for how it works with auto-brightness) | Screen brightness |
| `volume` | number, percent | Volume |
| `screen_state` | `active` or `screensaver` | Screen |
| `color_scheme` | `dark` or `light` | Color scheme |
| `screensaver_mode` | `none`, `dim`, `black` or `clock` | Screensaver mode |
| `screensaver_timer` | number, minutes | Screensaver timer |
| `locked` | boolean | Locked |
| `app_version` | string | No entity: it sets the Home Assistant device's software version |
| `ip_address` | string or `null`: the device's IP address | No entity |
| `wifi_ssid` | string or `null`: the Wi-Fi network's name | No entity |
| `wifi_signal` | integer or `null`: the Wi-Fi signal strength in dBm | No entity |
| `auto_brightness` | `on`, `off` or `null` | Auto-brightness, from 1.10.0 |
| `wake_on_approach` | `on`, `off` or `null` | Wake on approach, from 1.10.0 |
| `wake_on_approach_sensitivity` | `very_close`, `close`, `medium`, `far` or `null` | Wake on approach sensitivity, from 1.10.0 |
| `presence` | `present`, `absent` or `null` | Presence, from 1.10.0 |

`ip_address`, `wifi_ssid` and `wifi_signal` are only for device status in the app, so they have no entities. The app sends `null` for any of them it can't read. A change in `wifi_signal` alone fires no event (see [Events](#events)).

The last four are a wall panel's settings and presence sensor. Their entities are made only once the device reports a value for them. See [Wall Panel Settings and Presence](#wall-panel-settings-and-presence).

### Error Codes

| Code | Returned by | Meaning |
|---|---|---|
| `caller_not_registered` | `replace_managed_device` | No record has the calling device attached. It hasn't finished registering. |
| `target_not_found` | `replace_managed_device` | No record has that managed device ID. |
| `target_online` | `delete_managed_device` | The record's device is online, so it can't be deleted. |

The same strings are used by the Homey app, and the Zendo app narrows them at its boundary. Keep the three in sync.

### `list_managed_devices`

```yaml
managed_devices:
  - managed_device_id: "profileId123_deviceId456"
    profile_id: "profileId123"
    device_id: "deviceId456"
    device_name: "Front Door"
    name_by_user: "Front door panel"
    device_brand: "Apple"
    device_type: "tablet"
    device_model_name: "iPad Air"
    device_os: "ios"
    device_os_version: "18.5"
    app_version: "26.10.1"
    online: true
    offline_reason: null
    last_seen: 1791763200000
    state:
      battery_level: 82
      battery_state: "charging"
      screen_state: "active"
      locked: false
      app_version: "26.10.1"
      ip_address: "192.168.1.42"
      wifi_ssid: "Main Wi-Fi"
      wifi_signal: null
  - managed_device_id: "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00"
    profile_id: "profileId123"
    device_id: "deviceId789"
    device_name: "Hallway"
    name_by_user: null
    device_brand: "Samsung"
    device_type: "tablet"
    device_model_name: "Galaxy Tab A9"
    device_os: "android"
    device_os_version: null
    app_version: "26.9.4"
    online: false
    offline_reason: "timeout"
    last_seen: 1791759600000
    state: null
```

- `name_by_user` is the name the user gave the device in Home Assistant, or `null`.
- `device_os_version` is `null` when the app didn't send it.
- `offline_reason` is `timeout` or `unregistered` while the record is offline, and `null` while it's online. See [Online Status](#online-status).
- `last_seen` is in milliseconds since the Unix epoch, or `null` when the device hasn't been seen since it was restored from a version before 1.9.0.
- `state` is the last state the device sent, exactly as it sent it, or `null` when it hasn't sent one since the record was made or Home Assistant started. An offline record keeps its last state until Home Assistant restarts.

Each item is built by `_serialize_managed_device`, which also builds the data of `bngnt_managed_device_updated`.

---

## Events

The integration fires two events on the Home Assistant bus, so the app can show each device's status as it changes. Their data has top-level keys.

| Event | Data | Fired when |
|---|---|---|
| `bngnt_managed_device_updated` | One `list_managed_devices` item | A record is created or changed. See below. |
| `bngnt_managed_device_removed` | `{managed_device_id}` | A record is gone. See below. |

`bngnt_managed_device_updated` fires when:

- a device registers, whether its record is new or not;
- a device unregisters and its record changed: it was online, or its offline reason wasn't already `unregistered`;
- the offline check marks a record offline, once for each record;
- a device takes over a record with `replace_managed_device`, for the target;
- a device sends a state update and it was offline, it's the record's first state, or anything other than `wifi_signal` changed. The integration compares the new state with the old one before it replaces it.

`bngnt_managed_device_removed` fires when:

- a device takes over a record with `replace_managed_device`, for the caller's own record, just before the target's `bngnt_managed_device_updated`;
- a record is deleted, with `delete_managed_device` or with the Delete button in Home Assistant.

Notes:

- **The signal drifts.** A change in `wifi_signal` alone never fires an event, because the signal changes from one heartbeat to the next. The app reads it from `list_managed_devices`.
- **Removal is its own event.** That way, `bngnt_managed_device_updated` can always be parsed as a full list item, and an older app ignores an event it doesn't know.
- **The app subscribes only while it shows device status.** Home Assistant sends an event type only to the WebSocket clients subscribed to it, so the integration can always fire them.
- **Keep them in sync with the Homey app.** It emits the same two events, with camelCase payloads (`managedDeviceId`), on the same triggers.

---

## Automation Services

These services are for automations to control a device. Each takes `device`, one or more Home Assistant devices, and sends each one's attached Zendo device a JSON-RPC 2.0 command in a `bngnt_managed_device_command` event. The app answers with `managed_device_command_response`, and the service waits up to 10 seconds for the answer. The commands to several devices go out in parallel.

| Service | Fields | JSON-RPC method and params |
|---|---|---|
| `managed_device_screensaver_configure` | optional `mode`, `timer` | `screensaver.set` `{mode?, timer?}` |
| `managed_device_screen_configure` | optional `brightness`, `color_scheme` | `screen.set` `{brightness?, colorScheme?}` |
| `managed_device_screen_wake_up` | none | `screen.wakeUp` |
| `managed_device_audio_configure` | optional `volume` | `audio.set` `{volume?}` |
| `managed_device_app_reload` | none | `app.reload` |
| `managed_device_app_lock` | `pin`, optional `message` | `app.lock` `{pin, message?}` |
| `managed_device_app_unlock` | none | `app.unlock` |
| `managed_device_auto_brightness_configure` (from 1.10.0) | `mode`: `on` or `off` | `autoBrightness.set` `{mode}` |
| `managed_device_wake_on_approach_configure` (from 1.10.0) | optional `mode`: `on` or `off`, optional `sensitivity`: `very_close`, `close`, `medium` or `far`; at least one of them | `wakeOnApproach.set` `{mode?, sensitivity?}` |

- A settings method gets only the fields the call has, and the device leaves the rest unchanged.
- The two 1.10.0 services skip the devices that don't support the setting. See [Changing the Settings](#changing-the-settings).
- YAML reads an unquoted `on` or `off` as a boolean, so `mode` takes `true` and `false` as `on` and `off`.

### Errors

The call fails when a device is offline, when it doesn't answer within 10 seconds, and when it answers with a JSON-RPC error:

| Code | Meaning | Error |
|---|---|---|
| `-32601` | Method not found: the app on the device predates the command. | `Device '<name>' needs a newer version of the Zendo app` |
| `-32001` | Not supported: the device has no light sensor or no wake sensor, or it got a sensitivity and has no levels. | `Device returned error: <message>` |
| `-32602` | Invalid params. | `Device returned error: <message>` |

- From 1.10.0, Method not found reads as needing a newer app for every command, not only the new ones.
- `-32001` is rare: the services don't send a setting to a device whose last state says it doesn't support it.

---

## Wall Panel Settings and Presence

From 1.10.0, a wall panel running Zendo reports its auto-brightness and wake on approach settings and its presence sensor, and automations can change the settings. The app owns the settings: Home Assistant reads them from the state and changes them with a command, as it does the screensaver.

### State Keys

| Key | Values | `null` means |
|---|---|---|
| `auto_brightness` | `on` or `off` | The device has no working light sensor. |
| `wake_on_approach` | `on` or `off` | The device has no working presence or proximity sensor. |
| `wake_on_approach_sensitivity` | `very_close`, `close`, `medium` or `far` | Wake on approach has no sensitivity levels on this device: only a sensor that tells distance has them. |
| `presence` | `present` or `absent` | The device has no presence sensor, or no reading yet. |

`screen_brightness` is still the level the user set. With auto-brightness on, it's how bright the screen is in normal room light, and the device scales the brightness up and down from it as the light changes. The descriptions of `managed_device_screen_configure`'s `brightness` and of `managed_device_auto_brightness_configure` say so.

### Entities

| Entity | Type | States |
|---|---|---|
| Auto-brightness | Sensor, enum | On, Off |
| Wake on approach | Sensor, enum | On, Off |
| Wake on approach sensitivity | Sensor, enum | Very close, Close, Medium, Far |
| Presence | Binary sensor, occupancy | Detected (`present`), Clear (`absent`) |

- **Presence uses the occupancy device class**, which reads Detected and Clear. The presence device class reads Home and Away, which is wrong at a commercial property.
- **A value the integration doesn't know reads as unknown**, and so does `null`. The values are loose on the wire, and an enum sensor given a value outside its options would fail the state update. From 1.10.0 every enum sensor works this way, including battery state, screen, color scheme and screensaver mode.
- Their names, state labels and icons come from `strings.json` and `icons.json` through translation keys. The other managed device entities set their names and icons in code.

### Created When Reported

A record gets each of these four entities only once a state update has a value other than `null` for it. A phone or tablet without the sensor never gets an entity that is always unknown.

- An entity stays once it's made, even if the device later reports `null` or another device takes over the record. It then reads as unknown.
- The record lists them in `optional_entities`, which is persisted, so they're restored at startup with the others, unavailable until the device's first heartbeat.
- They're listed as soon as they're made, so two state updates in a row can't add one twice.
- `replace_managed_device`: the target keeps its own, and gets any the caller's state has a value for.

### Presence

Presence is what the panel's own sensor sees, and that differs by sensor:

- **A sensor that tells distance** sees only a short way: presence means someone right at the panel, and the wake on approach sensitivity sets how close.
- **A presence sensor** reports someone up to about 1 m away. `absent` arrives a few seconds after someone leaves.

Docs, action descriptions and entity names here never name a device's maker or model.

Use it to react to someone at the panel, not to tell whether anyone is in the area.

### Changing the Settings

`managed_device_auto_brightness_configure` and `managed_device_wake_on_approach_configure` send a setting only to the devices that support it:

- A device whose last state has `null` for the setting (`auto_brightness` or `wake_on_approach`) is skipped, with an info log.
- A `sensitivity` goes only to a device whose `wake_on_approach_sensitivity` isn't `null`. A device without levels still gets the `mode` when the call has one, and is skipped when it doesn't.
- If that leaves no device, the call fails with a validation error saying the selected devices don't support it.
- A device whose state doesn't have the key is still sent the command: an older app, or a device that hasn't sent its state since Home Assistant started. An older app answers Method not found, and an offline device fails as offline.

---

## Replacing a Device

`replace_managed_device` attaches the calling device to another record, the **target**, so the automations that control the target control the calling device instead. It is synchronous, with no `await`, so no other service call can change the records halfway through.

1. No record has the calling device attached: `caller_not_registered`.
2. No record has the target's managed device ID: `target_not_found`.
3. The calling device is already attached to the target: success, with no change and no events. A retry after a lost response is safe. `previous_device_name` is then the target's current name.
4. Otherwise:
   - The target takes the caller's attached ids, device info (including `device_os_version`), app version, state, `online`, `offline_reason` and `last_seen`.
   - The target's Home Assistant device gets the caller's name, model and app version. Its device ID, entity IDs, area and `name_by_user` stay.
   - The caller's own record is removed. Its config entry is removed from its Home Assistant device, and Home Assistant then removes that device and its entities.
   - The target's entities take the caller's availability and state. The target keeps its optional entities, and gets any the caller's state has a value for (see [Created When Reported](#created-when-reported)).
   - `bngnt_managed_device_removed` fires for the caller's record, then `bngnt_managed_device_updated` for the target.
   - The response carries the target's managed device ID and `previous_device_name`, the target's device name from before, so the app can offer to rename itself to it.

An online target can be taken over: the app asks for a second confirmation, and the integration doesn't check. The device that was attached to the target gets `registered: false` from its next heartbeat and registers again with a new record.

---

## Deleting a Device

Only offline managed devices can be deleted. Both ways go through `BNGntManagedDeviceRegistry.delete_device`, which fires `bngnt_managed_device_removed`.

- **`delete_managed_device`** forgets the record and removes the config entry from its Home Assistant device, so Home Assistant removes the device and its entities. An unknown managed device ID returns `deleted: false`.
- **The Delete button** on a managed device's page in Home Assistant calls `async_remove_config_entry_device` in `__init__.py`:
  - The Zendo service device can't be deleted.
  - Nothing can be deleted while the integration isn't loaded.
  - An online device is refused with an error that tells the user to turn off Always-On Display or sign the device out of Zendo first.
  - Otherwise the record is forgotten and Home Assistant removes the device and its entities itself.
  - A device with no record (an orphan) is always allowed.

---

## Migration to Minor Version 2

Before 1.9.0, every unique ID and the Zendo service device's identifier contained the config entry ID, which changes when the integration is reinstalled. 1.9.0 sets the config flow's `MINOR_VERSION` to 2, and `async_migrate_entry` moves existing entries:

| What | Before | After |
|---|---|---|
| Managed device sensors and binary sensor | `{entry_id}_md_{key}_{property}` | `md_{key}_{property}` |
| Status binary sensor | `{entry_id}_status` | `status` |
| Notify entity (a person) | `{entry_id}_{profile_id}` | `{profile_id}` |
| Event entity (a device group) | `{entry_id}_{profile_id}` | `{profile_id}` |
| Zendo service device identifier | `(zendo, {entry_id})` | `(zendo, service)` |

- Unique IDs are rewritten with `er.async_migrate_entries`, which also covers orphaned registry entries. Entity IDs and registry IDs don't change. Notify and event entities can share a profile ID because they're in different entity domains.
- The service device is moved with `async_update_device(new_identifiers=...)`. Changing only the entities' `DeviceInfo` would create a second "Zendo" device.
- Each step skips what's already done: a unique ID without the prefix is left alone, and the device is moved only if it still has the old identifier and no device has the new one. A conflict is logged and skipped. Running the migration twice is safe.
- Managed device identifiers `(zendo, <managed device ID>)` didn't contain the entry ID, so they don't change.

---

## Backward Compatibility

- **An older app with a 1.9.0 integration.** It sends no hint, so new records get `<profile_id>_<device_id>` when it's free, as before. It sends no `device_os_version` and none of the new state keys, so those are `null` or missing. It ignores the new response and list fields, and doesn't subscribe to the events.
- **A 1.9.0 app with an older integration.** The integration drops the unknown `managed_device_id` and `device_os_version` fields. A response without `managed_device_id` tells the app the integration is older. The app shows **Replace Existing Device** only when the `integration_version` attribute of `binary_sensor.zendo_status` is 1.9.0 or later, and device status on Control Panels only when it is 1.9.1 or later.
- **Records persisted by an older integration.** They have no `device_os_version` or `offline_reason`. They're restored with `device_os_version: null` and `offline_reason: timeout`, and the device's next registration sets both.
- **Downgrading the integration.** An earlier version builds the old unique IDs again, so its entities get new entity IDs, and it creates a second service device under the entry ID. Avoid downgrading below 1.9.0.

From 1.10.0 (wall panel settings and presence):

- **An older app with a 1.10.0 integration.** Its state has none of the four keys, so its records get none of the new entities. The two new services still send it the command, it answers Method not found, and the call fails saying the device needs a newer version of the Zendo app.
- **A newer app with an older integration.** The integration stores the state as it comes, so it keeps the four keys and lists them in `list_managed_devices`, but it makes no entities for them and has neither service. Automations can't change the settings until the integration is updated.
- **Records persisted by an older integration.** They have no `optional_entities`, so they're restored without the new entities. Each is made with the device's first state update that has a value for it.
- **Downgrading below 1.10.0.** The earlier version drops `optional_entities` the next time it persists the records. After upgrading again, the entities come back, with the same entity IDs, with the device's next state update that has a value for them.
