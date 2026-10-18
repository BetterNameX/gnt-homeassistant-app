# Agent Instructions

Read these files for project details:

- [.agents/naming-conventions.md](.agents/naming-conventions.md)
- [.agents/service-file-sync.md](.agents/service-file-sync.md)

## Docs

- [docs/push-notifications-deep-linking.md](docs/push-notifications-deep-linking.md): push
  notifications and their deep links.
- [docs/managed-devices.md](docs/managed-devices.md): managed devices (Always-On Display):
  records and their opaque managed device IDs, online status and the offline reason, the
  state the app reports, the `bngnt_managed_device_updated` / `_removed` / `_replaced`
  events, the automation services and their JSON-RPC commands and errors, wall panel
  settings and presence (entities made only once a device reports them), the admin code
  (its private store, its `admin_code_status` in each list item, the WebSocket command
  devices report it with, the admin-only actions, what the recorder keeps, and on a replace
  the old record's code status winning: the new device keeps a code only when it's `set`),
  the settings backup (its private store, the restore every replace leaves for the
  replacement, passed on under a new id while one is still pending, the three WebSocket
  commands, the 16 KiB cap shared with the Homey app and the Zendo app, and
  `settings_restore_pending`), replacing a device (the replacement, the target and the
  record the replacement leaves behind) and deleting one, the Delete button, and the
  migration to config entry minor version 2.
