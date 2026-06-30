# Govee BLE Lighting Integration for HomeAssistant

![Govee Logo](assets/govee-logo.png)

A powerful and seamless integration to control your Govee lighting devices via Govee API or BLE directly from HomeAssistant with full features support.

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Support & Contribution](#support--contribution)
- [License](#license)

---

## Features

- 🚀 **Direct BLE Control**: No need for middlewares or bridges. Connect and control your Govee devices directly through Bluetooth Low Energy.

- ☁️ **API Control**: Supported all light devices with full features support including scenes!

- 🌈 **Scene Selection**: Leverage the full potential of your Govee lights by choosing from all available scenes, transforming the ambiance of your room instantly.
  
- 💡 **Comprehensive Lighting Control**: Adjust brightness, change colors, or switch on/off with ease.

---

## Fork additions: H617A dynamic scenes over BLE

This fork adds working **dynamic scene** support for the **H617A** RGBIC strip over
local BLE, plus a more resilient BLE transport. Changes live in
`custom_components/govee-ble-lights/light.py` and add the scene catalogue
`jsons/H617A.json` (payload-identical to the already-bundled `H617C.json`).

### The missing "activate scene" command

On the H617A, uploading a scene's `scenceParam` with the `0xa3` multi-packet
protocol only *loads* the scene into the strip's memory — it does **not** switch
the strip out of manual-colour mode, so the scene never plays. The strip starts
animating only after an explicit **activate** packet:

```
33 05 04 <sceneCodeLo> <sceneCodeHi>      # sceneCode is 2-byte little-endian
```

`sceneCode` comes from the model JSON per light-effect. So setting a scene is two
steps over one BLE session: **(1)** send the `0xa3` `scenceParam` upload, **(2)**
send the activate packet. This was reverse-engineered live on H617A hardware;
~25 scenes across all nine categories animate correctly from the standard
Home Assistant effect dropdown.

### BLE transport hardening

- All packets of a command ride **one** freshly-established connection; the link
  is held open briefly so a scene upload commits, then closed on an idle timer
  (so it doesn't hog a Bluetooth-proxy connection slot).
- A held connection is never reused (an idle strip behind an ESPHome BLE proxy
  keeps reporting `is_connected == True` while silently dropping
  write-without-response packets).
- Per-entity command lock + a module-level connect lock serialise access to the
  shared adapter; `available` reflects the BT stack; state survives restarts via
  `RestoreEntity`. Effect names are rendered readably (`Category - Scene`).

### Scene speed control

There is **no separate BLE "speed" command**. The Govee app sets a scene's speed
by *rewriting bytes inside the `scenceParam` blob* before uploading it: it decodes
the effect segments and overwrites their colour/move/brightness timing fields with
values drawn from the scene's own `speedInfo` lookup table at a chosen index. This
fork reproduces that rewrite (`govee_scene_speed.py`), so scene speed is adjustable
from Home Assistant. The speed tables already live in the bundled catalogue, so no
extra capture is needed; an unrecognised blob is uploaded untouched.

Two entity services are exposed (target a Govee light entity):

- **`govee-ble-lights.set_speed`** — set *this strip's* speed: an integer level
  (`0` = liveliest; higher = calmer, range varies per scene), `"auto"` (default —
  follow the scene's saved/Govee-recommended speed), or `"off"` (upload the scene
  baseline untouched). Re-applies the current effect immediately.
- **`govee-ble-lights.set_scene_speed`** — save (or clear) a per-scene speed
  preset for the current effect. An integer pins that scene's speed for every strip
  that's on `"auto"`; `"auto"` clears the preset back to Govee's default. Presets
  persist to Home Assistant storage (survive restarts and HACS updates).

Resolution precedence: **strip's pinned level → saved scene preset → `auto`
(Govee's per-scene `defaultIndex`)**. Each light exposes `speed_index`,
`scene_speed_preset` and `resolved_speed` attributes so the active level is visible.

> Speed "feel" is per-scene: the byte's direction and range depend on the scene's
> colour type, so the same level looks different across scenes. Inherently flashing
> scenes (e.g. fireworks) still flicker even at their calmest level.

---

## Configuration

### What is needed

For Direct BLE Control:
- Before you begin, make certain HomeAssistant can access BLE on your platform. Ensure your HomeAssistant instance is granted permissions to utilize the Bluetooth Low Energy of your host machine.

For Govee API Control:
- Retrieve Govee-API-Key as described [here](https://developer.govee.com/reference/apply-you-govee-api-key), setup integration with API type ad fill your API key.

## Usage

With the integration setup, your Govee devices will appear as entities within HomeAssistant. All you need to do is select your device model when adding it.

---

## Troubleshooting for BLE

If you're facing issues with the integration, consider the following steps:

1. **Check BLE Connection**: 
   
   Ensure that the Govee device is within the Bluetooth range of your HomeAssistant host machine.

2. **Model Check**:

   Check that you selected correct device model.

3. **Logs**:

   HomeAssistant logs can provide insights into any issues. Navigate to `Configuration > Logs` to review any error messages related to the Govee integration.

---

## Support & Contribution

- **Found an Issue?** 
   
   Raise it in the [Issues section](https://github.com/Beshelmek/govee_ble_lights/issues) of this repository.

- **Device support**:

   Almost every Govee device has its own BLE message protocol. If you have an Android smartphone and your device is not supported, please contact me on [Telegram](https://t.me/Beshelmek).

- **Contributions**:

   We welcome community contributions! If you'd like to improve the integration or add new features, please fork the repository and submit a pull request.

---

## Future Plans

We aim to continuously improve this integration by:

- Supporting more Govee device models for BLE
- Enhancing the overall user experience and stability

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.
