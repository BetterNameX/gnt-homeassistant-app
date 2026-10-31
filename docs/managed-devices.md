# Managed Devices - Home Assistant Guide

> **Note:** This document is intended as context for AI-assisted development. For user-facing documentation, see https://docs.myzendo.com

A managed device is a phone, tablet or wall panel running the Zendo app with Always-On Display (Kiosk) on. It registers with Home Assistant so automations can control it: wake the screen, set the brightness or volume, lock it, and so on. Each managed device is a Home Assistant device with its own sensor entities (battery, screen, volume, locked and so on). A wall panel can also have auto-brightness, wake on approach and presence entities (see [Wall Panel Settings and Presence](#wall-panel-settings-and-presence)).

The code lives in `custom_components/zendo/managed_devices.py`, the admin code in `admin_code.py` (see [Admin Code](#admin-code)) and the settings backup in `settings_backup.py` (see [Settings Backup](#settings-backup)). The services and WebSocket commands are registered in `__init__.py`.

From 2.0.0, a Zendo device registers with Home Assistant and calls the `zendo/v2/` WebSocket commands instead, in `device_api.py`: see [Device Registration (v2)](#device-registration-v2). The v1 services and commands below stay for devices on an older app, with the rules in [Older Apps (v1)](#older-apps-v1). Listing and replacing devices, fetching and completing a settings restore, and an admin looking up a device's admin code, are `zendo/v2/` only.

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

These are the v1 services. A registered device uses their `zendo/v2/` commands instead, which take its ids from its registration (see [The v2 Commands](#the-v2-commands)). The v1 services refuse a call that names a registered or revoked device (see [Older Apps (v1)](#older-apps-v1)).

| Service | Fields | Response mode | Response |
|---|---|---|---|
| `register_managed_device` | `profile_id`, `device_id`, `device_name`, `device_brand`, `device_type`, `device_model_name`, `device_os`, optional `device_os_version`, `app_version`, optional `managed_device_id` | optional | `{success: true, managed_device_id}` |
| `unregister_managed_device` | `profile_id`, `device_id` | optional | `{success: true, registered}` |
| `update_managed_device_state` | `profile_id`, `device_id`, `state` | optional | `{success: true, registered}` |

From 2.0.0 there's no v1 `list_managed_devices`, `replace_managed_device` or `delete_managed_device` service: listing, replacing and deleting are `zendo/v2/list_managed_devices`, `zendo/v2/replace_managed_device` and `zendo/v2/delete_managed_device` only (see [The v2 Commands](#the-v2-commands)).

`device_os_version` is the version of the device's operating system (for example `18.5`), or `null`. An app that doesn't send it leaves it `null`.

The admin code and the settings backup go through WebSocket commands instead of services. See [Admin Code](#admin-code) and [Settings Backup](#settings-backup).

`unregister_managed_device` keeps the record, its Home Assistant device and its entities, and marks them unavailable with the offline reason `unregistered`. Turning off Always-On Display or signing out of the app does this.

### A Wall Panel's Fields

`zendo/v2/register_managed_device` also takes `device_model_slug`, `device_launcher_id` and `device_serial_number`, each optional. From 2.0.0 the v1 `register_managed_device` doesn't: an older app that still sends them registers as before, without them, and the record keeps the values it has. A record that a v1 registration makes has `null` for all three.

`device_model_slug`, from 1.9.5, is the ID of the device's model in the Zendo app's wall panel catalogue, or `null`. Only a wall panel has one: phones, tablets and any device on iOS never send it. A wall panel reports the same `device_type` as a tablet, so the slug is what tells them apart, and the app shows a wall panel icon for a record that has one.

- The integration stores it as it comes and doesn't check its form: the app's catalogue owns the slugs.
- No field, `null`, an empty string or a value that isn't a string all read as `null`, never as an error.
- Every v2 registration sets it, like the rest of the device info, so a device that stops sending it ends with `null`.
- It isn't part of the Home Assistant device's model, which stays the brand and model name.

`device_launcher_id` and `device_serial_number`, from 1.9.6, are what Zendo Launcher on a wall panel tells the app, or `null`. The launcher ID is Zendo Launcher's own ID for the panel (for example `K7QM3T`), which the app shows as Launcher ID. It's unrelated to `device_id` and to the managed device ID. The serial number is the panel's hardware serial, which Zendo Launcher reports once Zendo Provisioning has given it one. The app sends each only when it knows it: phones, tablets and any device without Zendo Launcher send neither, and a wall panel whose launcher has no serial number yet sends only the launcher ID.

- The integration stores them as they come and doesn't check their form: Zendo Launcher owns both.
- No field, `null`, an empty string or a value that isn't a string all read as `null`, never as an error.
- Every v2 registration sets them, like the rest of the device info, so a device that stops sending one ends with `null`.
- `device_serial_number` is also the Home Assistant device's serial number, set and cleared with it. The launcher ID is in none of the Home Assistant device's fields.
- The launcher usually answers the app after the app has registered, so the app registers again when either value changes.

### Settings Restore Pending

`settings_restore_pending`, from 1.9.4, is `true` while the record attached to the device has a settings restore for it to fetch, and `false` with `registered: false`. From 2.0.0 only `zendo/v2/register_managed_device` and `zendo/v2/update_managed_device_state` answer with it. The v1 `register_managed_device` and `update_managed_device_state` answer without it, which the app reads as no restore pending. See [Settings Backup](#settings-backup).

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
| `replacement_not_registered` | `zendo/v2/replace_managed_device` | No record has the replacement attached. It hasn't finished registering. |
| `target_not_found` | `zendo/v2/replace_managed_device`, `zendo/v2/get_managed_device_admin_code`, `zendo/v2/configure_managed_device_admin_code` | No record has that managed device ID. |
| `target_online` | `zendo/v2/delete_managed_device` | The record's device is online, so it can't be deleted. |
| `target_offline` (from 2.0.0) | `zendo/v2/configure_managed_device_admin_code` | The record's device is offline, so nothing was sent. |
| `no_answer` (from 2.0.0) | `zendo/v2/configure_managed_device_admin_code` | The device didn't answer within 10 seconds, the command couldn't be sent, or the integration unloaded before the answer. It may still have made the change. |
| `app_update_required` (from 2.0.0) | `zendo/v2/configure_managed_device_admin_code` | The device hasn't registered, so nothing was sent (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)), or it answered `-32601` Method not found. Either way, its app predates the command. |
| `not_supported` (from 2.0.0) | `zendo/v2/configure_managed_device_admin_code` | The device answered `-32001` Not supported: it can't make the change, for example because it's in more than one property. |
| `command_failed` (from 2.0.0) | `zendo/v2/configure_managed_device_admin_code` | The device answered with any other JSON-RPC error, `-32602` Invalid params included. |

The same strings are used by the Homey app, and the Zendo app narrows them at its boundary. Keep the three in sync.

### The List: `zendo/v2/list_managed_devices`

From 2.0.0 the list is `zendo/v2/list_managed_devices` only. Its answer, without `success: true`:

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
- `device_model_slug`, from 1.9.5, is the wall panel catalogue's ID for the device's model, and `null` for any device that isn't a wall panel. See [A Wall Panel's Fields](#a-wall-panels-fields).
- `device_launcher_id` and `device_serial_number`, from 1.9.6, are Zendo Launcher's ID for a wall panel and the panel's hardware serial number, each `null` while the app doesn't know it. See [A Wall Panel's Fields](#a-wall-panels-fields).
- `offline_reason` is `timeout` or `unregistered` while the record is offline, and `null` while it's online. See [Online Status](#online-status).
- `last_seen` is in milliseconds since the Unix epoch, or `null` when the device hasn't been seen since it was restored from a version before 1.9.0.
- `state` is the last state the device sent, exactly as it sent it, or `null` when it hasn't sent one since the record was made or Home Assistant started. An offline record keeps its last state until Home Assistant restarts.
- `admin_code_status`, from 1.9.4, is `set`, `not_set` or `unknown`, with the same meaning as the `status` of `zendo/v2/get_managed_device_admin_code`. It comes from the admin code store, and is only the status, never the code, so a record whose attached device hasn't registered can list `set` or `not_set` while the lookup answers `unknown`. See [Admin Code](#admin-code).

Each item is built by `_serialize_managed_device`, which also builds the data of `bngnt_managed_device_updated`.

---

## Events

The integration fires three events on the Home Assistant bus, so the app can show each device's status as it changes, and a device can tell that it has taken a record over. Their data has top-level keys.

| Event | Data | Fired when |
|---|---|---|
| `bngnt_managed_device_updated` | One `zendo/v2/list_managed_devices` item | A record is created or changed. See below. |
| `bngnt_managed_device_removed` | `{managed_device_id}` | A record is gone. See below. |
| `bngnt_managed_device_replaced` (from 1.9.4) | `{managed_device_id, profile_id, device_id}` | A replace attached another device to a record. See below. |

`bngnt_managed_device_updated` fires when:

- a device registers, whether its record is new or not;
- a device unregisters and its record changed: it was online, or its offline reason wasn't already `unregistered`;
- the offline check marks a record offline, once for each record;
- a replacement takes over a record with `zendo/v2/replace_managed_device`, for the target;
- a device sends a state update and it was offline, it's the record's first state, or anything other than `wifi_signal` changed. The integration compares the new state with the old one before it replaces it;
- a device's admin code report changes the record's `admin_code_status`, for example from `unknown` to `set`, or from `set` to `not_set`. A new code with the status still `set` fires nothing.

`bngnt_managed_device_removed` fires when:

- a replacement takes over a record with `zendo/v2/replace_managed_device`, for the record it leaves behind, just before the target's `bngnt_managed_device_updated`;
- a record is deleted, with `zendo/v2/delete_managed_device` or the Delete button in Home Assistant.

`bngnt_managed_device_replaced` fires on each replace that changes something, after the target's `bngnt_managed_device_updated`. `managed_device_id` is the record taken over, and `profile_id` + `device_id` are the replacement's, which are now attached to it. A retry, with the replacement already attached, fires none of the three.

Notes:

- **The signal drifts.** A change in `wifi_signal` alone never fires an event, because the signal changes from one heartbeat to the next. The app reads it from `zendo/v2/list_managed_devices`.
- **Removal is its own event.** That way, `bngnt_managed_device_updated` can always be parsed as a full list item, and an older app ignores an event it doesn't know.
- **The app subscribes to `updated` and `removed` only while it shows device status.** Home Assistant sends an event type only to the WebSocket clients subscribed to it, so the integration can always fire them.
- **A managed device listens for `replaced`**, and recognises itself by the event's ids, so it doesn't wait for its next heartbeat to find out it holds another record. The record's settings restore starts from there (see [Settings Backup](#settings-backup)).
- **Keep them in sync with the Homey app.** It emits the same three events, with camelCase payloads (`managedDeviceId`, `profileId`, `deviceId`), on the same triggers.
- **A registered device gets them through a subscription.** The three still fire on the bus, and `zendo/v2/subscribe_managed_device_changes` forwards them, as `zendo/v2/subscribe_events` does `bngnt_managed_device_replaced` (see [Subscriptions](#subscriptions)).

---

## Automation Services

These services are for automations to control a device. Each takes `device`, one or more Home Assistant devices, and sends each one's attached Zendo device a JSON-RPC 2.0 command, and waits up to 10 seconds for the answer. The commands to several devices go out in parallel.

- **To a registered device**, the command goes in an envelope through the `zendo/v2/subscribe_events` subscriptions, encrypted for `adminCode.set` and `app.lock`, and the device answers with `zendo/v2/managed_device_command_response` (see [Command Envelopes](#command-envelopes)).
- **To any other**, it's a `bngnt_managed_device_command` event, and the app answers with `managed_device_command_response`. From 2.0.0 such a device is never sent `adminCode.set` or `app.lock` (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).
- Either way, an answer counts only from the device the command went to.

| Service | Fields | JSON-RPC method and params |
|---|---|---|
| `managed_device_screensaver_configure` | optional `mode`, `timer` | `screensaver.set` `{mode?, timer?}` |
| `managed_device_screen_configure` | optional `brightness`, `color_scheme` | `screen.set` `{brightness?, colorScheme?}` |
| `managed_device_screen_wake_up` | none | `screen.wakeUp` |
| `managed_device_audio_configure` | optional `volume` | `audio.set` `{volume?}` |
| `managed_device_app_reload` (admin only) | none | `app.reload` |
| `managed_device_app_lock` (admin only) | `pin`, optional `message` | `app.lock` `{pin, message?}` |
| `managed_device_app_unlock` (admin only) | none | `app.unlock` |
| `managed_device_auto_brightness_configure` (from 1.9.3) | `mode`: `on` or `off` | `autoBrightness.set` `{mode}` |
| `managed_device_wake_on_approach_configure` (from 1.9.3) | optional `mode`: `on` or `off`, optional `sensitivity`: `very_close`, `close`, `medium` or `far`; at least one of them | `wakeOnApproach.set` `{mode?, sensitivity?}` |
| `managed_device_admin_code_configure` (from 1.9.3, admin only) | `code`, or `remove: true` | `adminCode.set` `{code}`, with `code: null` to remove it |

- A settings method gets only the fields the call has, and the device leaves the rest unchanged.
- The auto-brightness and wake on approach services skip the devices that don't support the setting. See [Changing the Settings](#changing-the-settings).
- The admin-only services are registered with `async_register_admin_service`: a call from a user who isn't an administrator is refused with `Unauthorized`, and a call with no user, such as one from an automation, is allowed.
- `managed_device_admin_code_configure` handles its errors its own way: the devices that take the change keep it, and the error names the others. See [Admin Code](#admin-code).
- YAML reads an unquoted `on` or `off` as a boolean, so `mode` takes `true` and `false` as `on` and `off`.

### Errors

The call fails when a device is offline, when it doesn't answer within 10 seconds or before the integration unloads, and when it answers with a JSON-RPC error:

| Code | Meaning | Error |
|---|---|---|
| `-32601` | Method not found: the app on the device predates the command. | `Device '<name>' needs a newer version of the Zendo app` |
| `-32001` | Not supported: the device has no light sensor or no wake sensor, or it got a sensitivity and has no levels. | `Device returned error: <message>` |
| `-32602` | Invalid params. | `Device returned error: <message>` |

- From 1.9.3, Method not found reads as needing a newer app for every command, not only the new ones.
- `-32001` is rare: the services don't send a setting to a device whose last state says it doesn't support it.
- From 2.0.0, `managed_device_app_lock` and `managed_device_admin_code_configure` also fail with a device's newer app error for a device that hasn't registered, which is sent nothing. On a Home Assistant that isn't linked to a site, they fail with one error saying so instead (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

**`managed_device_app_lock` locks the devices it can.** From 2.0.0 it sends the PIN to every device that can take it, whatever the others do, and waits for all of them. Then:

- With one device, the error is that device's own, as in the table.
- With several, the error names each device that wasn't locked, and why, with each device's own error. For example: `Couldn't lock 2 of 3 devices. Device 'Hallway' needs a newer version of the Zendo app. Device 'Front door' is offline. The other device was locked.` A device that answered with a JSON-RPC error reads `Device '<name>' returned error: <message>`. Each device is named by its own name, as in its own error.
- On a Home Assistant that isn't linked to a site, nothing is sent, and the error says that once, naming no device (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

The other automation services fail with the first error a device gives, and `managed_device_admin_code_configure` has its own errors (see [Admin Code](#admin-code)).

### A PIN or an Admin Code Needs a Current App

From 2.0.0, `adminCode.set` and `app.lock` go only to a device that has registered with `zendo/v2/register_device` (see [Device Registration (v2)](#device-registration-v2)), in an encrypted envelope (see [Command Envelopes](#command-envelopes)). A device that hasn't registered is sent neither, not even as a `bngnt_managed_device_command` event: a PIN or an admin code reaches only a device on a current Zendo app. On a Home Assistant linked to a site, a device that hasn't registered is on an older Zendo app.

| Caller | For a device that hasn't registered |
|---|---|
| `managed_device_app_lock` | It isn't locked, and the error says `Device '<name>' needs a newer version of the Zendo app`. The other devices are still locked (see [Errors](#errors)). |
| `managed_device_admin_code_configure` | It doesn't take the change, with the reason `it needs a newer version of the Zendo app`. The other devices still take it (see [`managed_device_admin_code_configure`](#managed_device_admin_code_configure)). |
| `zendo/v2/configure_managed_device_admin_code` | `{"success": false, "error": "app_update_required"}` (see [`zendo/v2/configure_managed_device_admin_code`](#zendov2configure_managed_device_admin_code)). |

**On a Home Assistant that isn't linked to a site**, no device can register, whichever app it's on, so updating the app wouldn't help. `managed_device_app_lock` and `managed_device_admin_code_configure` send nothing, and fail with one error, once, however many devices the call names:

```
Zendo isn't set up on Home Assistant yet. An admin needs to open the Zendo app, go to Settings, then Property, then Zendo on Home Assistant, and tap Set Up.
```

`zendo/v2/configure_managed_device_admin_code` never gets this far: an admin call answers `not_linked` first (see [Linking to a Site](#linking-to-a-site)).

- **One rule, in `send_command`.** Before it sends anything, it asks `command_refusal` in `device_auth.py`, so every caller gets the same answer. That checks whether the method can go to the device, with `may_send_command`, and then the link and whether the device is online. The methods are `MUST_ENCRYPT_METHODS`. A refusal raises `BNGntManagedDeviceAppUpdateRequiredError`, the same error Method not found raises, or `BNGntManagedDeviceNotLinkedError` on a Home Assistant that isn't linked.
- **An offline device fails as offline**, whichever app it's on. The exception is a method it can't get on a Home Assistant that isn't linked: that error comes first, since bringing the device online wouldn't help.
- **Every other command is unchanged** for a device that hasn't registered: `app.unlock`, `app.reload`, the screen, audio, screensaver, wake up, auto-brightness and wake on approach commands still reach it as a `bngnt_managed_device_command` event.
- **The device's reports still count.** A device that hasn't registered still reports its admin code with `zendo/report_managed_device_admin_code`, so the store keeps it and its `admin_code_status` in the list reads as before. `zendo/v2/get_managed_device_admin_code` answers `unknown` for it until it registers (see [`zendo/v2/get_managed_device_admin_code`](#zendov2get_managed_device_admin_code)).

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
- On a replace, the target keeps its own, and gets any the replacement's state has a value for.

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

From 1.9.3. A kiosk device's admin code is 4, 6 or 8 digits, set on the device, from Home Assistant, or from 2.0.0 by an admin in the Zendo app. The device tells Home Assistant its code, or that it has none, whenever it changes and every time it connects, and Home Assistant keeps the last report, so the code can be looked up even while the device is offline.

A code never goes in a record's `state`, `_serialize_managed_device` (so never in `zendo/v2/list_managed_devices` or `bngnt_managed_device_updated`), an entity, an attribute, an event the integration fires, a log line or an error message. From 1.9.4 its status does: each list item, and so each `bngnt_managed_device_updated`, has `admin_code_status`, which reads `set`, `not_set` or `unknown` from the store. From 2.0.0 an admin's device can get the code itself, sealed to its own key (see [Admin Commands (v2)](#admin-commands-v2)).

From 1.9.4, a replace also copies the target's admin code status and code into the settings restore it leaves for the new device, which fetches it with `zendo/v2/get_managed_device_settings_restore` (see [Settings Backup](#settings-backup)).

### Storage

The codes are in their own store, `.storage/zendo.managed_device_admin_codes` (`Store` version 1), not in the config entry's data. The file is written with `private=True`, so only its owner can read it, and with atomic writes.

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

`async_setup_entry` loads the store before the registry starts, and drops the entries of managed device IDs that aren't in the config entry's records. A change is written straight after, with `async_delay_save`, and a pending write is flushed when the integration unloads, so that it can't land after the file is removed. Home Assistant's final write at stop saves one too. Once unloaded, the store takes no more writes, so a call still under way then can't write the file again after it's removed, or over the one the next setup loads. The registrations', the settings backups' and the site config encryption key's stores work the same way.

| When | The record's entry |
|---|---|
| The device reports a code, or none | Set, with `reported_at` now. A report of the same code still moves `reported_at`. When the record's status changes, `bngnt_managed_device_updated` fires for it (see [Events](#events)). |
| `unregister_managed_device` | Kept. |
| `zendo/v2/delete_managed_device`, or the Delete button | Removed. |
| A replace | The old device's code state wins: `set`, `not_set` or `unknown`. The target keeps its own entry, or stays without one, and the replacement's entry is removed, never moved to the target. The status and code also go into the settings restore (see [Settings Backup](#settings-backup)): the new device takes the code when the status is `set`, and removes its own when it's `not_set` or `unknown`. A lookup shows the target's state until the new device reports, or `unknown` while the new device hasn't registered. The target's `bngnt_managed_device_updated` carries the status it has after the replace. A retry, with the replacement already attached, changes nothing. The Homey app does the same. |
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

**It stays a WebSocket command, never a service:** a report fires no `call_service` event.

Errors:

| Error code | Message | When |
|---|---|---|
| `invalid_format` | `admin_code must be exactly 4, 6 or 8 digits, or null` | `admin_code` isn't 4, 6 or 8 ASCII digits or `null`. |
| `invalid_format` | Home Assistant's schema message | `profile_id` or `device_id` is missing or empty, or `admin_code` is missing. |
| `home_assistant_error` | `Managed devices are not initialized` | The integration isn't loaded. A WebSocket command can't be unregistered, so it stays after an unload. |

The schema takes any value for `admin_code`, and the handler checks it with a fixed message. It's never logged.

### Action

`managed_device_admin_code_configure` is admin-only, registered with `async_register_admin_service`. A call from a user who isn't an administrator is refused with `Unauthorized`. A call with no user, such as one from an automation, is allowed.

From 2.0.0 there's no `managed_device_admin_code_get` action: an admin looks a device's admin code up in the Zendo app, which calls `zendo/v2/get_managed_device_admin_code` (see [Admin Commands (v2)](#admin-commands-v2) and [Older Apps (v1)](#older-apps-v1)).

#### `managed_device_admin_code_configure`

Takes `device`, one or more Home Assistant devices, and either `code` (a password text field) or `remove: true`. It sends each device's attached Zendo device `adminCode.set` with `{code}`, or `{code: null}` to remove it, through `send_command`, in parallel, each waiting up to 10 seconds. From 2.0.0 a device that hasn't registered is sent nothing (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

These are validation errors, raised before anything is sent, each with a fixed message:

- both `code` and `remove: true`, or neither (`remove: false` and an empty `code` count as not given);
- a `code` that isn't text: YAML reads an unquoted `012345` as an octal number, so a number is never taken as a code, and the message says to put it in quotes;
- a `code` that isn't 4, 6 or 8 ASCII digits;
- an unknown field, or no device.

The schema takes any value for each field, and the handler checks them.

**Partial success.** A device that answers success keeps the change, whatever the others do. Then the action fails with an error that names each device that didn't take it, and why:

| Why | Reason in the error |
|---|---|
| The device is offline, checked before sending | `it's offline` |
| No answer within 10 seconds | `it didn't answer within 10 seconds. Check its admin code in the Zendo app to see whether it changed` |
| The integration unloaded before the answer | `Zendo stopped on Home Assistant before the device answered` |
| `-32001` Not supported: in more than one property, or the device can't make the change until something is resolved on it | Setting: `it declined, for example because it's in more than one property: an admin code works only on a device that's in one property`. Removing: `it declined. Check Admin Code in the device's Settings` |
| `-32601` Method not found: the app predates the admin code | `it needs a newer version of the Zendo app` |
| From 2.0.0, on a Home Assistant linked to a site, the device hasn't registered: it's on an older Zendo app, and is sent nothing | `it needs a newer version of the Zendo app` |
| `-32602` Invalid params | `it didn't accept the code` |
| Any other error code | `it answered with error <code>` |

For example: `Couldn't set the admin code on 2 of 3 devices. 'Front desk': it's offline. 'Meeting room': it needs a newer version of the Zendo app. The other device took it.` With one device: `Couldn't set the admin code on 'Front desk': it's offline.` A device is named by the name given to it in Home Assistant, or else its own name. The device's own error message is never used.

On a Home Assistant that isn't linked to a site, nothing is sent, and the error says that once, naming no device (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

A device that answers success has already reported the new code (the app reports before it answers), so `zendo/v2/get_managed_device_admin_code` straight after reads it. The integration doesn't write the store itself.

The devices are chosen as for the other automation services. Home Assistant has no device group or all-devices target for them: select the devices.

### Admin Commands (v2)

From 2.0.0. The Zendo app lets any admin view, change or turn off a managed device's admin code with two WebSocket commands. Each takes an admin-scoped token, as `device_control_backend_token`, and the record's `managed_device_id`, and checks the token as the other admin commands do, answering `not_linked`, `invalid_token`, `wrong_site`, `not_admin`, `device_revoked` or `unknown_api_key` (see [The v2 Commands](#the-v2-commands)). Neither is a service, so neither fires a `call_service` event, and the integration never logs a code.

#### `zendo/v2/get_managed_device_admin_code`

```json
{"id": 46, "type": "zendo/v2/get_managed_device_admin_code", "device_control_backend_token": "<device control backend token>", "managed_device_id": "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00"}
```

The result:

```json
{"success": true, "status": "set", "reported_at": "2026-10-14T09:30:00.123456+00:00", "sealed": {"scheme": "ecies-p256", "data": "<base64>"}}
```

| `status` | `reported_at` | `sealed` opens to | Meaning |
|---|---|---|---|
| `set` | When the device last reported | `{"admin_code": "<the code>"}` | The device has this admin code. |
| `not_set` | When the device last reported | `{"admin_code": null}` | The device reported that it has no admin code. |
| `unknown` | `null` | `{"admin_code": null}` | The device hasn't reported: its app predates admin codes, it hasn't connected since the integration was updated, or the integration was reinstalled. Or the record's attached device hasn't registered, whatever it reported. |

- **It reads only the store** and sends nothing to the device, so it works while the device is offline. `status` reads as the record's `admin_code_status` in the list does, except for a device that hasn't registered.
- **A device that hasn't registered reads as `unknown`.** For a record whose attached device has no registration with this integration, a revoked device included, the answer has `status: "unknown"` and `reported_at: null`, and `sealed` opens to `{"admin_code": null}`, whatever the store holds: the integration only hands out codes for devices registered with it. It's the attached device that counts, not the caller. The store doesn't change, so the entry reads again once the device registers, and `admin_code_status` in the list still comes from the store. The rule is `admin_code_lookup_for_attached_device` in `device_auth.py`.
- **The code is never in clear**, not even `null`: every answer has a `sealed` object instead of `admin_code`. It's sealed to the encryption public key in the caller's token, its `encryptionPublicKey` claim, and its additional data is `sealed`, the caller's `profileId`, its `deviceId` and `adminCode.get`, joined by newlines, so the caller's own device opens it (see [Sealed Objects](#sealed-objects)).
- **`public_key_required`** answers a token without an encryption public key, or with one that isn't a P-256 key. It's checked after the token and before the fields.
- **`target_not_found`** answers a `managed_device_id` that no record has.
- A `managed_device_id` that's missing, empty or not a string fails with `invalid_format` and `Invalid managed_device_id`.

#### `zendo/v2/configure_managed_device_admin_code`

```json
{"id": 47, "type": "zendo/v2/configure_managed_device_admin_code", "device_control_backend_token": "<device control backend token>", "managed_device_id": "md_3f2a9c0e8b7d4c1e9a6f5b4d3c2e1f00", "code": "246810"}
```

or, to turn the admin code off, `"remove": true` in place of `code`. The result is `{"success": true}`.

- **It sends the record's device `adminCode.set`**, with `{code}`, or `{code: null}` to remove it, and waits up to 10 seconds for the answer, as `managed_device_admin_code_configure` does for each device, with the same code in `admin_code.py` (`async_send_admin_code`). It goes only to a registered device, in an encrypted envelope (see [Command Envelopes](#command-envelopes)). A device that hasn't registered is sent nothing, and the answer is `app_update_required` (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).
- **A device that answers success** has already reported the new code, so a get straight after reads it. The integration doesn't write the store itself.
- **Otherwise it answers `{"success": false, "error": <code>}`**, with the codes the Homey app answers too:

| Error code | When | The action's reason for the same outcome |
|---|---|---|
| `target_not_found` | No record has that managed device ID. | `it's no longer a managed device` |
| `target_offline` | The record's device is offline, checked before sending. | `it's offline` |
| `no_answer` | No answer within 10 seconds, the command couldn't be sent, or the integration unloaded before the answer. The device may still have made the change: get the code to see. | `it didn't answer within 10 seconds. ...` |
| `app_update_required` | The device hasn't registered: it's on an older Zendo app, and nothing was sent. Or it answered `-32601` Method not found: the app on the device predates the admin code. | `it needs a newer version of the Zendo app` |
| `not_supported` | `-32001` Not supported: the device is in more than one property, or can't make the change until something is resolved on it. | `it declined ...` |
| `command_failed` | Any other JSON-RPC error, `-32602` Invalid params included. | `it didn't accept the code`, or `it answered with error <code>` |

Its fields fail with `invalid_format` and a fixed message that never has the code:

| Message | When |
|---|---|
| `Invalid managed_device_id` | `managed_device_id` is missing, empty or not a string. |
| `Invalid remove` | `remove` isn't `true`, `false` or `null`. |
| `Give either code or remove: true` | Both `code` and `remove: true`, or neither. A `null` `code` and `remove: false` count as not given. |
| `code must be exactly 4, 6 or 8 digits` | `code` isn't a string of 4, 6 or 8 ASCII digits. |

The schema takes any value for `code`, and the handler checks it with a fixed message, as for `admin_code` in `zendo/report_managed_device_admin_code`.

### Admin Code Compatibility

- **An older Zendo app with a 1.9.3 integration.** It never reports, so its status is `unknown`. `managed_device_admin_code_configure` sends it the command, it answers Method not found, and the error says it needs a newer version of the Zendo app.
- **A device that hasn't registered, with a 2.0.0 integration.** It's never sent `adminCode.set`: `managed_device_admin_code_configure` says it needs a newer version of the Zendo app, or that Zendo isn't set up on Home Assistant until it's linked to a site, and `zendo/v2/configure_managed_device_admin_code` answers `app_update_required`. It still reports its code, so its status in the list reads as before, and `zendo/v2/get_managed_device_admin_code` answers `unknown` for it until it registers (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).
- **A newer Zendo app with an older integration.** The command answers `unknown_command`. The app avoids it: it checks the integration's version is 1.9.3 or later before it reports, and until then it doesn't let anyone set an admin code, since Home Assistant couldn't keep a copy.
- **Downgrading below 1.9.3.** The earlier version doesn't know the store and leaves the file alone. After upgrading again, the entries of records that still exist come back, and each device reports again when it next connects.
- **Removing the integration** removes the file. After reinstalling, every device's code is `unknown` until it reports, which it does when it next connects.

---

## Settings Backup

From 1.9.4. Each kiosk device backs up its device settings to its record, and a replace hands the record's settings and admin code state to the device that takes it over. When an admin replaces the panel "Hallway" with "Hallway 2", Hallway 2 takes Hallway's record, and with it Hallway's settings and admin code.

This section covers Home Assistant's side only. For the whole flow (what the backup holds, when the app sends it, how the new device applies a restore, the edge cases and why it works this way), see Zendo's [docs/managed-devices.md, "The settings backup"](https://github.com/BetterNameX/elderflower-app/blob/master/docs/managed-devices.md#the-settings-backup).

- **The backup** is a record's latest settings, as its device last reported them. It's a JSON object owned by the app: the integration checks its size and never reads it, so a new setting needs no integration release.
- **The restore** is the snapshot a replace leaves on the target: the target's backup, admin code status and code at that moment, and the name of the device that was attached to it. The new device fetches it, applies it and completes it.

None of it goes in a record's `state`, `_serialize_managed_device` (so never in `zendo/v2/list_managed_devices` or `bngnt_managed_device_updated`), an entity, an attribute, an event, a log line or an error message.

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
| `register_managed_device`, `update_managed_device_state` | Unchanged. The v2 answer says whether a restore is pending. |
| `unregister_managed_device` | Kept. |
| A replace | The target gets a new restore (see below), which passes on the data of any restore still pending under a new `restore_id`, and keeps its latest backup until the new device reports its own. Every replace that changes something leaves a restore. The replacement's entry is removed. A retry, with the replacement already attached, changes nothing. |
| The device completes its restore | The restore is removed. |
| `zendo/v2/delete_managed_device`, or the Delete button | Removed. |
| Home Assistant starts | Dropped if the record is gone. |
| The integration is removed | The file is removed, in `async_remove_entry`. |

### On a Replace

A replace reads the target's device name and admin code before it changes anything, and then leaves this restore on the target, when it has none pending:

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

The app uses three WebSocket commands: it reports a backup, then the device that takes a record over fetches the restore and completes it. Each finds the record by the device's attached `profile_id` + `device_id`, as `update_managed_device_state` does (see [Finding a Record](#finding-a-record)). With none, the answer has `registered: false` and nothing changes. None is admin-only, like the device-facing services, and extra fields are dropped, so a newer app can add one.

| Command | From 2.0.0 |
|---|---|
| Report a backup | `zendo/v2/report_managed_device_settings_backup`, and the v1 `zendo/report_managed_device_settings_backup`, which phones and tablets on a released app still send |
| Fetch the restore | `zendo/v2/get_managed_device_settings_restore` only |
| Complete the restore | `zendo/v2/complete_managed_device_settings_restore` only |

The v2 commands take the device's `api_key` in place of `profile_id` and `device_id` (see [The v2 Commands](#the-v2-commands)).

#### Reporting a Backup

```json
{"id": 43, "type": "zendo/report_managed_device_settings_backup", "profile_id": "profileId123", "device_id": "deviceId456", "settings_backup": {"version": 1, "volume": 60}}
```

or, from a registered device:

```json
{"id": 43, "type": "zendo/v2/report_managed_device_settings_backup", "api_key": "<API key>", "settings_backup": {"version": 1, "volume": 60}}
```

The result is `{"success": true, "registered": true}`.

- `settings_backup` must be a JSON object of at most 16384 bytes, measured as its compact JSON (no spaces) in UTF-8. The Homey app and the Zendo app use the same cap, measured the same way, so keep `SETTINGS_BACKUP_MAX_BYTES` in sync in all three.
- A report changes nothing else on the record: not `online`, not `last_seen`, and it fires no event.

#### `zendo/v2/get_managed_device_settings_restore`

```json
{"id": 44, "type": "zendo/v2/get_managed_device_settings_restore", "api_key": "<API key>"}
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
    "sealed": {"scheme": "ecies-p256", "data": "<base64>"}
  }
}
```

- `restore` is `null` when none is pending. Its `settings_backup` is `null` for none. `admin_code_status` is `set`, `not_set` or `unknown`. `created_at` isn't sent.
- `sealed` opens to `{"admin_code": ...}`: the code when `admin_code_status` is `set`, and `null` otherwise (see [Sealed Objects](#sealed-objects)).
- It only reads, so fetching again gets the same restore until it's completed.
- The integration never logs it.

#### `zendo/v2/complete_managed_device_settings_restore`

```json
{"id": 45, "type": "zendo/v2/complete_managed_device_settings_restore", "api_key": "<API key>", "restore_id": "9b2d4c7e1a5f4e3b8c6d0a1f2e3b4c5d"}
```

The result is `{"success": true, "registered": true, "completed": true}`.

- `completed: true`: the record's restore had this `restore_id`, and it's removed.
- `completed: false`: it didn't, and nothing changed. The restore was already completed, a second replace has passed its data on under a new `restore_id`, or there's none.

#### Errors

| Error code | Message | When |
|---|---|---|
| `invalid_format` | `settings_backup must be a JSON object of at most 16384 bytes` | The report's `settings_backup` isn't an object, or is over the cap. |
| `invalid_format` | Home Assistant's schema message | The v1 report: a field is missing, or `profile_id` or `device_id` is empty. |
| `invalid_format` | `Invalid restore_id` | The v2 completion: `restore_id` is missing, empty or not a string. |
| `not_set_up` | `Zendo isn't set up` | The integration isn't loaded, for the v2 commands. |
| `home_assistant_error` | `Managed devices are not initialized` | The integration isn't loaded. A WebSocket command can't be unregistered, so it stays after an unload. |

A v2 command answers `{"success": false, "error": "unknown_api_key"}` for an API key that isn't one (see [The v2 Commands](#the-v2-commands)).

The schema takes any value for `settings_backup`, and the handler checks it with a fixed message that never has the value, as for `admin_code`.

**They stay WebSocket commands, never services:** a report fires no `call_service` event, and an automation can't call a command.

### Telling the Device

- **`zendo/v2/register_managed_device`** and **`zendo/v2/update_managed_device_state`** answer with `settings_restore_pending`: `true` while the record attached to the device has a restore, `false` otherwise. The heartbeat carries it, so a device that missed the event, for example while it was offline, still finds out. From 2.0.0 their v1 services answer without it (see [Settings Restore Pending](#settings-restore-pending)).
- **`bngnt_managed_device_replaced`** fires on each replace that changes something (see [Events](#events)), with the replacement's ids, so it can register again straight away rather than at its next heartbeat.

---

## Replacing a Device

`zendo/v2/replace_managed_device` attaches the **replacement**, the device whose `profile_id` and `device_id` are in the call, to another record, the **target**, so the automations that control the target control the replacement instead. The call doesn't have to come from the replacement: an admin's phone can replace a wall panel. The replacement's own record is the one it **leaves behind**. It is synchronous, with no `await`, so no other call can change the records halfway through. From 2.0.0 there's no v1 `replace_managed_device`.

For example, an admin replaces the panel "Hallway" with "Hallway 2". Hallway 2 is the replacement, Hallway's record is the target, and Hallway 2's own record is the one it leaves behind. Hallway 2 takes Hallway's record, its settings and its admin code.

1. No record has the replacement attached: `replacement_not_registered`.
2. No record has the target's managed device ID: `target_not_found`.
3. The replacement is already attached to the target: success, with no change and no events. A retry after a lost response is safe. `previous_device_name` is then the target's current name.
4. Otherwise:
   - The target's device name, admin code status and code are read first, for the settings restore, before anything changes.
   - The target takes the replacement's attached ids, device info (including `device_os_version`, `device_model_slug`, `device_launcher_id` and `device_serial_number`), app version, state, `online`, `offline_reason` and `last_seen`.
   - The target's Home Assistant device gets the replacement's name, model, serial number and app version. Its device ID, entity IDs, area and `name_by_user` stay.
   - The record the replacement leaves behind is removed, and so is its Home Assistant device. Home Assistant removes the device's entities with it.
   - The target's entities take the replacement's availability and state. The target keeps its optional entities, and gets any the replacement's state has a value for (see [Created When Reported](#created-when-reported)).
   - The old device's code state wins: the target keeps its admin code entry, `set` or `not_set`, or stays `unknown` without one, and never takes the replacement's (see [Admin Code](#admin-code)).
   - The target gets a settings restore, a snapshot of its latest backup and of its admin code status and code, for the replacement to fetch. The replacement's own backup entry is removed (see [Settings Backup](#settings-backup)).
   - `bngnt_managed_device_removed` fires for the record the replacement leaves behind, then `bngnt_managed_device_updated` for the target, with the `admin_code_status` it has after the replace, then `bngnt_managed_device_replaced`.
   - The response carries the target's managed device ID and `previous_device_name`, the target's device name from before, so the app can offer to give the replacement that name.

An online target can be taken over: the app asks for a second confirmation, and the integration doesn't check. The device that was attached to the target gets `registered: false` from its next heartbeat and registers again with a new record.

---

## Deleting a Device

Only offline managed devices can be deleted. Both ways go through `BNGntManagedDeviceRegistry.delete_device`, which removes the record's admin code entry (see [Admin Code](#admin-code)) and its settings backup entry, pending restore included (see [Settings Backup](#settings-backup)), and fires `bngnt_managed_device_removed`.

- **`zendo/v2/delete_managed_device`** forgets the record and removes its Home Assistant device, and Home Assistant removes the device's entities with it. An unknown managed device ID returns `deleted: false`.
- **The Delete button** on a managed device's page in Home Assistant calls `async_remove_config_entry_device` in `__init__.py`:
  - The Zendo service device can't be deleted.
  - Nothing can be deleted while the integration isn't loaded.
  - An online device is refused with an error that tells the user to turn off Always-On Display or sign the device out of Zendo first.
  - Otherwise the record is forgotten and Home Assistant removes the device and its entities itself.
  - A device with no record (an orphan) is always allowed.

---

## Device Registration (v2)

From 2.0.0. A Zendo device registers with Home Assistant, then calls the `zendo/v2/` WebSocket commands. The integration takes the device's ids from its registration, never from the call. A device on an older app keeps using the v1 services and commands that are left, with the rules in [Older Apps (v1)](#older-apps-v1).

The code is in `device_api.py` (the commands, the site link and the subscriptions), `device_auth.py` (tokens, registrations and command envelopes, with no Home Assistant import, so the tests load it on its own), `registrations.py`, `device_control_backend_keys.py` and `v1_rules.py`. The Homey app does the same, with camelCase fields: keep the two in sync.

### Linking to a Site

Home Assistant belongs to one Zendo site, and is linked by holding a push token: the token and its site are stored together in the config entry, as `push_notification_token` and `linked_site_id`. It's linked in one of two ways:

- **`zendo/v2/set_up`**, with an admin's token and the site's push token (see [`zendo/v2/set_up`](#zendov2set_up)).
- **A push token already stored**, by an older version or an older app's `setup_push_notifications`. This link is legacy: it brings an integration updated from before 2.0.0 onto the v2 calls by itself, and it's removed along with the v1 calls. It runs once each time the integration starts, at a Home Assistant restart or a reload, in the background, when a push token is stored and there's no link. Nothing else starts it, so a push token an older app stores later links at the next start. It asks Zendo which site the push token is for, with the `siteId` of `sitePushNotificationControlBackendDetails`, and then:
  - **With no answer at all**, when Zendo can't be reached, the request times out or the control backend's URL can't be resolved, as is usual right after a restart, it asks again after a minute, then twice as long after each one, up to an hour. It stops once it gets an answer, once Home Assistant is linked some other way, once another push token is stored, or when the integration unloads.
  - **With any answer**, a site, a 401, any other HTTP status, GraphQL errors or a response without a site, it stops, and doesn't ask again until the next start.
  - **After an answer with a site**, it reads the link and the stored push token again, and stores nothing if Home Assistant is linked by then, or if the stored push token is no longer the one it asked about.

Then:

- **A linked Home Assistant stays on its site.** `zendo/v2/set_up` answers `wrong_site` for another one, and there's no unlink: moving it to another site means removing the integration and adding it again (see [Moving to Another Property or Starting Over](#moving-to-another-property-or-starting-over)).
- **Until it's linked**, `zendo/v2/register_device` and the admin calls answer `not_linked`, and `managed_device_app_lock` and `managed_device_admin_code_configure` fail saying Zendo isn't set up on Home Assistant (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

#### `zendo/v2/setup_status`

```json
{"id": 40, "type": "zendo/v2/setup_status", "device_control_backend_token": "<device control backend token>"}
```

The result:

```json
{"success": true, "version": "2.0.0", "link": "this_property", "push_token_serial": "<serial>"}
```

| `link` | Meaning |
|---|---|
| `this_property` | Home Assistant is linked to the token's site. |
| `another_property` | Home Assistant is linked to another site. |
| `not_linked` | Home Assistant isn't linked to a site. |

- **`version`** is the integration's version.
- **`push_token_serial`** is the `jti` claim of the stored push token, which is its serial, read without verifying the token: the app only compares it with device-api's current serial. It's `null` when there's no push token, or it can't be read, or the claim isn't a non-empty string.
- **The token** is admin-scoped, and checked against its own site, since there may be no link yet. The device doesn't have to have registered, and the caller doesn't have to be a Home Assistant administrator.
- **It calls no cloud.** A stored push token that hasn't linked yet reads `not_linked`, with its serial. Checking a token that names a `kid` the integration doesn't have waits to read the signing keys record from DNS (see [Signing Keys](#signing-keys)).
- `device_control_backend_token` takes any value in the schema, and the handler checks it.

| Error code | When |
|---|---|
| `invalid_token` | The token is missing, or isn't one the integration accepts. |
| `not_admin` | The token isn't admin-scoped. |
| `device_revoked` | The token's device was revoked. |

#### `zendo/v2/set_up`

```json
{"id": 40, "type": "zendo/v2/set_up", "device_control_backend_token": "<device control backend token>", "push_token": "<push token>", "destinations": [{"id": "front-door", "destination": "security_camera", "link": "<deep link>"}]}
```

The result is exactly what `zendo/v2/setup_status` answers, after the changes. A call that's refused changes nothing.

The token is checked as for `zendo/v2/setup_status`. Then, by the link:

| Home Assistant | The call needs |
|---|---|
| Not linked | A Home Assistant administrator, and a `push_token`. The device doesn't have to have registered, since nothing can register before the link. |
| Linked to the token's site | A device that has registered, as for the other admin calls. Any Home Assistant user. Each of `push_token` and `destinations` is optional, and with neither the call only answers. |
| Linked to another site | It can't be set up: the answer is `wrong_site`. |

- **With `push_token`**, it asks Zendo which site the push token is for, which must be the token's site. It reads the link again after that wait, and stores the push token and the site together.
- **Every call refreshes the people and device groups** when a push token is stored, whether or not this one sent it. A refresh can be rate limited, so one that fails doesn't fail the call.
- **With `destinations`**, it stores them with the same code as `zendo/v2/set_deep_link_destinations`, which stays for site config publishes. With a `push_token` too, they're stored with the link, before the refresh. An empty list counts, and stores no destinations.
- A `null` `push_token` or `destinations` counts as not given.
- `device_control_backend_token` and `push_token` take any value in the schema, and the handler checks them.

| Error code | When |
|---|---|
| `invalid_token` | The token is missing, or isn't one the integration accepts. |
| `not_admin` | The token isn't admin-scoped, or Home Assistant isn't linked and the caller isn't a Home Assistant administrator. |
| `device_revoked` | The token's device was revoked, or is revoked while Zendo confirms the push token. |
| `wrong_site` | Home Assistant is linked to another site than the token's, including while Zendo confirms the push token, or the push token isn't for the token's site. |
| `unknown_api_key` | Home Assistant is linked, and the token's device hasn't registered. The app registers, then tries again. |
| `invalid_push_token` | Zendo refused the push token, or couldn't be reached. |

Its fields fail with `invalid_format`:

| Message | When |
|---|---|
| `Invalid push_token` | Home Assistant isn't linked and there's no `push_token`, or it isn't a non-empty string. |
| `Invalid destinations` | `destinations` isn't a list, or a JSON-stringified one. |

### Signing Keys

The device control backend token is an ES256 JWT that Zendo gives each device, renewed every day, which lasts a day. Its public keys are one TXT record, `device-control-backend.v1.keys.gnt.betterna.me`, which holds the whole key set:

```json
{"keys":[{"alg":"ES256","exp":2582150400,"key":"<base64 DER SPKI>","kid":"cd8e8e2f2ba758c3","nbf":1793232000}],"ttl":3600}
```

- The record is read over DNS over HTTPS, as the service discovery record is, and taken only when the resolver validated it (`AD` is true).
- A record past 255 bytes comes as several strings, which are joined before it's parsed. Cloudflare's JSON answer gives them quoted and escaped, and Google's already joined. The service discovery record is read the same way.
- A key is used only from its `nbf` until its `exp`.
- Each key has its own `alg`. The integration implements ES256, with P-256 keys. A key with an `alg` it doesn't implement is ignored, logged once at info, and the record's other keys are still used, so the record can list a newer algorithm's keys before every hub implements it.
- An ES256 key that can't be read, or whose `kid` isn't the first 8 bytes of SHA-256 over its DER SPKI, as hex, is left out, with a warning.
- The record is read again at most once a minute: in the background once it's past its `ttl`, with the token checked straight away with the keys kept, and before the check when a token names a `kid` it doesn't have.
- The last record read is kept in `.storage/zendo.device_control_backend_keys`, so a token can still be checked after a restart while DNS or Zendo can't be reached.

A token is checked with the key its header's `kid` names, and with that key's own `alg`: a header `alg` that isn't the key's, or isn't one the integration implements, is refused, and so is `alg: none`. Claims the integration doesn't know are ignored. A token is accepted when it's signed by one of those keys, from the production issuer, `https://device-control-backend.production.gnt.betterna.me`, for the audience `api:device-control-backend`, unexpired (with a minute's leeway for clocks), and for the linked site. A token from any other issuer is refused. An admin's token also has the scope `device-control-backend:admin`.

### Registering

`zendo/v2/register_device`:

```json
{"id": 41, "type": "zendo/v2/register_device", "device_control_backend_token": "<device control backend token>", "encryption": {"schemes": ["ecies-p256"]}}
```

The result:

```json
{"success": true, "api_key": "<43 base64url characters>", "encryption": {"scheme": "ecies-p256", "key_id": "e23e3a73c4a61211"}}
```

- **The token carries the device's encryption public key,** the `encryptionPublicKey` claim: base64 of a P-256 key's DER SPKI. The integration keeps that key from the token, and encrypts to it. A token without one, or with one that isn't a P-256 key, is answered `public_key_required`.
- **`key_id`** is the public key's id: hex of the first 8 bytes of SHA-256 over its DER SPKI, the same rule as a signing key's `kid`.
- **`api_key` is given once, in this answer.** The integration keeps only its SHA-256 hash.
- **`encryption.schemes`** is the device's list, most preferred first: today only `ecies-p256`, which a device that sends no `encryption` gets too. Schemes the integration doesn't know are skipped. With none in common, the answer is `unsupported_scheme`.
- **Registering again** replaces the API key, and the old one stops working at once. The public key comes from the new token.
- A revoked device can't register again (`device_revoked`). A device that joins the site again has a new `device_id`, so it registers.
- A device registers when it connects, and again when a call answers `unknown_api_key`.

### The v2 Commands

Each command takes its credential as a field, `api_key` or `device_control_backend_token`, and drops fields it doesn't know. Every field is checked in the handler, and one that isn't valid fails with `invalid_format` and a fixed message that names the field and never has its value. Expected failures answer `{"success": false, "error": <code>}`.

| Command | Fields | Credential | Answer |
|---|---|---|---|
| `zendo/v2/info` | none | none | `{version, linked}` |
| `zendo/v2/register_device` | `device_control_backend_token`, `encryption` | Device control backend token | See [Registering](#registering) |
| `zendo/v2/subscribe_events` | `api_key` | API key | A subscription |
| `zendo/v2/register_managed_device` | `api_key`, the fields of `register_managed_device` without the ids, and a wall panel's optional `device_model_slug`, `device_launcher_id` and `device_serial_number` (see [A Wall Panel's Fields](#a-wall-panels-fields)) | API key | `{success: true, managed_device_id, settings_restore_pending}` |
| `zendo/v2/unregister_managed_device` | `api_key` | API key | As `unregister_managed_device` |
| `zendo/v2/update_managed_device_state` | `api_key`, `state` (an object, or a JSON-stringified one) | API key | `{success: true, registered, settings_restore_pending}` |
| `zendo/v2/managed_device_command_response` | `api_key`, `response` (an object, or a JSON string) | API key | `{success: true}` |
| `zendo/v2/report_managed_device_admin_code` | `api_key`, `admin_code` | API key | As `zendo/report_managed_device_admin_code` |
| `zendo/v2/report_managed_device_settings_backup` | `api_key`, `settings_backup` | API key | As its v1 command (see [Reporting a Backup](#reporting-a-backup)) |
| `zendo/v2/get_managed_device_settings_restore` | `api_key` | API key | The pending restore, with `admin_code` in a sealed object (see [Settings Backup](#settings-backup) and [Sealed Objects](#sealed-objects)) |
| `zendo/v2/complete_managed_device_settings_restore` | `api_key`, `restore_id` | API key | `{success: true, registered, completed}` (see [Settings Backup](#settings-backup)) |
| `zendo/v2/doorbell_action` | `api_key`, `event_id`, optional `action` | API key | `{success: true}`, and `bngnt_doorbell_action` fires as for `doorbell_action` |
| `zendo/v2/get_encryption_key` | `api_key` | API key | `{success: true, sealed}`, with `key` in the sealed object, or `{success: true, key: null}` when no key is held (see [The Site Config Encryption Key](#the-site-config-encryption-key)) |
| `zendo/v2/revoke_device` | `api_key`, or `device_control_backend_token`, `profile_id` and `device_id` | API key to leave the site, or an admin-scoped token | `{success: true}` |
| `zendo/v2/subscribe_managed_device_changes` | `device_control_backend_token` | Admin-scoped token | A subscription |
| `zendo/v2/setup_status` | `device_control_backend_token` | Admin-scoped token, which needs no registration | `{success: true, version, link, push_token_serial}` (see [`zendo/v2/setup_status`](#zendov2setup_status)) |
| `zendo/v2/set_up` | `device_control_backend_token`, optional `push_token`, optional `destinations` (a list, or a JSON-stringified one) | Admin-scoped token, and, unlinked, a Home Assistant administrator | As `zendo/v2/setup_status`, after the changes (see [`zendo/v2/set_up`](#zendov2set_up)) |
| `zendo/v2/list_managed_devices` | `device_control_backend_token` | Admin-scoped token | `{success: true, managed_devices}` (see [The List](#the-list-zendov2list_managed_devices)) |
| `zendo/v2/replace_managed_device` | `device_control_backend_token`, `profile_id`, `device_id`, `managed_device_id` | Admin-scoped token | `{success: true, managed_device_id, previous_device_name}` or `{success: false, error}` (see [Replacing a Device](#replacing-a-device)) |
| `zendo/v2/delete_managed_device` | `device_control_backend_token`, `managed_device_id` | Admin-scoped token | `{success: true, deleted}` or `{success: false, error: "target_online"}` (see [Deleting a Device](#deleting-a-device)) |
| `zendo/v2/get_managed_device_admin_code` | `device_control_backend_token`, `managed_device_id` | Admin-scoped token | `{success: true, status, reported_at, sealed}`, with `admin_code` in the sealed object, or `{success: false, error}` (see [Admin Commands (v2)](#admin-commands-v2)) |
| `zendo/v2/configure_managed_device_admin_code` | `device_control_backend_token`, `managed_device_id`, and either `code` or `remove: true` | Admin-scoped token | `{success: true}` or `{success: false, error}` (see [Admin Commands (v2)](#admin-commands-v2)) |
| `zendo/v2/set_deep_link_destinations` | `device_control_backend_token`, `destinations` (a list, or a JSON-stringified one) | Admin-scoped token | `{success: true}` |
| `zendo/v2/notify_site_config_manifest_update` | `device_control_backend_token`, `payload` | Admin-scoped token | `{success: true}`, and `bngnt_site_config_manifest_updated` fires |
| `zendo/v2/claim_encryption_key` | `device_control_backend_token`, `key` | Admin-scoped token | `{success: true, sealed}`, with the key it keeps as `key` in the sealed object, or `{success: false, error}` (see [The Site Config Encryption Key](#the-site-config-encryption-key)) |

An admin-scoped token counts only for a device that has registered, except for `zendo/v2/setup_status`, and `zendo/v2/set_up` on a Home Assistant that isn't linked.

| Error code | When |
|---|---|
| `not_linked` | Home Assistant isn't linked to a site. `zendo/v2/setup_status` and `zendo/v2/set_up` never answer it as an error: it's a value of their `link`. |
| `invalid_token` | The token isn't one the integration accepts. |
| `wrong_site` | The token, or the push token, is for another site than the one Home Assistant is linked to, or, in `zendo/v2/set_up`, the two are for different sites. |
| `not_admin` | The call needs an admin-scoped token, or, for `zendo/v2/set_up` on a Home Assistant that isn't linked, a Home Assistant administrator. |
| `unknown_api_key` | No registration has that API key, or the admin's device has none. The app registers, then tries again. |
| `device_revoked` | The device was revoked. |
| `public_key_required` | Registering, `zendo/v2/get_managed_device_admin_code` or `zendo/v2/claim_encryption_key`, with a token that carries no `encryptionPublicKey`, or one that isn't a P-256 key. |
| `unsupported_scheme` | The device and the integration have no encryption scheme in common. |
| `invalid_push_token` | `zendo/v2/set_up` couldn't confirm the push token's site with Zendo: Zendo refused it, or couldn't be reached. |

After an unload, each command answers the error `not_set_up`, since a WebSocket command can't be unregistered. That includes `zendo/v2/info`, so `linked: false` only ever means Home Assistant isn't linked, never that the integration is unloaded.

### Subscriptions

- **`zendo/v2/subscribe_events`** sends each event as `{"event_type": <name>, "data": <data>}`, like Home Assistant's own `subscribe_events`:
  - `bngnt_managed_device_command`, for a registered device: a command envelope (see [Command Envelopes](#command-envelopes));
  - `bngnt_device_deleted`, `{profile_id, device_id}`, when a device is revoked;
  - `bngnt_subscriptions_ended`, when the integration unloads (see below);
  - forwarded from the bus: `bngnt_site_config_manifest_updated`, `bngnt_doorbell_ring`, `bngnt_doorbell_action` and `bngnt_managed_device_replaced`.
- **`zendo/v2/subscribe_managed_device_changes`** forwards `bngnt_managed_device_updated`, `bngnt_managed_device_removed` and `bngnt_managed_device_replaced`, for Control Panels, and sends `bngnt_subscriptions_ended` (see below).
- **Both work for any Home Assistant user**, administrator or not.
- **A subscription that's refused fails with the error code itself** as the error's code, `unknown_api_key` or `not_admin` for example, since a subscription has no answer of its own.
- **Revoking a device ends its subscriptions.**
- **An unload ends them all, and says so.** A reload or a removal leaves the connection open, so before dropping them the integration sends each open subscription, on both commands, `{"event_type": "bngnt_subscriptions_ended", "data": {}}`. The app subscribes again when it hears it. Until the integration is set up again, subscribing answers the error `not_set_up`, so the app tries again.

### Command Envelopes

A command for a registered device goes to every `zendo/v2/subscribe_events` subscription, and never on the bus: no `bngnt_managed_device_command` event fires for it. Each device reads only the commands with its own ids.

```json
{"profile_id": "profileId123", "device_id": "deviceId456", "id": "<JSON-RPC id>", "encryption": "none", "payload": {"jsonrpc": "2.0", "method": "screen.wakeUp", "params": {}, "id": "<JSON-RPC id>"}}
```

- **`adminCode.set` and `app.lock` are always encrypted** to the device's public key. Their `encryption` is `ecies-p256`, `key_id` names the device's key, and `payload` is the request as compact JSON, encrypted as in [ecies-p256](#ecies-p256). The additional data is the UTF-8 of `profile_id`, `device_id`, `id`, `encryption` and `key_id`, joined by newlines.
- **Every other command goes in clear**, with `encryption: "none"`, the request itself as `payload`, and no `key_id`. A `sealed` object in its params goes as it is: the integration doesn't open it.
- **A device that hasn't registered never gets `adminCode.set` or `app.lock`**, in an envelope or as a `bngnt_managed_device_command` event (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).
- **The device answers** with `zendo/v2/managed_device_command_response`. An answer counts only when it comes from the device the command went to: for a registered device, from its registration, and for any other, from a call that names it.

`device_auth.py`'s `MUST_ENCRYPT_METHODS` lists the methods that are always encrypted, and that `may_send_command` keeps from a device that hasn't registered. The Zendo app refuses them in clear: keep it in sync with the app and the Homey app.

### ecies-p256

Encrypting to a device's P-256 public key, for envelopes and sealed objects alike:

1. A one-off P-256 key pair is made for each message.
2. Its ECDH with the device's public key gives the shared secret, the 32-byte x-coordinate.
3. HKDF-SHA256, with an empty salt and as info the UTF-8 of `zendo ecies-p256 v1` followed by the one-off public key and the device's public key, both as 65-byte uncompressed points, gives a 32-byte AES key.
4. AES-256-GCM encrypts, with a random 12-byte nonce and the message's additional data.

The result is base64 of the one-off public key (65 bytes), the nonce, the ciphertext and the 16-byte tag. `device_auth.py` does it with `cryptography`, which Home Assistant ships with.

### Sealed Objects

A sealed object carries some of a message's fields encrypted to the public key of the device that opens it, while the rest of the message stays in clear: `{"scheme": "ecies-p256", "data": <base64>}`. `data` is the fields' compact JSON, encrypted as in [ecies-p256](#ecies-p256), with as additional data the UTF-8 of `sealed`, that device's `profile_id`, its `device_id` and the method, joined by newlines. The device opens it and adds the fields back.

- **The settings restore answer** of `zendo/v2/get_managed_device_settings_restore` has no `admin_code`: its `restore` carries a `sealed` object instead, for the method `settingsRestore.get`, which opens to `{"admin_code": <the code, or null>}`. Every restore gets one, with a code or without. `admin_code_status` stays in clear.
- **The admin code answer** of `zendo/v2/get_managed_device_admin_code` has no `admin_code` either: it carries a `sealed` object instead, for the method `adminCode.get`, which opens to `{"admin_code": <the code, or null>}`. It's sealed to the encryption public key in the caller's token, not a registration's, with the caller's `profileId` and `deviceId` from the token in the additional data. Every answer gets one, with a code or without. `status` and `reported_at` stay in clear.
- **The site config encryption key's answers,** from `zendo/v2/get_encryption_key` and `zendo/v2/claim_encryption_key`, carry a `sealed` object for the method `encryptionKey.get`, which opens to `{"key": <the key>}`. A read is sealed to the registration's public key, and a claim to the encryption public key in the caller's token (see [The Site Config Encryption Key](#the-site-config-encryption-key)).
- **A `sealed` object in a command's params** is sent to the device as it is.

### The Site Config Encryption Key

The Zendo app encrypts the site config with a key it generates, and keeps it in a helper it creates, `input_text.bn_gnt_elderflower_config_encryption_key`. From 2.0.0 the integration keeps its own copy, in `encryption_key.py`, and a device on the v2 calls reads the key from the integration, so the helper can be removed later with no app release. The helper stays for now, because older apps read it. The Homey app has the same two calls.

**Which key is held,** always in this order:

1. the store's;
2. otherwise the helper's state, if it's a non-empty string and not `unknown` or `unavailable`, which is then copied into the store;
3. otherwise none.

Setup works this out when it loads the store, and each of the two commands works it out again while no key is held. Once the store holds a key, the helper is never read again, so a later change to the helper doesn't reach the store. Nothing here ever writes the helper, and a stored key is never replaced. `held_encryption_key` in `device_auth.py` has the rule.

**Load order.** The integration doesn't depend on `input_text`, so a problem there can't stop it setting up. The helper's state may not be there yet at startup, so the held key is worked out again on each call until one is stored. A claim can't store `unknown` or `unavailable`, which are what a helper with no value reads as.

#### `zendo/v2/get_encryption_key`

```json
{"id": 47, "type": "zendo/v2/get_encryption_key", "api_key": "<API key>"}
```

Any registered device can call it. The answer is:

```json
{"success": true, "sealed": {"scheme": "ecies-p256", "data": "<base64>"}}
```

- **`sealed`** opens to `{"key": "<the key>"}`. It's sealed to the registration's public key, and its additional data is `sealed`, the registration's `profile_id`, its `device_id` and `encryptionKey.get`, joined by newlines (see [Sealed Objects](#sealed-objects)).
- **With no key held,** the answer is `{"success": true, "key": null}`, in clear.

#### `zendo/v2/claim_encryption_key`

```json
{"id": 48, "type": "zendo/v2/claim_encryption_key", "device_control_backend_token": "<device control backend token>", "key": "<the key>"}
```

It takes an admin-scoped token, as the other admin calls do. The answer is `{"success": true, "sealed": ...}`, or `{"success": false, "error": <code>}`.

- **It stores `key` only if no key is held,** and always answers with the key it keeps. With a key held, the one in the call is dropped, so the caller uses the one in the answer.
- **`sealed`** opens to `{"key": "<the key kept>"}`. It's sealed to the encryption public key in the caller's token, its `encryptionPublicKey` claim, and its additional data is `sealed`, the `profileId` and `deviceId` from the token and `encryptionKey.get`, joined by newlines.
- **`public_key_required`** answers a token without an encryption public key, or with one that isn't a P-256 key, and nothing is stored. It's checked after the token and before the fields.
- **A `key` that isn't a non-empty string** fails with `invalid_format` and the message `Invalid key`, and nothing is stored.
- **An unload during the token check** answers the error `not_set_up`, and nothing is stored, so the app tries again.

#### Key Storage

`.storage/zendo.site_config_encryption_key` (`Store` version 1), written like the admin codes' store: `private=True`, with atomic writes, saved with `async_delay_save` and flushed when the integration unloads.

```json
{"key": "<the key>"}
```

- Copying the helper's key is a write, like a claim.
- At load, anything but a non-empty string reads as no key stored.
- Removing the integration removes the file and leaves the helper, which isn't the integration's (see [Moving to Another Property or Starting Over](#moving-to-another-property-or-starting-over)).

### Revoking

`zendo/v2/revoke_device` with an admin-scoped token revokes the device `profile_id` and `device_id` name. With the device's own `api_key`, the device leaves the site. Either way:

- its registration is removed, and a tombstone kept for good;
- `bngnt_device_deleted` goes to the subscriptions, and fires on the bus;
- its subscriptions end;
- its managed device, if it has one, goes offline with the reason `unregistered`, so automations still point at it and a replacement can take it over;
- every long-lived token labelled for it is removed a second later, for any Home Assistant user, which closes its connections. The wait lets `bngnt_device_deleted` go out first. The label is the `bngnt1:` the app puts at the end of the token's name, with the site, profile and device. A device leaving the site also gets its answer first: its own token is removed a second after the answer.

### Older Apps (v1)

From 2.0.0, these v1 calls stay for devices on an older app while they move to v2:

- the services `register_managed_device`, `unregister_managed_device`, `update_managed_device_state` and `managed_device_command_response` (see [Services](#services));
- the services `setup_push_notifications`, `set_deep_link_destinations`, `notify_site_config_manifest_update` and `doorbell_action`;
- the WebSocket commands `zendo/report_managed_device_admin_code` (see [Admin Code](#admin-code)) and `zendo/report_managed_device_settings_backup` (see [Settings Backup](#settings-backup)).

Changed in 2.0.0:

- **`register_managed_device` doesn't take a wall panel's fields.** An app that still sends `device_model_slug`, `device_launcher_id` or `device_serial_number` registers as before: the fields are dropped, and the record keeps the values it has (see [A Wall Panel's Fields](#a-wall-panels-fields)).
- **`register_managed_device` and `update_managed_device_state` answer without `settings_restore_pending`.** The app reads a missing field as no restore pending, so a device on an older app never asks for one.
- **A device on an older app is never sent `adminCode.set` or `app.lock`.** Locking it or setting its admin code fails, saying it needs a newer version of the Zendo app, or that Zendo isn't set up on Home Assistant until it's linked to a site, and every other command still reaches it as a `bngnt_managed_device_command` event (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)).

Removed in 2.0.0, `zendo/v2/` only from then on:

| v1 call | Use instead |
|---|---|
| The service `list_managed_devices` | `zendo/v2/list_managed_devices` |
| The service `replace_managed_device` | `zendo/v2/replace_managed_device` |
| The service `delete_managed_device` | `zendo/v2/delete_managed_device` |
| The WebSocket command `zendo/get_managed_device_settings_restore` | `zendo/v2/get_managed_device_settings_restore` |
| The WebSocket command `zendo/complete_managed_device_settings_restore` | `zendo/v2/complete_managed_device_settings_restore` |
| The action `managed_device_admin_code_get` | `zendo/v2/get_managed_device_admin_code`, which the Zendo app calls when an admin views a device's admin code (see [Admin Commands (v2)](#admin-commands-v2)) |

A call to a removed service fails as an unknown action (`not_found` over the WebSocket API), and a removed command answers `unknown_command`.

The v1 calls that stay follow these rules:

- **A call that names a registered or revoked device is refused.**
- **Once Home Assistant is linked, `setup_push_notifications` is refused.**
- **Once a device has registered with an admin-scoped token, the calls an admin makes are refused:** `setup_push_notifications`, `set_deep_link_destinations` and `notify_site_config_manifest_update`.

A refused service that answers returns `{"success": false, "error": "v2_required"}`, and so does a refused WebSocket command. One that doesn't answer fails with "This needs a newer version of the Zendo app." Each v1 call is logged once per device at info, so the devices still on an older app can be found. The integration remembers up to 500 of them while it runs, and then starts over, so a call may be logged again. The Homey app does the same.

The automation services, `send_notification`, `ring_doorbell`, `refresh_profiles`, `list_deep_link_destinations` and `managed_device_admin_code_configure`, are for people and automations, not the app. From 2.0.0, `setup_push_notifications`, `refresh_profiles`, `managed_device_admin_code_configure`, `managed_device_app_reload`, `managed_device_app_lock` and `managed_device_app_unlock` are admin-only (see [Automation Services](#automation-services)).

### Registration Storage

`.storage/zendo.device_registrations` (`Store` version 1), written like the admin codes' store: `private=True`, with atomic writes, saved with `async_delay_save` and flushed when the integration unloads.

```json
{
  "registrations": [
    {"profile_id": "profileId123", "device_id": "deviceId456", "api_key_hash": "<sha256 hex>", "public_key": "<base64 DER SPKI>", "key_id": "e23e3a73c4a61211", "admin": false, "registered_at": 1793232000.0}
  ],
  "tombstones": [{"profile_id": "profileId123", "device_id": "deviceId000", "revoked_at": 1793232000.0}],
  "admin_registered": true
}
```

- A device has at most one registration, and a tombstoned device none.
- `admin_registered` turns on the first time a device registers with an admin-scoped token, and stays on.
- At load, any part that isn't valid is dropped, including a registration whose `key_id` isn't its public key's id.
- Removing the integration removes the file, and the signing keys' and the link with it: Home Assistant links again with `zendo/v2/set_up`, and every device registers again (see [Moving to Another Property or Starting Over](#moving-to-another-property-or-starting-over)).

### Tests

`tests/test_device_auth.py` checks `device_auth.py` against `tests/companion-app-authentication-vectors.json`, the cases the Zendo app and the Homey app check too: the tokens and what registering with each gives, the keys record as each resolver answers it, the envelopes and the sealed objects, each down to the same bytes. It also checks `may_send_command`, which commands go to a device that hasn't registered, `command_refusal`, why a command isn't sent and which reason comes first, `site_link` and `push_token_serial`, what `zendo/v2/setup_status` answers, `link_retry_interval`, how long the link from a stored push token waits, and `admin_code_lookup_for_attached_device`, what an admin's lookup answers for a device that hasn't registered, and `held_encryption_key` and the answers built on it, which site config encryption key is held and what the two commands say about it. From the repository root, with PyJWT and cryptography installed (Home Assistant ships both):

```sh
python3 -m unittest discover -s tests -v
```

---

## Moving to Another Property or Starting Over

Home Assistant stays linked to the property it was first linked to, and there's no unlink (see [Linking to a Site](#linking-to-a-site)). To move it to another property, or to start over at the same one, remove the Zendo integration in Home Assistant and add it again.

**Removing it removes:**

- the link to the property;
- every device's registration, and the record of the devices that were revoked;
- push notifications: the push token, and the people and device group entities;
- the managed devices, with their Home Assistant devices and entities, so the automations that use them stop working;
- every device's admin code and settings backup, and any settings restore still waiting for a replacement, which then keeps its own settings;
- the integration's copy of the site config encryption key;
- the deep link destinations.

`async_remove_entry` removes the admin codes', the settings backups', the registrations', the signing keys' and the site config encryption key's stores. Home Assistant removes the config entry, which holds the link, the push token and the managed device records, and the entry's devices and entities with it. The devices stay signed in to Home Assistant: removing the integration leaves the Home Assistant tokens the Zendo app made, so each device connects again by itself. It also leaves the site config encryption key's helper, which isn't the integration's.

**Once an admin has set it up again, by itself:**

- each device registers again, and reports its state again;
- each device reports its admin code and its settings backup again. Until it does, its admin code status is `unknown`;
- setup copies the site config encryption key from the helper again, when the helper holds one, whichever key the store held before. At another property, that's the old property's key. With none there, the next admin's claim sets it (see [The Site Config Encryption Key](#the-site-config-encryption-key));
- each device gets a managed device again. A device that had one here before asks for the managed device ID it had, and gets it back, and Home Assistant can bring its old device and entities back with it (see [Choosing a Managed Device ID](#choosing-a-managed-device-id)). Any other device gets a new managed device: at another property, that's every device.

**What an admin redoes:**

- **Set it up**, from an admin's Zendo app connected to Home Assistant as an administrator, with `zendo/v2/set_up`. The push token it sends links Home Assistant to the property and brings back push notifications, with the people and device group entities, and the same call can send the deep link destinations again.
- **Update the automations** that use a managed device that came back as a new one, or a person or device group whose entity ID changed. At another property, that's every automation that uses Zendo.

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

From 2.0.0 (device registration):

- **An older app with a 2.0.0 integration.** It never registers, so it keeps using the v1 services and commands that stay, with the rules in [Older Apps (v1)](#older-apps-v1). An upgraded integration links at startup from the push token it already has, so from then on the older app can't set up push notifications, and once an admin's device has registered, its admin actions are refused too, until it's updated.
- **The calls 2.0.0 removed.** An older app's calls to `list_managed_devices`, `replace_managed_device`, `zendo/get_managed_device_settings_restore` and `zendo/complete_managed_device_settings_restore` fail, so it can't list or replace devices until it's updated. Its register and state update answers have no `settings_restore_pending`, so it never fetches a restore: a restore left on its record stays until a device on a newer app completes it, a later replace passes it on, or the record is deleted.
- **The `managed_device_admin_code_get` action is gone.** An automation or script that calls it fails as an unknown action. An admin views a device's admin code in the Zendo app, which uses `zendo/v2/get_managed_device_admin_code`. `managed_device_admin_code_configure` stays.
- **A wall panel on an older app.** `register_managed_device` drops its `device_model_slug`, `device_launcher_id` and `device_serial_number`, and its record keeps the values it has: a record that 1.9.5 or 1.9.6 stored keeps them, and a new record has `null` for all three until the panel registers with v2.
- **A newer app with an older integration.** `zendo/v2/info` answers `unknown_command`, and the app uses the v1 services and commands as before.
- **The site config encryption key.** An upgraded integration copies the key from the helper at setup. An older app keeps reading and writing the helper, which the integration never writes: a key an older app writes there once the store holds one doesn't reach the store (see [The Site Config Encryption Key](#the-site-config-encryption-key)).
- **The status entity.** `binary_sensor.zendo_status` stays, with two attributes, `integration_version` and `push_notifications_configured`, which released apps read. It no longer has `api_version`, `people` or `device_groups`.
- **Commands.** A command to a device that hasn't registered is a `bngnt_managed_device_command` event as before, except `adminCode.set` and `app.lock`, which it's never sent: an automation that locks it or sets its admin code fails, saying it needs a newer version of the Zendo app, or that Zendo isn't set up on Home Assistant until it's linked to a site (see [A PIN or an Admin Code Needs a Current App](#a-pin-or-an-admin-code-needs-a-current-app)). A command to a registered device is never an event: it goes through the subscriptions.
- **Downgrading below 2.0.0.** The earlier version doesn't know the registration, signing key and site config encryption key stores, or `linked_site_id`, and leaves them alone. A device that has registered stays on v2 with this Home Assistant, so it can't reach the earlier version's v1 calls until the integration is upgraded again, when its registration comes back.
