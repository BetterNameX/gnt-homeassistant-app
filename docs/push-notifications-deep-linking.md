# Push Notifications Deep Linking - Home Assistant Guide

> **Note:** This document is intended as context for AI-assisted development. For user-facing documentation, see https://docs.myzendo.com

Deep link destinations allow push notifications sent from Home Assistant automations to open a specific screen in the Zendo app when tapped. For example, a notification about motion on a camera can open that camera's live stream directly.

---

## How It Works

1. The Zendo mobile app pushes a list of available destinations (cameras, etc.) into Home Assistant.
2. Home Assistant stores this list locally.
3. When creating an automation that sends a notification, you reference a destination by its ID.
4. When the user taps the notification on their phone, the Zendo app opens the matching screen.

---

## Step 1: Destinations Are Populated Automatically

The Zendo app sends destinations to Home Assistant automatically at two points:

- When push notifications are first set up.
- When site configuration is saved in the app (e.g. after adding or renaming a camera).

**No manual action is needed.** As long as the Zendo app has completed push notification setup, destinations will be available.

---

## Step 2: List Available Destinations

To see which destinations are available, call the `list_deep_link_destinations` service. This is a response-returning service - it outputs data you can inspect.

### Developer Tools > Services

1. Go to **Developer Tools > Services** in Home Assistant.
2. Select **Zendo: List deep link destinations**.
3. Click **Call Service**.
4. The response panel will show the available destinations.

### Response Format

```yaml
destinations:
  - id: "securityCamera_abc123"
    destination: "security_camera"
    title: "Front door"
    subtitle: "Camera/Doorbell"
    entityType: "security_camera"
    link: "Zendo:/deeplink-v1?dst=security_camera&id=abc123"
  - id: "securityCamera_def456"
    destination: "security_camera"
    title: "Back garden"
    subtitle: "Camera/Doorbell"
    entityType: "security_camera"
    link: "Zendo:/deeplink-v1?dst=security_camera&id=def456"
```

### Field Reference


| Field         | Description                                                                                     |
| ------------- | ----------------------------------------------------------------------------------------------- |
| `id`          | The stable identifier you use in automations. This is what you pass to `deep_link_destination`. |
| `destination` | The type of destination (e.g. `security_camera`).                                               |
| `title`       | Human-readable name of the destination (e.g. the camera's label).                               |
| `subtitle`    | Secondary description (e.g. "Camera/Doorbell").                                                 |
| `entityType`  | The entity category.                                                                            |
| `link`        | The full deep link URI. You don't need to use this directly.                                    |


---

## Step 3: Send a Notification with a Deep Link

Use the `zendo.send_notification` service with the optional `deep_link_destination` field.

### YAML Automation Example

```yaml
automation:
  - alias: "Front door motion - notify with camera deep link"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_motion
        to: "on"
    action:
      - service: zendo.send_notification
        target:
          entity_id: notify.zendo_owner
        data:
          message: "Motion detected at the front door"
          deep_link_destination: "securityCamera_abc123"
```

When you tap this notification, the Zendo app opens the front door camera stream.

### With Interruption Level

```yaml
- service: zendo.send_notification
  target:
    entity_id: notify.zendo_owner
  data:
    message: "Someone is at the front door"
    interruption_level: "time_sensitive"
    deep_link_destination: "securityCamera_abc123"
```

### Without a Deep Link (Existing Behaviour)

The `deep_link_destination` field is entirely optional. Omitting it sends a normal notification that shows an alert or the notification detail view when tapped - exactly as before.

```yaml
- service: zendo.send_notification
  target:
    entity_id: notify.zendo_owner
  data:
    message: "Good morning!"
```

### Developer Tools > Services

1. Go to **Developer Tools > Services**.
2. Select **Zendo: Send notification**.
3. Pick the target entity (a Zendo person).
4. Fill in the **Message**.
5. Optionally fill in **Deep link destination** with the destination ID (e.g. `securityCamera_abc123`).
6. Click **Call Service**.

---

## What Happens When a Destination Can't Be Resolved


| Scenario                                              | Behaviour                                                                                                                 |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `deep_link_destination` ID not found in cached list   | Warning logged, notification sent **without** a deep link. The message still arrives.                                     |
| No destinations have been set yet (app hasn't synced) | Same as above - the field is simply ignored.                                                                              |
| Camera removed from config after destination was set  | Notification sends with the deep link. The Zendo app handles the missing camera gracefully with its existing fallback UI. |
| `deep_link_destination` field omitted                 | Normal notification, no deep link. Existing behaviour unchanged.                                                          |


The notification **never fails** because of a deep link issue. The deep link is best-effort.

---

## Example: Doorbell Notification with Camera Deep Link

A doorbell press is the ideal use case - the notification tells you someone is at the door, and tapping it opens the doorbell camera stream so you can see who it is.

```yaml
automation:
  - alias: "Doorbell pressed - notify household"
    description: >-
      When the doorbell is pressed, notify everyone with a time-sensitive
      alert. Tapping the notification opens the doorbell camera stream
      so they can see who's at the door.
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_press
        to: "on"
    action:
      - service: zendo.send_notification
        target:
          entity_id:
            - notify.zendo_owner
            - notify.zendo_bob
        data:
          message: "Someone is at the door"
          interruption_level: "time_sensitive"
          deep_link_destination: "securityCamera_abc123"
```

### What Happens

1. The doorbell is pressed.
2. Owner and Bob both receive a time-sensitive push notification: "Someone is at the door".
3. They tap the notification.
4. The Zendo app opens directly to the doorbell camera's live stream - they can immediately see who's there.

---

## Service Reference


| Service                             | Purpose                         | Fields                                                                                                    |
| ----------------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `zendo.list_deep_link_destinations` | List stored destinations        | (none) - returns response data                                                                            |
| `zendo.send_notification`           | Send a push notification        | `message` (required), `interruption_level` (optional), `deep_link_destination` (optional, destination ID) |


