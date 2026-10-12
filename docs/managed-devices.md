# Managed Devices - Home Assistant Guide

> **Note:** This document is intended as context for AI-assisted development. For user-facing documentation, see https://docs.myzendo.com

A managed device is a phone, tablet or wall panel running the Zendo app with Always-On Display (Kiosk) on. It registers with Home Assistant so automations can control it: wake the screen, set the brightness or volume, lock it, and so on. Each managed device is a Home Assistant device with its own sensor entities (battery, screen, volume, locked and so on).

The code lives in `custom_components/zendo/managed_devices.py`. The services are registered in `__init__.py`.

---

## Records and Managed Device IDs

The integration owns each managed device. It keeps one **record** per managed device, keyed by a **managed device ID**:

- The managed device ID is also the Home Assistant device identifier `(zendo, <managed device ID>)`, and the entities' unique IDs are `md_<managed device ID>_<property>`. Automations point at the Home Assistant device, so they follow the record.
- The record holds the Zendo device that is **attached** to it right now: its `profile_id` and `device_id`, plus its name, model, operating system, app version, last state, whether it's online and when it was last seen.
- The attached device can change. A replacement tablet can take over a record (see [Replacing a Device](#replacing-a-device)), and the record keeps its managed device ID, Home Assistant device, entity IDs, area and the name the user gave it.

**Managed device IDs are opaque.** Never build one from a profile ID and device ID, and never parse one. Records made before 1.9.0 have the ID `<profile_id>_<device_id>`, and new ones may have that form too, but a record's ID says nothing about the device attached to it now.

Records are persisted in the config entry under `managed_devices`, keyed by managed device ID. The persisted fields are the attached ids, the device info, the app version and `last_seen`. The state and `online` are not persisted: after a restart every record starts offline, with its entities unavailable until the device sends its first heartbeat.

`last_seen` is a Unix timestamp in seconds. It is written to the config entry when a device registers, is replaced or is deleted, and when it goes online or offline, not on every heartbeat.

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
| `register_managed_device` | `profile_id`, `device_id`, `device_name`, `device_brand`, `device_type`, `device_model_name`, `device_os`, `app_version`, optional `managed_device_id` | optional | `{success: true, managed_device_id}` |
| `unregister_managed_device` | `profile_id`, `device_id` | optional | `{success: true, registered}` |
| `update_managed_device_state` | `profile_id`, `device_id`, `state` | optional | `{success: true, registered}` |
| `list_managed_devices` | none | only | `{managed_devices: [...]}` |
| `replace_managed_device` | `profile_id`, `device_id`, `managed_device_id` | only | `{success: true, managed_device_id, previous_device_name}` or `{success: false, error}` |
| `delete_managed_device` | `managed_device_id` | optional | `{success: true, deleted}` or `{success: false, error: "target_online"}` |

`unregister_managed_device` keeps the record, its Home Assistant device and its entities, and marks them unavailable. Turning off Always-On Display or signing out of the app does this.

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
    app_version: "26.10.1"
    online: false
    last_seen: 1791763200000
```

`name_by_user` is the name the user gave the device in Home Assistant, or `null`. `last_seen` is in milliseconds since the Unix epoch, or `null` when the device hasn't been seen since it was restored from a version before 1.9.0.

---

## Replacing a Device

`replace_managed_device` attaches the calling device to another record, the **target**, so the automations that control the target control the calling device instead. It is synchronous, with no `await`, so no other service call can change the records halfway through.

1. No record has the calling device attached: `caller_not_registered`.
2. No record has the target's managed device ID: `target_not_found`.
3. The calling device is already attached to the target: success, with no change. A retry after a lost response is safe. `previous_device_name` is then the target's current name.
4. Otherwise:
   - The target takes the caller's attached ids, device info, app version, state, `online` and `last_seen`.
   - The target's Home Assistant device gets the caller's name, model and app version. Its device ID, entity IDs, area and `name_by_user` stay.
   - The caller's own record is removed. Its config entry is removed from its Home Assistant device, and Home Assistant then removes that device and its entities.
   - The target's entities take the caller's availability and state.
   - The response carries the target's managed device ID and `previous_device_name`, the target's device name from before, so the app can offer to rename itself to it.

An online target can be taken over: the app asks for a second confirmation, and the integration doesn't check. The device that was attached to the target gets `registered: false` from its next heartbeat and registers again with a new record.

---

## Deleting a Device

Only offline managed devices can be deleted. Both ways go through `BNGntManagedDeviceRegistry.delete_device`.

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

- **An older app with a 1.9.0 integration.** It sends no hint, so new records get `<profile_id>_<device_id>` when it's free, as before. It ignores the new response fields.
- **A 1.9.0 app with an older integration.** The integration drops the unknown `managed_device_id` field. A response without `managed_device_id` tells the app the integration is older. The app shows **Replace Existing Device** only when the `integration_version` attribute of `binary_sensor.zendo_status` is 1.9.0 or later.
- **Downgrading the integration.** An earlier version builds the old unique IDs again, so its entities get new entity IDs, and it creates a second service device under the entry ID. Avoid downgrading below 1.9.0.
