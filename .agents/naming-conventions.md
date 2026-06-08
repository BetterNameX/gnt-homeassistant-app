# Naming Conventions

## "Zendo" vs "BNGnt"

The word "Zendo" must only appear in **user-visible labels** - text that end
users see in the Home Assistant UI, error messages, documentation aimed at
users, device names, manufacturer fields, HACS metadata, etc.

Everything else - class names, internal event names, docstrings, code
comments - must use **BNGnt** (PascalCase) or **bngnt_** (snake_case).
When "Zendo" appeared only as a filler word in a docstring or comment
(e.g. "Constants for the Zendo integration"), simply omit it rather than
replacing it ("Constants for the integration").

### The domain exception

The Home Assistant integration **domain** stays `zendo` because changing it
would be a breaking change for existing users (their config entries, entity
IDs, automations, and HACS tracking all depend on it). This means:

- The folder remains `custom_components/zendo/`.
- `DOMAIN = "zendo"` in `const.py`.
- `"domain": "zendo"` in `manifest.json`.
- Service calls use `zendo.*` (e.g. `zendo.send_notification`).
- Entity IDs use `zendo` as prefix (e.g. `notify.zendo_owner`).
- `integration: zendo` in `services.yaml` target selectors.

### What uses BNGnt / bngnt_

- Python class names: `BNGntNotifyEntity`, `BNGntStatusBinarySensor`,
  `BNGntConfigFlow`.
- HA bus event names: `bngnt_doorbell_ring`, `bngnt_doorbell_action`,
  `bngnt_site_config_manifest_updated`.
- Docstrings and comments: no "Zendo" - omit the word or use the code name.

### Quick reference

| Context                        | Use          | Example                                      |
|--------------------------------|--------------|----------------------------------------------|
| HA UI label / device name      | Zendo        | `name="Zendo"`                               |
| Error message shown to user    | Zendo        | `"Please open the Zendo iOS/Android app..."` |
| User-facing docs / README      | Zendo        | "Search for **Zendo** and install it."       |
| HACS / manifest display name   | Zendo        | `"name": "Zendo"`                            |
| Integration domain             | zendo        | `DOMAIN = "zendo"` (exception - do not change) |
| Python class name              | BNGnt        | `class BNGntConfigFlow`                      |
| Bus event name                 | bngnt_       | `"bngnt_doorbell_ring"`                      |
| Docstring / code comment       | (omit)       | `"""Config flow for the integration."""`     |
