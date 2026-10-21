# Managed Devices - Home Assistant Guide

> **Note:** This document is intended as context for AI-assisted development. For user-facing documentation, see https://docs.myzendo.com

A managed device is a phone, tablet or wall panel running the Zendo app with Always-On Display (Kiosk) on. It registers with Home Assistant so automations can control it: wake the screen, set the brightness or volume, lock it, and so on. Each managed device is a Home Assistant device with its own sensor entities (battery, screen, volume, locked and so on). A wall panel can also have auto-brightness, wake on approach and presence entities (see [Wall Panel Settings and Presence](#wall-panel-settings-and-presence)).

The code lives in `custom_components/zendo/managed_devices.py`, the admin code in `admin_code.py` (see [Admin Code](#admin-code)) and the settings backup in `settings_backup.py` (see [Settings Backup](#settings-backup)). The services and WebSocket commands are registered in `__init__.py`.

---

## Records and Managed Device IDs

The integration owns each managed device. It keeps one **record** per managed device, keyed by a **managed device ID**:

- The managed device ID is also the Home Assistant device identifier `(zendo, <managed device ID>)`, and the entities' unique IDs are `md_<managed device ID>_<property>`. Automations point at the Home Assistant device, so they follow the record.
- The record holds the Zendo device that is **attached** to it right now: its `profile_id` and `device_id`, plus its name, model (and a wall panel's model slug, launcher ID and serial number), operating system and its version, app version, last state, whether it's online, why it's offline and when it was last seen.
- The attached device can change. A replacement tablet can take over a record (see [Replacing a Device](#replacing-a-device)), and the record keeps its managed device ID, Home Assistant device, entity IDs, area and the name the user gave it.

**Managed device IDs are opaque.** Never build one from a profile ID and device ID, and never parse one. Records made before 1.9.0 have the ID `<profile_id>_<device_id>`, and new ones may have that form too, but a record's ID says nothing about the device attached to it now.

Records are persisted in the config entry under `managed_devices`, keyed by managed device ID. A record's admin code and settings backup aren't: each has its own store (see [Admin Code](#admin-code) and [Settings Backup](#settings-backup)). The persisted fields are the attached ids, the device info (including `device_os_version`, from 1.9.5 `device_model_slug`, and from 1.9.6 `device_launcher_id` and `device_serial_number`), the app version, `offline_reason`, `last_seen` and, from 1.9.3, `optional_entities` (see [Created When Reported](#created-when-reported)). The state and `online` are not persisted: after a restart every record starts offline, with its entities unavailable until the device sends its first heartbeat. See [Online Status](#online-status) for the reason a restored record gets.

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

A new ID is chosen only when `register_managed_device` finds no record attached to the registering device. The first of these that applies wins:

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
| `register_managed_device` | `profile_id`, `device_id`, `device_name`, `device_brand`, `device_type`, `device_model_name`, optional `device_model_slug`, optional `device_launcher_id`, optional `device_serial_number`, `device_os`, optional `device_os_version`, `app_version`, optional `managed_device_id` | optional | `{success: true, managed_device_id, settings_restore_pending}` |
| `unregister_managed_device` | `profile_id`, `device_id` | optional | `{success: true, registered}` |
| `update_managed_device_state` | `profile_id`, `device_id`, `state` | optional | `{success: true, registered, settings_restore_pending}` |
| `list_managed_devices` | none | only | `{managed_devices: [...]}` |
| `replace_managed_device` | `profile_id`, `device_id`, `managed_device_id` | only | `{success: true, managed_device_id, previous_device_name}` or `{success: false, error}` |
| `delete_managed_device` | `managed_device_id` | optional | `{success: true, deleted}` or `{success: false, error: "target_online"}` |

`device_os_version` is the version of the device's operating system (for example `18.5`), or `null`. An app that doesn't send it leaves it `null`.

`device_model_slug`, from 1.9.5, is the ID of the device's model in the Zendo app's wall panel catalogue, or `null`. Only a wall panel has one: phones, tablets and any device on iOS never send it. A wall panel reports the same `device_type` as a tablet, so the slug is what tells them apart, and the app shows a wall panel icon for a record that has one.

- The integration stores it as it comes and doesn't check its form: the app's catalogue owns the slugs.
- No field, `null`, an empty string or a value that isn't a string all read as `null`, never as an error.
- Every registration sets it, like the rest of the device info, so a device that stops sending it ends with `null`.
- It isn't part of the Home Assistant device's model, which stays the brand and model name.

`device_launcher_id` and `device_serial_number`, from 1.9.6, are what Zendo Launcher on a wall panel tells the app, or `null`. The launcher ID is Zendo Launcher's own ID for the panel (for example `K7QM3T`), which the app shows as Launcher ID. It's unrelated to `device_id` and to the managed device ID. The serial number is the panel's hardware serial, which Zendo Launcher reports once Zendo Provisioning has given it one. The app sends each only when it knows it: phones, tablets and any device without Zendo Launcher send neither, and a wall panel whose launcher has no serial number yet sends only the launcher ID.

- The integration stores them as they come and doesn't check their form: Zendo Launcher owns both.
- No field, `null`, an empty string or a value that isn't a string all read as `null`, never as an error.
- Every registration sets them, like the rest of the device info, so a device that stops sending one ends with `null`.
- `device_serial_number` is also the Home Assistant device's serial number, set and cleared with it. The launcher ID is in none of the Home Assistant device's fields.
- The launcher usually answers the app after the app has registered, so the app registers again when either value changes.

`settings_restore_pending`, from 1.9.4, is `true` while the record attached to the device has a settings restore for it to fetch, and `false` with `registered: false`. See [Settings Backup](#settings-backup).

The admin code and the settings backup go through WebSocket commands instead of services. See [Admin Code](#admin-code) and [Settings Backup](#settings-backup).

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
| `auto_brightness` | `on`, `off` or `null` | Auto-brightness, from 1.9.3 |
| `wake_on_approach` | `on`, `off` or `null` | Wake on approach, from 1.9.3 |
| `wake_on_approach_sensitivity` | `very_close`, `close`, `medium`, `far` or `null` | Wake on approach sensitivity, from 1.9.3 |
| `presence` | `present`, `absent` or `null` | Presence, from 1.9.3 |

`ip_address`, `wifi_ssid` and `wifi_signal` are only for device status in the app, so they have no entities. The app sends `null` for any of them it can't read. A change in `wifi_signal` alone fires no event (see [Events](#events)).

The last four are a wall panel's settings and presence sensor. Their entities are made only once the device reports a value for them. See [Wall Panel Settings and Presence](#wall-panel-settings-and-presence).

### Error Codes

| Code | Returned by | Meaning |
|---|---|---|
| `replacement_not_registered` | `replace_managed_device` | No record has the replacement attached. It hasn't finished registering. |
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
    device_model_slug: null
    device_launcher_id: null
    device_serial_number: null
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
    admin_code_status: "set"
  - managed_device_id: "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00"
    profile_id: "profileId123"
    device_id: "deviceId789"
    device_name: "Hallway"
    name_by_user: null
    device_brand: "Samsung"
    device_type: "tablet"
    device_model_name: "Galaxy Tab A9"
    device_model_slug: null
    device_launcher_id: null
    device_serial_number: null
    device_os: "android"
    device_os_version: null
    app_version: "26.9.4"
    online: false
    offline_reason: "timeout"
    last_seen: 1791759600000
    state: null
    admin_code_status: "unknown"
```

- `name_by_user` is the name the user gave the device in Home Assistant, or `null`.
- `device_os_version` is `null` when the app didn't send it.
- `device_model_slug`, from 1.9.5, is the wall panel catalogue's ID for the device's model, and `null` for any device that isn't a wall panel. See [Services](#services).
- `device_launcher_id` and `device_serial_number`, from 1.9.6, are Zendo Launcher's ID for a wall panel and the panel's hardware serial number, each `null` while the app doesn't know it. See [Services](#services).
- `offline_reason` is `timeout` or `unregistered` while the record is offline, and `null` while it's online. See [Online Status](#online-status).
- `last_seen` is in milliseconds since the Unix epoch, or `null` when the device hasn't been seen since it was restored from a version before 1.9.0.
- `state` is the last state the device sent, exactly as it sent it, or `null` when it hasn't sent one since the record was made or Home Assistant started. An offline record keeps its last state until Home Assistant restarts.
- `admin_code_status`, from 1.9.4, is `set`, `not_set` or `unknown`, with the same meaning as the `status` of `managed_device_admin_code_get`. It comes from the admin code store, and is only the status, never the code. See [Admin Code](#admin-code).

Each item is built by `_serialize_managed_device`, which also builds the data of `bngnt_managed_device_updated`.

---

## Events

The integration fires three events on the Home Assistant bus, so the app can show each device's status as it changes, and a device can tell that it has taken a record over. Their data has top-level keys.

| Event | Data | Fired when |
|---|---|---|
| `bngnt_managed_device_updated` | One `list_managed_devices` item | A record is created or changed. See below. |
| `bngnt_managed_device_removed` | `{managed_device_id}` | A record is gone. See below. |
| `bngnt_managed_device_replaced` (from 1.9.4) | `{managed_device_id, profile_id, device_id}` | A replace attached another device to a record. See below. |

`bngnt_managed_device_updated` fires when:

- a device registers, whether its record is new or not;
- a device unregisters and its record changed: it was online, or its offline reason wasn't already `unregistered`;
- the offline check marks a record offline, once for each record;
- a replacement takes over a record with `replace_managed_device`, for the target;
- a device sends a state update and it was offline, it's the record's first state, or anything other than `wifi_signal` changed. The integration compares the new state with the old one before it replaces it;
- a device's admin code report changes the record's `admin_code_status`, for example from `unknown` to `set`, or from `set` to `not_set`. A new code with the status still `set` fires nothing.

`bngnt_managed_device_removed` fires when:

- a replacement takes over a record with `replace_managed_device`, for the record it leaves behind, just before the target's `bngnt_managed_device_updated`;
- a record is deleted, with `delete_managed_device` or with the Delete button in Home Assistant.

`bngnt_managed_device_replaced` fires on each `replace_managed_device` that changes something, after the target's `bngnt_managed_device_updated`. `managed_device_id` is the record taken over, and `profile_id` + `device_id` are the replacement's, which are now attached to it. A retry, with the replacement already attached, fires none of the three.

Notes:

- **The signal drifts.** A change in `wifi_signal` alone never fires an event, because the signal changes from one heartbeat to the next. The app reads it from `list_managed_devices`.
- **Removal is its own event.** That way, `bngnt_managed_device_updated` can always be parsed as a full list item, and an older app ignores an event it doesn't know.
- **The app subscribes to `updated` and `removed` only while it shows device status.** Home Assistant sends an event type only to the WebSocket clients subscribed to it, so the integration can always fire them.
- **A managed device listens for `replaced`**, and recognises itself by the event's ids, so it doesn't wait for its next heartbeat to find out it holds another record. The record's settings restore starts from there (see [Settings Backup](#settings-backup)).
- **Keep them in sync with the Homey app.** It emits the same three events, with camelCase payloads (`managedDeviceId`, `profileId`, `deviceId`), on the same triggers.

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
| `managed_device_auto_brightness_configure` (from 1.9.3) | `mode`: `on` or `off` | `autoBrightness.set` `{mode}` |
| `managed_device_wake_on_approach_configure` (from 1.9.3) | optional `mode`: `on` or `off`, optional `sensitivity`: `very_close`, `close`, `medium` or `far`; at least one of them | `wakeOnApproach.set` `{mode?, sensitivity?}` |
| `managed_device_admin_code_configure` (from 1.9.3, admin only) | `code`, or `remove: true` | `adminCode.set` `{code}`, with `code: null` to remove it |

- A settings method gets only the fields the call has, and the device leaves the rest unchanged.
- The auto-brightness and wake on approach services skip the devices that don't support the setting. See [Changing the Settings](#changing-the-settings).
- `managed_device_admin_code_configure` handles its errors its own way: the devices that take the change keep it, and the error names the others. See [Admin Code](#admin-code).
- YAML reads an unquoted `on` or `off` as a boolean, so `mode` takes `true` and `false` as `on` and `off`.

### Errors

The call fails when a device is offline, when it doesn't answer within 10 seconds, and when it answers with a JSON-RPC error:

| Code | Meaning | Error |
|---|---|---|
| `-32601` | Method not found: the app on the device predates the command. | `Device '<name>' needs a newer version of the Zendo app` |
| `-32001` | Not supported: the device has no light sensor or no wake sensor, or it got a sensitivity and has no levels. | `Device returned error: <message>` |
| `-32602` | Invalid params. | `Device returned error: <message>` |

- From 1.9.3, Method not found reads as needing a newer app for every command, not only the new ones.
- `-32001` is rare: the services don't send a setting to a device whose last state says it doesn't support it.

---

## Wall Panel Settings and Presence

From 1.9.3, a wall panel running Zendo reports its auto-brightness and wake on approach settings and its presence sensor, and automations can change the settings. The app owns the settings: Home Assistant reads them from the state and changes them with a command, as it does the screensaver.

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
- **A value the integration doesn't know reads as unknown**, and so does `null`. The values are loose on the wire, and an enum sensor given a value outside its options would fail the state update. From 1.9.3 every enum sensor works this way, including battery state, screen, color scheme and screensaver mode.
- Their names, state labels and icons come from `strings.json` and `icons.json` through translation keys. The other managed device entities set their names and icons in code.

### Created When Reported

A record gets each of these four entities only once a state update has a value other than `null` for it. A phone or tablet without the sensor never gets an entity that is always unknown.

- An entity stays once it's made, even if the device later reports `null` or another device takes over the record. It then reads as unknown.
- The record lists them in `optional_entities`, which is persisted, so they're restored at startup with the others, unavailable until the device's first heartbeat.
- They're listed as soon as they're made, so two state updates in a row can't add one twice.
- `replace_managed_device`: the target keeps its own, and gets any the replacement's state has a value for.

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

## Admin Code

From 1.9.3. A kiosk device's admin code is 4, 6 or 8 digits, set on the device or from Home Assistant. The device tells Home Assistant its code, or that it has none, whenever it changes and every time it connects, and Home Assistant keeps the last report, so the code can be looked up even while the device is offline.

A code never goes in a record's `state`, `_serialize_managed_device` (so never in `list_managed_devices` or `bngnt_managed_device_updated`), an entity, an attribute, an event the integration fires, a log line or an error message. From 1.9.4 its status does: each list item, and so each `bngnt_managed_device_updated`, has `admin_code_status`, which reads `set`, `not_set` or `unknown` as `managed_device_admin_code_get` does.

From 1.9.4, a replace also copies the target's admin code status and code into the settings restore it leaves for the new device, which fetches it with a WebSocket command (see [Settings Backup](#settings-backup)).

### Storage

The codes are in their own store, `.storage/zendo.managed_device_admin_codes` (`Store` version 1), not in the config entry's data, which anything that reads the entry would see. The file is written with `private=True`, so only its owner can read it, and with atomic writes.

It is keyed by managed device ID:

```json
{
  "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00": { "code": "246810", "reported_at": "2026-10-14T09:30:00.123456+00:00" },
  "profileId123_deviceId456": { "code": null, "reported_at": "2026-10-14T09:31:12.000000+00:00" }
}
```

- `code` is the code, or `null` when the device reported that it has none.
- `reported_at` is when the device last reported, ISO 8601 in UTC.
- A record with no entry hasn't had a report: its code is unknown.

`async_setup_entry` loads the store before the registry starts, and drops the entries of managed device IDs that aren't in the config entry's records. A change is written straight after, with `async_delay_save`, and a pending write is flushed when the integration unloads, so that it can't land after the file is removed. Home Assistant's final write at stop saves one too.

| When | The record's entry |
|---|---|
| The device reports a code, or none | Set, with `reported_at` now. A report of the same code still moves `reported_at`. When the record's status changes, `bngnt_managed_device_updated` fires for it (see [Events](#events)). |
| `unregister_managed_device` | Kept. |
| `delete_managed_device`, or the Delete button | Removed. |
| `replace_managed_device` | The old device's code state wins: `set`, `not_set` or `unknown`. The target keeps its own entry, or stays without one, and the replacement's entry is removed, never moved to the target. The status and code also go into the settings restore (see [Settings Backup](#settings-backup)): the new device takes the code when the status is `set`, and removes its own when it's `not_set` or `unknown`. A lookup shows the target's state until the new device reports. The target's `bngnt_managed_device_updated` carries the status it has after the replace. A retry, with the replacement already attached, changes nothing. The Homey app does the same. |
| Home Assistant starts | Dropped if the record is gone. |
| The integration is removed | The file is removed, in `async_remove_entry`. |

### Reporting: `zendo/report_managed_device_admin_code`

The app reports with a WebSocket command, not a service:

```json
{"id": 42, "type": "zendo/report_managed_device_admin_code", "profile_id": "profileId123", "device_id": "deviceId456", "admin_code": "246810"}
```

The result is `{"success": true, "registered": true}`.

- `admin_code` is the code, or `null` for none.
- The record is found by its attached `profile_id` + `device_id`, as for `update_managed_device_state` (see [Finding a Record](#finding-a-record)). With none, the result is `registered: false`, nothing is kept, and the app registers again.
- The app reports when the code changes, after each registration, and before it answers `adminCode.set`. A device that has taken a record over reports once it has completed the settings restore, so its report comes after it has applied the old device's code state: the old code, or no code (see [Settings Backup](#settings-backup)).
- A report that changes the record's `admin_code_status` fires `bngnt_managed_device_updated` for it. A report of another code, or the same one, with the status still `set`, fires nothing, so the event never says that a code changed.
- It isn't admin-only, like the device-facing services.
- Extra fields are dropped, so a newer app can add one.

**Why a command.** Every service call fires a `call_service` event with its data, and the recorder keeps it. A service would record the code every time a device connects. A WebSocket command fires no event, so nothing is recorded. Home Assistant logs the WebSocket messages it receives, this one included, only with debug logging on for `homeassistant.components.websocket_api`.

Errors:

| Error code | Message | When |
|---|---|---|
| `invalid_format` | `admin_code must be exactly 4, 6 or 8 digits, or null` | `admin_code` isn't 4, 6 or 8 ASCII digits or `null`. |
| `invalid_format` | Home Assistant's schema message | `profile_id` or `device_id` is missing or empty, or `admin_code` is missing. |
| `home_assistant_error` | `Managed devices are not initialized` | The integration isn't loaded. A WebSocket command can't be unregistered, so it stays after an unload. |

The schema takes any value for `admin_code`, and the handler checks it with a fixed message. A schema error ends with the value it rejected ("Got ...") and Home Assistant's WebSocket layer logs it.

### Actions

Both are admin-only, registered with `async_register_admin_service`. A call from a user who isn't an administrator is refused with `Unauthorized`. A call with no user, such as one from an automation, is allowed.

#### `managed_device_admin_code_get`

Takes `device`, one Home Assistant device, and only responds:

```yaml
status: set
admin_code: "246810"
reported_at: "2026-10-14T09:30:00.123456+00:00"
```

| `status` | `admin_code` | `reported_at` | Meaning |
|---|---|---|---|
| `set` | The code | When the device last reported | The device has this admin code. |
| `not_set` | `null` | When the device last reported | The device reported that it has no admin code. |
| `unknown` | `null` | `null` | The device hasn't reported: its app predates admin codes, it hasn't connected since the integration was updated, or the integration was reinstalled. |

It reads only the store and sends nothing to the device, so it works while the device is offline. A device that isn't a managed device is a validation error.

#### `managed_device_admin_code_configure`

Takes `device`, one or more Home Assistant devices, and either `code` (a password text field) or `remove: true`. It sends each device's attached Zendo device `adminCode.set` with `{code}`, or `{code: null}` to remove it, through `send_command`, in parallel, each waiting up to 10 seconds.

These are validation errors, raised before anything is sent, each with a fixed message:

- both `code` and `remove: true`, or neither (`remove: false` and an empty `code` count as not given);
- a `code` that isn't text: YAML reads an unquoted `012345` as an octal number, so a number is never taken as a code, and the message says to put it in quotes;
- a `code` that isn't 4, 6 or 8 ASCII digits;
- an unknown field, or no device.

The schema takes any value for each field and the handler checks them. If a schema rejected the call, Home Assistant would log the call's data, code included, at debug level, and the error could end with the value it rejected.

**Partial success.** A device that answers success keeps the change, whatever the others do. Then the action fails with an error that names each device that didn't take it, and why:

| Why | Reason in the error |
|---|---|
| The device is offline, checked before sending | `it's offline` |
| No answer within 10 seconds | `it didn't answer within 10 seconds. Look up its admin code to see whether it changed` |
| `-32001` Not supported: in more than one property, or the device can't make the change until something is resolved on it | Setting: `it declined, for example because it's in more than one property: an admin code works only on a device that's in one property`. Removing: `it declined. Check Admin Code in the device's Settings` |
| `-32601` Method not found: the app predates the admin code | `it needs a newer version of the Zendo app` |
| `-32602` Invalid params | `it didn't accept the code` |
| Any other error code | `it answered with error <code>` |

For example: `Couldn't set the admin code on 2 of 3 devices. 'Front desk': it's offline. 'Meeting room': it needs a newer version of the Zendo app. The other device took it.` With one device: `Couldn't set the admin code on 'Front desk': it's offline.` A device is named by the name given to it in Home Assistant, or else its own name. The device's own error message is never used.

A device that answers success has already reported the new code (the app reports before it answers), so a get straight after reads it. The integration doesn't write the store itself.

The devices are chosen as for the other automation services. Home Assistant has no device group or all-devices target for them: select the devices.

### Recorder Exposure

- **Setting a code from Home Assistant records it.** It's in the `call_service` event's `service_data` and in the `bngnt_managed_device_command` event's `request`, and the recorder keeps both until it purges them. The command event also reaches every Zendo app subscribed to it, every managed device connected to this Home Assistant included, and each ignores a command for another device. The code is also in the action's data in automation and script traces, and in the log with debug logging on for `homeassistant.core`. The App Lock's PIN is exposed the same way.
- **Getting a code records only the device.** The `call_service` event has only `device`, and the response isn't in any event. An automation or script that keeps the response in a `response_variable` keeps the code in its trace, and anything it sends it to, such as a notification, has it too.
- **A device's report records nothing**, because it's a WebSocket command. With debug logging on for `homeassistant.components.websocket_api`, Home Assistant logs it, as it logs every message it receives.
- **Setting the code on the device** keeps it out of the recorder: only the report reaches Home Assistant.
- **A settings restore records nothing.** From 1.9.4, the restore a replace leaves carries the target's code, and the new device fetches it with a WebSocket command, so it's in no event. With debug logging on for `homeassistant.components.websocket_api`, Home Assistant logs the answer, as it logs every message it sends. Any Home Assistant user can fetch a pending restore: see [Who Can Read a Restore](#who-can-read-a-restore).

The action descriptions say a code set from Home Assistant is kept in recorded events and traces.

### Admin Code Compatibility

- **An older Zendo app with a 1.9.3 integration.** It never reports, so its status is `unknown`. `managed_device_admin_code_configure` sends it the command, it answers Method not found, and the error says it needs a newer version of the Zendo app.
- **A newer Zendo app with an older integration.** The command answers `unknown_command`. The app avoids it: it checks the integration's version is 1.9.3 or later before it reports, and until then it doesn't let anyone set an admin code, since Home Assistant couldn't keep a copy.
- **Downgrading below 1.9.3.** The earlier version doesn't know the store and leaves the file alone. After upgrading again, the entries of records that still exist come back, and each device reports again when it next connects.
- **Removing the integration** removes the file. After reinstalling, every device's code is `unknown` until it reports, which it does when it next connects.

---

## Settings Backup

From 1.9.4. Each kiosk device backs up its device settings to its record, and a replace hands the record's settings and admin code state to the device that takes it over. When an admin replaces the panel "Hallway" with "Hallway 2", Hallway 2 takes Hallway's record, and with it Hallway's settings and admin code.

This section covers Home Assistant's side only. For the whole flow (what the backup holds, when the app sends it, how the new device applies a restore, the edge cases and why it works this way), see Zendo's [docs/managed-devices.md, "The settings backup"](https://github.com/BetterNameX/elderflower-app/blob/master/docs/managed-devices.md#the-settings-backup).

- **The backup** is a record's latest settings, as its device last reported them. It's a JSON object owned by the app: the integration checks its size and never reads it, so a new setting needs no integration release.
- **The restore** is the snapshot a replace leaves on the target: the target's backup, admin code status and code at that moment, and the name of the device that was attached to it. The new device fetches it, applies it and completes it.

None of it goes in a record's `state`, `_serialize_managed_device` (so never in `list_managed_devices` or `bngnt_managed_device_updated`), an entity, an attribute, an event, a log line or an error message.

### Backup Storage

The backups are in their own store, `.storage/zendo.managed_device_settings_backups` (`Store` version 1), written like the admin codes' (see [Storage](#storage)): `private=True`, so only its owner can read it, with atomic writes, loaded before the registry starts, saved with `async_delay_save` and flushed when the integration unloads.

It is keyed by managed device ID. The backups here are shortened:

```json
{
  "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00": {
    "settings_backup": { "version": 1, "volume": 60 },
    "reported_at": "2026-10-17T09:30:00.123456+00:00",
    "restore": {
      "restore_id": "9b2d4c7e1a5f4e3b8c6d0a1f2e3b4c5d",
      "previous_device_name": "Hallway",
      "settings_backup": { "version": 1, "volume": 60 },
      "admin_code_status": "set",
      "admin_code": "246810",
      "created_at": "2026-10-17T09:45:00.000000+00:00"
    }
  }
}
```

- `settings_backup` is the latest backup the record's device reported, and `reported_at` when, ISO 8601 in UTC. Both are `null` when the entry is there only for its restore.
- `restore` is there while a restore is pending. Its `settings_backup` is the target's at the replace, or `null` for none. `admin_code_status` is the target's admin code status then, and `admin_code` its code when that is `set`, and `null` otherwise. `created_at` is when the replace happened.
- An entry always has a backup, a restore or both. A record with no entry has neither.
- At load, an entry whose record is gone is dropped, and so is any part of an entry that isn't valid. A restore without `admin_code_status` reads as `set` when it has a code and `unknown` when it doesn't, and is written back with it.

| When | The record's entry |
|---|---|
| The device reports a backup | `settings_backup` set, `reported_at` now. A pending restore stays. |
| `register_managed_device`, `update_managed_device_state` | Unchanged. The answer says whether a restore is pending. |
| `unregister_managed_device` | Kept. |
| `replace_managed_device` | The target gets a new restore (see below), which passes on the data of any restore still pending under a new `restore_id`, and keeps its latest backup until the new device reports its own. Every replace that changes something leaves a restore. The replacement's entry is removed. A retry, with the replacement already attached, changes nothing. |
| The device completes its restore | The restore is removed. |
| `delete_managed_device`, or the Delete button | Removed. |
| Home Assistant starts | Dropped if the record is gone. |
| The integration is removed | The file is removed, in `async_remove_entry`. |

### On a Replace

`replace_managed_device` reads the target's device name and admin code before it changes anything, and then leaves this restore on the target, when it has none pending:

| Field | Value |
|---|---|
| `restore_id` | A new uuid4 hex. |
| `previous_device_name` | The target's `device_name` before the replace: the device whose place the new one takes. |
| `settings_backup` | The target's latest backup, or `null` if its device never reported one. |
| `admin_code_status` | The target's admin code status: `set`, `not_set` when its device reported that it has none, or `unknown` when it never reported. Only `set` leaves the new device with a code: for `not_set` and `unknown` it removes its own. The admin code store keeps the same entry on the target, or none (see [Storage](#storage)). |
| `admin_code` | The code when `admin_code_status` is `set`, and `null` otherwise. |
| `created_at` | Now. |

- **Every replace that changes something leaves a restore**, with no backup and the status `unknown` too, since the new device still has to remove its own code.
- **A restore still pending is passed on.** When the target already has a restore that no device has completed, the new restore copies its `previous_device_name`, `settings_backup`, `admin_code_status` and `admin_code`, with a new `restore_id` and `created_at` now, rather than taking the target's latest backup and admin code. The original device's data stays until a device completes it, so a transfer that failed or was abandoned never loses its settings or code. For example: Hallway 2 replaces Hallway and reports its own backup and code without completing, then Hallway 3 replaces Hallway 2. Hallway 3 gets Hallway's settings and code, and the restore still names Hallway.
- **Each restore has its own `restore_id`**, so a completion meant for an older one never removes a newer one: for example, from a device that fetched the restore of the record it held before another replace moved it to this one.

### Commands

The app uses three WebSocket commands. Each finds the record by its attached `profile_id` + `device_id`, as `update_managed_device_state` does (see [Finding a Record](#finding-a-record)). With none, the answer has `registered: false` and nothing changes. None is admin-only, like the device-facing services, and extra fields are dropped, so a newer app can add one.

#### `zendo/report_managed_device_settings_backup`

```json
{"id": 43, "type": "zendo/report_managed_device_settings_backup", "profile_id": "profileId123", "device_id": "deviceId456", "settings_backup": {"version": 1, "volume": 60}}
```

The result is `{"success": true, "registered": true}`.

- `settings_backup` must be a JSON object of at most 16384 bytes, measured as its compact JSON (no spaces) in UTF-8. The Homey app and the Zendo app use the same cap, measured the same way, so keep `SETTINGS_BACKUP_MAX_BYTES` in sync in all three.
- A report changes nothing else on the record: not `online`, not `last_seen`, and it fires no event.

#### `zendo/get_managed_device_settings_restore`

```json
{"id": 44, "type": "zendo/get_managed_device_settings_restore", "profile_id": "profileId123", "device_id": "deviceId456"}
```

The result:

```json
{
  "success": true,
  "registered": true,
  "restore": {
    "restore_id": "9b2d4c7e1a5f4e3b8c6d0a1f2e3b4c5d",
    "previous_device_name": "Hallway",
    "settings_backup": {"version": 1, "volume": 60},
    "admin_code_status": "set",
    "admin_code": "246810"
  }
}
```

- `restore` is `null` when none is pending. Its `settings_backup` is `null` for none. `admin_code_status` is `set`, `not_set` or `unknown`, and `admin_code` is the code when it's `set`, and `null` otherwise. `created_at` isn't sent.
- It only reads, so fetching again gets the same restore until it's completed.
- The integration never logs it.

#### `zendo/complete_managed_device_settings_restore`

```json
{"id": 45, "type": "zendo/complete_managed_device_settings_restore", "profile_id": "profileId123", "device_id": "deviceId456", "restore_id": "9b2d4c7e1a5f4e3b8c6d0a1f2e3b4c5d"}
```

The result is `{"success": true, "registered": true, "completed": true}`.

- `completed: true`: the record's restore had this `restore_id`, and it's removed.
- `completed: false`: it didn't, and nothing changed. The restore was already completed, a second replace has passed its data on under a new `restore_id`, or there's none.

#### Errors

| Error code | Message | When |
|---|---|---|
| `invalid_format` | `settings_backup must be a JSON object of at most 16384 bytes` | The report's `settings_backup` isn't an object, or is over the cap. |
| `invalid_format` | Home Assistant's schema message | A field is missing, or `profile_id`, `device_id` or `restore_id` is empty. |
| `home_assistant_error` | `Managed devices are not initialized` | The integration isn't loaded. A WebSocket command can't be unregistered, so it stays after an unload. |

The schema takes any value for `settings_backup`, and the handler checks it with a fixed message that never has the value, as for `admin_code`: a schema error ends with the value it rejected ("Got ...") and Home Assistant's WebSocket layer logs it.

**Why commands.** Every service call fires a `call_service` event with its data, and the recorder keeps it, so a report as a service would record the backup at every change. The restore carries an admin code, and an automation or script that called a service would keep its response in its trace. A WebSocket command fires no event, and an automation can't call it. Home Assistant logs the WebSocket messages it receives and sends, these included, only with debug logging on for `homeassistant.components.websocket_api`.

#### Who Can Read a Restore

The commands aren't admin-only, since the app may sign in as a user who isn't an administrator. So any Home Assistant user who knows the ids attached to a record, which `list_managed_devices` lists, can fetch its pending restore, admin code included, until the new device completes it. `managed_device_admin_code_get`, which reads the same code, is admin-only.

### Telling the Device

- **`register_managed_device`** and **`update_managed_device_state`** answer with `settings_restore_pending`: `true` while the record attached to the device has a restore, `false` otherwise. The heartbeat carries it, so a device that missed the event, for example while it was offline, still finds out.
- **`bngnt_managed_device_replaced`** fires on each replace that changes something (see [Events](#events)), with the replacement's ids, so it can register again straight away rather than at its next heartbeat.

---

## Replacing a Device

`replace_managed_device` attaches the **replacement**, the device whose `profile_id` and `device_id` are in the call, to another record, the **target**, so the automations that control the target control the replacement instead. The call doesn't have to come from the replacement: an admin's phone can replace a wall panel. The replacement's own record is the one it **leaves behind**. It is synchronous, with no `await`, so no other service call can change the records halfway through.

For example, an admin replaces the panel "Hallway" with "Hallway 2". Hallway 2 is the replacement, Hallway's record is the target, and Hallway 2's own record is the one it leaves behind. Hallway 2 takes Hallway's record, its settings and its admin code.

1. No record has the replacement attached: `replacement_not_registered`.
2. No record has the target's managed device ID: `target_not_found`.
3. The replacement is already attached to the target: success, with no change and no events. A retry after a lost response is safe. `previous_device_name` is then the target's current name.
4. Otherwise:
   - The target's device name, admin code status and code are read first, for the settings restore, before anything changes.
   - The target takes the replacement's attached ids, device info (including `device_os_version`, `device_model_slug`, `device_launcher_id` and `device_serial_number`), app version, state, `online`, `offline_reason` and `last_seen`.
   - The target's Home Assistant device gets the replacement's name, model, serial number and app version. Its device ID, entity IDs, area and `name_by_user` stay.
   - The record the replacement leaves behind is removed. Its config entry is removed from its Home Assistant device, and Home Assistant then removes that device and its entities.
   - The target's entities take the replacement's availability and state. The target keeps its optional entities, and gets any the replacement's state has a value for (see [Created When Reported](#created-when-reported)).
   - The old device's code state wins: the target keeps its admin code entry, `set` or `not_set`, or stays `unknown` without one, and never takes the replacement's (see [Admin Code](#admin-code)).
   - The target gets a settings restore, a snapshot of its latest backup and of its admin code status and code, for the replacement to fetch. The replacement's own backup entry is removed (see [Settings Backup](#settings-backup)).
   - `bngnt_managed_device_removed` fires for the record the replacement leaves behind, then `bngnt_managed_device_updated` for the target, with the `admin_code_status` it has after the replace, then `bngnt_managed_device_replaced`.
   - The response carries the target's managed device ID and `previous_device_name`, the target's device name from before, so the app can offer to give the replacement that name.

An online target can be taken over: the app asks for a second confirmation, and the integration doesn't check. The device that was attached to the target gets `registered: false` from its next heartbeat and registers again with a new record.

---

## Deleting a Device

Only offline managed devices can be deleted. Both ways go through `BNGntManagedDeviceRegistry.delete_device`, which removes the record's admin code entry (see [Admin Code](#admin-code)) and its settings backup entry, pending restore included (see [Settings Backup](#settings-backup)), and fires `bngnt_managed_device_removed`.

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

From 1.9.3 (wall panel settings and presence):

- **An older app with a 1.9.3 integration.** Its state has none of the four keys, so its records get none of the new entities. The two new services still send it the command, it answers Method not found, and the call fails saying the device needs a newer version of the Zendo app.
- **A newer app with an older integration.** The integration stores the state as it comes, so it keeps the four keys and lists them in `list_managed_devices`, but it makes no entities for them and has neither service. Automations can't change the settings until the integration is updated.
- **Records persisted by an older integration.** They have no `optional_entities`, so they're restored without the new entities. Each is made with the device's first state update that has a value for it.
- **Downgrading below 1.9.3.** The earlier version drops `optional_entities` the next time it persists the records. After upgrading again, the entities come back, with the same entity IDs, with the device's next state update that has a value for them.

For the admin code, from 1.9.3, see [Admin Code Compatibility](#admin-code-compatibility).

From 1.9.4 (the settings backup, and the admin code on a replace):

- **An older app with a 1.9.4 integration.** It never reports a backup, so a replace's restore carries only the target's admin code status and code. It ignores `admin_code_status` in list items and events, `settings_restore_pending` and `bngnt_managed_device_replaced`, and reads the `bngnt_managed_device_updated` a status change fires as any other update. It never fetches or completes a restore, so the restore stays on the record until it's replaced again or deleted. It still reports its own admin code at every connection, which replaces the entry the target kept.
- **A newer app with an older integration.** The three commands answer `unknown_command`, the register and state update answers have no `settings_restore_pending`, list items have no `admin_code_status`, an admin code report fires no event, and nothing fires `bngnt_managed_device_replaced`. A replace moves the replacement's admin code to the target, as it did before 1.9.4, and leaves no restore. The Zendo app checks the integration's version is 1.9.4 or later before it uses them.
- **The error code.** Where 1.9.4 answers `replacement_not_registered`, earlier versions answer `caller_not_registered`. The Homey app answers the new code from its 1.9.4, and the Zendo app reads the new one.
- **Downgrading below 1.9.4.** The earlier version doesn't know the store and leaves the file alone. A replace made meanwhile moves the replacement's admin code and leaves no restore. After upgrading again, the entries of records that still exist come back, pending restores included.
- **Removing the integration** removes the file, as it does the admin codes'.

From 1.9.5 (the model slug):

- **An older app with a 1.9.5 integration.** It sends no `device_model_slug`, so its records have `null` and a wall panel lists as a tablet. It ignores the new list field.
- **A newer app with an older integration.** The integration drops the unknown `device_model_slug` field, and its list items and `bngnt_managed_device_updated` have no `device_model_slug` key, so the app must read a missing key as `null`. A wall panel shows as a tablet until the integration is updated.
- **Records persisted by an older integration.** They have no `device_model_slug`. They're restored with `null`, with no migration step, and the device's next registration sets it.
- **Downgrading below 1.9.5.** The earlier version drops `device_model_slug` the next time it persists the records. After upgrading again, each device's next registration sets it.

From 1.9.6 (the launcher ID and serial number):

- **An older app with a 1.9.6 integration.** It sends neither field, so its records have `null` for both and their Home Assistant devices have no serial number. It ignores the new list fields.
- **A newer app with an older integration.** The integration drops the unknown `device_launcher_id` and `device_serial_number` fields, and its list items and `bngnt_managed_device_updated` have neither key, so the app must read a missing key as `null`. The app doesn't check the integration's version for them: it shows each only when the record has it.
- **Records persisted by an older integration.** They have neither key. They're restored with `null`, with no migration step, and with no serial number on their Home Assistant devices. The device's next registration sets them.
- **Downgrading below 1.9.6.** The earlier version drops both the next time it persists the records. It never sets a Home Assistant device's serial number, so each keeps the one it had, even after a replace attaches another device. After upgrading again, the restore clears it, and each device's next registration sets both.
