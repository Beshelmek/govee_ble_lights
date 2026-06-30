from __future__ import annotations

import array
import asyncio
import logging
import re

from enum import IntEnum
import bleak_retry_connector

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store

import voluptuous as vol

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
from .govee_scene_speed import apply_scene_speed
import base64
from homeassistant.helpers.storage import Store
from . import Hub

_LOGGER = logging.getLogger(__name__)

# Per-scene speed presets, shared by all strips and persisted to HA storage so they
# survive restarts and HACS updates. Maps an effect's display name to a chosen speed
# level (int) or the string "auto" (follow Govee's default) / "off" (scene baseline).
# A scene absent from the map is treated as "auto".
_SCENE_SPEED_STORE_KEY = "govee_ble_lights_scene_speeds"
_SCENE_SPEED_VERSION = 1
_scene_speed_presets: dict[str, object] = {}
_scene_speed_store: Store | None = None
_scene_speed_loaded = False
_scene_speed_lock = asyncio.Lock()


async def _ensure_scene_presets(hass) -> None:
    """Load the shared scene-speed presets once (idempotent across all entities)."""
    global _scene_speed_store, _scene_speed_loaded
    async with _scene_speed_lock:
        if _scene_speed_loaded:
            return
        _scene_speed_store = Store(hass, _SCENE_SPEED_VERSION, _SCENE_SPEED_STORE_KEY)
        try:
            data = await _scene_speed_store.async_load()
            if isinstance(data, dict):
                _scene_speed_presets.update(data)
        except Exception as err:  # noqa: BLE001 - bad store shouldn't break the light
            _LOGGER.warning("Govee: could not load scene-speed presets: %s", err)
        _scene_speed_loaded = True


async def _save_scene_presets() -> None:
    if _scene_speed_store is not None:
        await _scene_speed_store.async_save(dict(_scene_speed_presets))

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile(r"\[(\d+)/(\d+)/(\d+)/(\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H617A', 'H617C']
PERCENT_MODELS = ['H617A']

# Number of BLE write attempts before a command is reported as failed.
MAX_COMMAND_ATTEMPTS = 3

# When the strip only accepts write-without-response, those writes are never
# ACKed: behind a BLE proxy at range a dropped packet vanishes silently and the
# scene upload never lands (the strip just replays its last frame). Pace the
# chunked packets so a distant proxy doesn't overrun and drop them. Acknowledged
# writes (Write Request) self-pace, so this only applies to the fallback path.
INTER_PACKET_DELAY = 0.02  # seconds

# A dynamic scene needs the BLE link held open for a few seconds after upload to
# commit (then it persists, like closing the Govee app). We keep the connection
# open after every command and close it only after this idle window, so we don't
# permanently hold one of the BLE proxy's limited connection slots per strip.
IDLE_DISCONNECT_SECONDS = 15

# These strips share a single Bluetooth adapter. Serialize connection
# *establishment* across all entities so concurrent connects to different
# devices don't fight over the one radio. Actual writes still run in parallel
# once each entity holds its own connection.
_CONNECT_LOCK = asyncio.Lock()

class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """
    The mode in which a color change happens in.
    
    Currently only manual is supported.
    """
    MANUAL = 0x02
    MICROPHONE = 0x06
    SCENES = 0x05
    SEGMENTS = 0x15


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])

        platform = entity_platform.async_get_current_platform()

        # This strip's current speed: an int level (0 = liveliest; higher = calmer,
        # varies per scene), "auto" (follow the scene's saved/Govee default), or
        # "off" (scene baseline). Re-applies the current effect immediately.
        speed_value = vol.Any("auto", "off", vol.Coerce(int))
        platform.async_register_entity_service(
            "set_speed",
            {vol.Required("speed_index"): speed_value},
            "async_set_speed",
        )
        # Save (or clear) the per-scene speed preset for the current effect. An int
        # pins that scene's speed everywhere; "auto" reverts it to Govee's default.
        platform.async_register_entity_service(
            "set_scene_speed",
            {vol.Required("speed_index"): vol.Any("auto", "off", vol.Coerce(int))},
            "async_set_scene_speed",
        )


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        if LightEntityFeature.EFFECT in self.supported_features_compat:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            self._brightness = brightness
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        self._state = False
        await self.hub.api.toggle_power(self.sku, self.device, 0)


class GoveeBluetoothLight(RestoreEntity, LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._use_percent = self._model in PERCENT_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None
        self._rgb_color = None
        self._effect = None

        # Serialize a single device's command sequences so the chunked packets
        # of one scene are never interleaved with another command.
        self._command_lock = asyncio.Lock()
        # Connection is held open between commands (so scenes commit) and closed
        # on an idle timer. A fresh one is established for every command.
        self._client: BleakClient | None = None
        self._idle_disconnect_cancel = None

        # Effect catalogue, parsed once off the event loop in async_added_to_hass.
        self._effect_indexes: dict[str, tuple[int, int, int, int]] = {}
        self._model_json: dict | None = None
        self._attr_effect_list = []
        # This strip's current speed setting: "auto" (follow the scene's saved/Govee
        # default), an int level, or "off" (scene baseline, no speed rewrite).
        self._speed_index: object = "auto"

    async def async_added_to_hass(self) -> None:
        """Restore prior state and load the effect catalogue (off the event loop)."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "unavailable", "unknown"):
            self._state = last_state.state == "on"
            self._brightness = last_state.attributes.get("brightness")
            rgb = last_state.attributes.get("rgb_color")
            self._rgb_color = tuple(rgb) if rgb else None
            self._effect = last_state.attributes.get("effect")
            restored_speed = last_state.attributes.get("speed_index")
            if restored_speed is not None:
                self._speed_index = restored_speed

        await _ensure_scene_presets(self.hass)

        await self._async_load_effects()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the idle timer and drop the BLE link when the entity goes away."""
        self._cancel_idle_disconnect()
        await self._disconnect()

    async def _async_load_effects(self) -> None:
        """Parse jsons/<MODEL>.json once and build a clean name -> indices map.

        The bundled catalogue lists every special-effect variant with a cryptic
        ``[c/s/le/se]`` suffix. We collapse to one entry per light-effect and
        render readable ``Category - Scene[ - Sub]`` names instead.
        """
        path = Path(__file__).parent / "jsons" / f"{self._model}.json"

        def _load() -> dict:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)

        try:
            data = await self.hass.async_add_executor_job(_load)
        except FileNotFoundError:
            _LOGGER.warning("Govee %s: no effect catalogue for model %s at %s",
                            self._mac, self._model, path)
            return
        except Exception as err:  # noqa: BLE001 - surface a bad/corrupt file, keep light usable
            _LOGGER.warning("Govee %s: failed to parse %s: %s", self._mac, path, err)
            return

        names: list[str] = []
        indexes: dict[str, tuple[int, int, int, int]] = {}

        for category_idx, category in enumerate(data.get('data', {}).get('categories', [])):
            category_name = category.get('categoryName', f"Category {category_idx}")
            for scene_idx, scene in enumerate(category.get('scenes', [])):
                scene_name = scene.get('sceneName', '').strip()
                for light_idx, light_effect in enumerate(scene.get('lightEffects', [])):
                    specials = light_effect.get('specialEffect') or []
                    if not specials:
                        continue
                    sub_name = (light_effect.get('scenceName') or '').strip()
                    label = f"{category_name} - {scene_name}" if scene_name else category_name
                    if sub_name:
                        label = f"{label} - {sub_name}"
                    # Disambiguate the rare genuine name collision.
                    display = label
                    suffix = 2
                    while display in indexes:
                        display = f"{label} ({suffix})"
                        suffix += 1
                    # Collapse variants: first special-effect carries a usable payload.
                    indexes[display] = (category_idx, scene_idx, light_idx, 0)
                    names.append(display)

        self._model_json = data
        self._effect_indexes = indexes
        self._attr_effect_list = names
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def available(self) -> bool:
        """Report unavailable when HA's Bluetooth stack can't currently see the strip."""
        if self.hass is None:
            return False
        return bluetooth.async_address_present(self.hass, self._mac.upper(), True)

    @property
    def brightness(self):
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def effect(self) -> str | None:
        return self._effect

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]

        new_brightness = self._brightness
        new_rgb = self._rgb_color
        new_effect = self._effect

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            if self._use_percent:
                brightnessPercent = int(brightness * 100 / 255)
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightnessPercent]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))
            new_brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                                               0x00, 0x00, 0xFF, 0x7F]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))

            new_rgb = (red, green, blue)
            new_effect = None

        if ATTR_EFFECT in kwargs and kwargs.get(ATTR_EFFECT):
            effect = kwargs[ATTR_EFFECT]
            scene_commands = self._scene_commands(effect)
            if scene_commands is None:
                _LOGGER.warning("Govee %s: unknown effect %r", self._mac, effect)
            else:
                commands.extend(scene_commands)
                new_effect = effect

        # All packets for this command go out over a single BLE session.
        await self._send_commands(commands)

        # Only reflect new state once the writes actually succeeded.
        self._state = True
        self._brightness = new_brightness
        self._rgb_color = new_rgb
        self._effect = new_effect
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._send_commands([self._prepareSinglePacketData(LedCommand.POWER, [0x0])])
        self._state = False
        self.async_write_ha_state()

    def _scene_commands(self, effect: str) -> list | None:
        """Resolve an effect name to its chunked BLE scene packets, or None."""
        indices = self._effect_indexes.get(effect)
        if indices is None:
            # Backward compatibility with the old "... [c/s/le/se]" names.
            search = EFFECT_PARSE.search(effect)
            if not search:
                return None
            indices = tuple(int(search.group(i)) for i in range(1, 5))

        if self._model_json is None:
            _LOGGER.warning("Govee %s: effect catalogue not loaded yet", self._mac)
            return None

        category_idx, scene_idx, light_idx, special_idx = indices
        try:
            scene = (self._model_json['data']['categories'][category_idx]
                     ['scenes'][scene_idx])
            light_effect = scene['lightEffects'][light_idx]
            specials = light_effect['specialEffect']
            # Prefer the variant whose supportSku matches this model: its blob is
            # the same bytes, but it carries the per-model speedInfo we apply below.
            special_effect = next(
                (se for se in specials
                 if self._model in (se.get('supportSku') or [])),
                specials[special_idx])
            param = base64.b64decode(special_effect['scenceParam'])
        except (KeyError, IndexError, ValueError) as err:
            _LOGGER.warning("Govee %s: bad effect data for %r: %s", self._mac, effect, err)
            return None

        # Apply the resolved speed level by rewriting the speed bytes in the blob the
        # way the Govee app does. resolve returns "off" (upload baseline untouched),
        # "auto" (use Govee's per-scene defaultIndex), or an int level. See
        # govee_scene_speed for the byte-level details.
        speed_info = special_effect.get('speedInfo') or {}
        if speed_info.get('supSpeed') and speed_info.get('config'):
            level = self._resolve_speed(effect)
            if level != "off":
                override = None if level == "auto" else level
                param = apply_scene_speed(param, speed_info['config'], override)

        packets = list(prepareMultiplePacketsData(0xa3,
                                                  array.array('B', [0x02]),
                                                  array.array('B', param)))

        # The 0xa3 upload only loads the scene into the strip's memory; it does
        # NOT switch the strip out of manual-color mode. The strip plays the
        # scene only after this "activate scene" packet:
        #   33 05 04 <sceneCodeLo> <sceneCodeHi>   (sceneCode is 2-byte little-endian)
        # Reverse-engineered live on H617A (2026-06-29). Without it, scenes never
        # animate; with it, every scene works. The sceneCode lives per lightEffect
        # (falling back to the scene's own code).
        scene_code = light_effect.get('sceneCode')
        if scene_code is None:
            scene_code = scene.get('sceneCode')
        if scene_code is not None:
            packets.append(self._prepareSinglePacketData(
                LedCommand.COLOR,
                [0x04, scene_code & 0xFF, (scene_code >> 8) & 0xFF]))
        else:
            _LOGGER.warning("Govee %s: effect %r has no sceneCode; it will upload "
                            "but may not activate", self._mac, effect)

        _LOGGER.debug("Govee %s: scene %r -> %d byte param -> %d packets (code %s)",
                      self._mac, effect, len(param), len(packets), scene_code)
        return packets

    async def _send_commands(self, commands: list) -> None:
        """Send all packets for one command over a single, freshly-established session.

        Two hard-won constraints, both confirmed on H617A hardware behind an
        ESPHome BLE proxy:

        1. All packets of a command must ride ONE connection, and for a dynamic
           scene the link must stay OPEN for several seconds after the upload or
           the strip never commits it (disconnecting ~2s later discards it; once
           committed it persists like closing the Govee app). So we keep the
           connection open and close it later on an idle timer.
        2. We must never *reuse* a held-open connection for the next command:
           the strip drops idle links and the proxy keeps reporting
           ``is_connected == True``, so write-without-response packets (which are
           never ack'd) vanish silently. So every command establishes a fresh
           connection, closing any prior held one first.
        """
        async with self._command_lock:
            self._cancel_idle_disconnect()
            await self._disconnect()  # never write to a possibly-stale held link

            last_err: Exception | None = None
            for attempt in range(1, MAX_COMMAND_ATTEMPTS + 1):
                try:
                    self._client = await self._connect()
                    # Prefer acknowledged writes (Write Request) when the strip
                    # offers them: at range a write-without-response packet is
                    # dropped silently, so a scene upload never lands and the
                    # strip replays its old frame. An ACKed write raises on a
                    # dropped packet, so the retry loop below actually re-sends
                    # instead of silently no-op'ing. Fall back to write-without-
                    # response (paced) when the characteristic can't do requests.
                    char = self._client.services.get_characteristic(UUID_CONTROL_CHARACTERISTIC)
                    use_response = char is not None and "write" in char.properties
                    target = char if char is not None else UUID_CONTROL_CHARACTERISTIC
                    _LOGGER.debug("Govee %s: connected=%s, write_response=%s, sending %d packets (%d bytes total)",
                                  self._mac, self._client.is_connected, use_response, len(commands),
                                  sum(len(c) for c in commands))
                    for idx, command in enumerate(commands):
                        await self._client.write_gatt_char(target, command, response=use_response)
                        _LOGGER.debug("Govee %s: wrote packet %d/%d (%d bytes)",
                                      self._mac, idx + 1, len(commands), len(command))
                        # Pace the unacknowledged fallback so a distant proxy
                        # doesn't overrun; ACKed writes already self-pace.
                        if not use_response and idx + 1 < len(commands):
                            await asyncio.sleep(INTER_PACKET_DELAY)
                    # Leave the link open so the strip can commit, then auto-close.
                    self._schedule_idle_disconnect()
                    return
                except Exception as err:  # noqa: BLE001 - BLE stacks raise a zoo of errors
                    last_err = err
                    _LOGGER.warning("Govee %s: BLE command failed (attempt %d/%d): %s",
                                    self._mac, attempt, MAX_COMMAND_ATTEMPTS, err)
                    await self._disconnect()
            raise HomeAssistantError(
                f"Govee {self._mac}: BLE command failed after {MAX_COMMAND_ATTEMPTS} attempts: {last_err}")

    async def _connect(self) -> BleakClient:
        """Establish a fresh BLE connection to the strip."""
        # Always re-fetch the device handle; a cached one goes stale across
        # adapter changes and re-advertisements.
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self._mac.upper(), True)
        if ble_device is None:
            raise HomeAssistantError(f"Govee {self._mac} is not visible to Home Assistant's Bluetooth")
        self._ble_device = ble_device

        # Serialize connection establishment across all strips on the shared radio.
        async with _CONNECT_LOCK:
            client = await bleak_retry_connector.establish_connection(
                BleakClient, ble_device, self.unique_id,
            )
            _LOGGER.debug("Govee %s: established connection to %s (connectable device)",
                          self._mac, ble_device.address)
            return client

    def _schedule_idle_disconnect(self) -> None:
        """(Re)arm the timer that closes the held-open link after an idle window."""
        self._cancel_idle_disconnect()
        self._idle_disconnect_cancel = async_call_later(
            self.hass, IDLE_DISCONNECT_SECONDS, self._async_idle_disconnect)

    def _cancel_idle_disconnect(self) -> None:
        if self._idle_disconnect_cancel is not None:
            self._idle_disconnect_cancel()
            self._idle_disconnect_cancel = None

    async def _async_idle_disconnect(self, _now) -> None:
        self._idle_disconnect_cancel = None
        async with self._command_lock:
            # If a command re-armed the timer while we waited for the lock, a newer
            # connection is in use — don't tear it down.
            if self._idle_disconnect_cancel is None:
                await self._disconnect()

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    @property
    def extra_state_attributes(self) -> dict:
        """Surface the speed settings: this strip's level, the current scene's saved
        preset, and the level that actually resolves for the current effect."""
        scene_preset = (_scene_speed_presets.get(self._effect, "auto")
                        if self._effect else None)
        return {
            "speed_index": self._speed_index,
            "scene_speed_preset": scene_preset,
            "resolved_speed": self._resolve_speed(self._effect) if self._effect else None,
        }

    def _resolve_speed(self, effect: str | None) -> object:
        """Resolve the effective speed for an effect: an int level, "auto" (Govee
        default), or "off" (baseline). Precedence: this strip's pinned level >
        the scene's saved preset > "auto"."""
        if isinstance(self._speed_index, int) or self._speed_index == "off":
            return self._speed_index
        # strip is on "auto" -> defer to the scene's saved preset (default "auto")
        if effect is None:
            return "auto"
        return _scene_speed_presets.get(effect, "auto")

    async def async_set_speed(self, speed_index) -> None:
        """Set THIS strip's current speed and re-apply the current effect now."""
        self._speed_index = speed_index
        if self._effect:
            await self.async_turn_on(effect=self._effect)
        self.async_write_ha_state()

    async def async_set_scene_speed(self, speed_index) -> None:
        """Save (or clear) the current scene's speed preset for all strips.

        An int pins that scene's speed; "auto" removes the preset (Govee default).
        The preset only takes visible effect where a strip's own speed is "auto".
        """
        if self._effect is None:
            _LOGGER.warning("Govee %s: set_scene_speed with no active effect", self._mac)
            return
        if speed_index == "auto":
            _scene_speed_presets.pop(self._effect, None)
        else:
            _scene_speed_presets[self._effect] = speed_index
        await _save_scene_presets()
        # Re-apply here so the change is visible immediately on this strip too.
        if self._speed_index == "auto":
            await self.async_turn_on(effect=self._effect)
        self.async_write_ha_state()

    def _prepareSinglePacketData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([checksum & 0xFF])
        return frame
