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
  devices report it with, the admin-only action that sets it, the `zendo/v2/` commands an
  admin views and changes it with, the view sealed to the key in the admin's token, the
  lookup's `unknown` for a device that hasn't registered, the
  `managed_device_admin_code_get` action 2.0.0 removed, and on a replace the old record's
  code status winning: the new device keeps a code only when it's `set`), the settings backup
  (its private store, the restore every replace leaves for the replacement, passed on under
  a new id while one is still pending, the three WebSocket commands, of which fetching and
  completing the restore are `zendo/v2/` only, the 16 KiB cap shared with the Homey app and
  the Zendo app, and `settings_restore_pending`, which only the v2 answers carry), a wall
  panel's fields (which only the v2 register takes), replacing a device (the replacement,
  the target and the record the replacement leaves behind) and deleting one, the Delete
  button, device registration from 2.0.0 (the site link, a push token and its site stored
  together, `zendo/v2/setup_status` and `zendo/v2/set_up` with their errors, the legacy link from
  a stored push token, production tokens only, the signing keys record and its store, the
  `zendo/v2/` commands, their `device_control_backend_token` field and their error codes, the
  subscriptions, command envelopes (a PIN or an admin code only ever goes to a registered
  device), the site config encryption key (its private store, which key is held, from
  the store or the `input_text` helper, and `zendo/v2/get_encryption_key` and
  `zendo/v2/claim_encryption_key`), revoking, the v1 calls older apps still have, those
  2.0.0 removed and the rules for the rest, the registrations store, and the tests against
  the shared vectors), moving to another property or starting over (removing the
  integration: what it removes, what comes back by itself and what an admin redoes), the
  status entity's two attributes, and the migration to config entry minor version 2.

## Tests

`python3 -m unittest discover -s tests -v`, from the repository root, with PyJWT and
cryptography installed. See docs/managed-devices.md, "Tests".
