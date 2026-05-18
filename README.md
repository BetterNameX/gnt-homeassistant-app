# Zendo for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Control Home Assistant from a floor plan with [Zendo](https://myzendo.com).

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BetterNameX&repository=gnt-homeassistant-app&category=integration)

1. Open HACS in your Home Assistant instance.
2. Go to **Integrations**.
3. Click the three-dot menu in the top right and select **Custom repositories**.
4. Add `https://github.com/BetterNameX/gnt-homeassistant-app` with category **Integration**.
5. Search for **Zendo** and install it.
6. Restart Home Assistant.

### Manual

1. Copy the `custom_components/zendo` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings** > **Devices & services** > **Add integration**.
2. Search for **Zendo** and follow the setup flow.
3. Download the Zendo app from [myzendo.com/download](https://myzendo.com/download).
