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
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
import base64
from . import Hub
from datetime import timedelta

SCAN_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile("\[(\d+)/(\d+)/(\d+)/(\d+)]")
# Models that use segment mode 0x15 with format: [0x15, 0x01, R, G, B, 0, 0, 0, 0, 0, 0xFF, 0x7F]
SEGMENTED_MODELS = [
    'H6053', 'H6072', 'H6102', 'H6199',
    # H617x series confirmed to use segment mode 0x15
    'H617A', 'H617C', 'H617E', 'H617F',
    'H6171', 'H6172', 'H6173',
]
# RGBIC models that use segment mode 0x0b (if any - kept for future use)
RGBIC_SEGMENT_MODELS = [
    'H618A', 'H618C', 'H618E', 'H618F',
    'H619A', 'H619C', 'H619E', 'H619F',
    'H61A0', 'H61A1', 'H61A2', 'H61A5', 'H61A8',
    'H61B2', 'H61C3', 'H61C5', 'H61E0', 'H61E1',
    'H6602', 'H6609'
]

class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """
    The mode in which a color change happens in.
    """
    MANUAL = 0x02           # For non-RGBIC strips
    SCENE_SELECT = 0x04     # Scene selection mode
    SCENES = 0x05           # Legacy scenes mode
    MICROPHONE = 0x06
    SEGMENTS = 0x15         # Segment mode for H617x series
    RGBIC_SEGMENT = 0x0b    # RGBIC segment mode (H618x, H619x, etc.)


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
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
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
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
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
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

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
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._is_rgbic = self._model in RGBIC_SEGMENT_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None

    @property
    def effect_list(self) -> list[str] | None:
        effect_list = []
        json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
        for categoryIdx, category in enumerate(json_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    for seffectIxd, specialEffect in enumerate(lightEffect['specialEffect']):
                        # if 'supportSku' not in specialEffect or self._model in specialEffect['supportSku']:
                        # Workaround cause we need to store some metadata in effect (effect names not unique)
                        indexes = str(categoryIdx) + "/" + str(sceneIdx) + "/" + str(leffectIdx) + "/" + str(
                            seffectIxd)
                        effect_list.append(
                            category['categoryName'] + " - " + scene['sceneName'] + ' - ' + lightEffect[
                                'scenceName'] + " [" + indexes + "]")

        return effect_list

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        # Send power on 3x for reliability on segmented models
        power_cmd = self._prepareSinglePacketData(LedCommand.POWER, [0x1])
        if self._is_segmented:
            commands = [power_cmd, power_cmd, power_cmd]
        else:
            commands = [power_cmd]

        self._state = True

        # Get brightness - either from kwargs or use stored value
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness or 255)
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = brightness

        # Get color - either from kwargs or use stored value
        if ATTR_RGB_COLOR in kwargs:
            self._attr_rgb_color = kwargs.get(ATTR_RGB_COLOR)

        rgb_color = getattr(self, '_attr_rgb_color', None) or (255, 255, 255)
        red, green, blue = rgb_color

        if self._is_segmented:
            # H617C and similar: send brightness command (3x for reliability)
            # Map HA brightness (0-255) to device range (0x14-0xFE)
            if brightness <= 0:
                device_brightness = 0x14
            else:
                device_brightness = int(0x14 + (brightness * (0xFE - 0x14) / 255))
                device_brightness = min(0xFE, max(0x14, device_brightness))
            brightness_cmd = self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [device_brightness])
            commands.extend([brightness_cmd, brightness_cmd, brightness_cmd])

            # Only send color command if user is explicitly setting color NOW
            # This prevents overwriting the device's current color when only brightness changes
            if ATTR_RGB_COLOR in kwargs:
                # Segmented models use mode 0x15 - send 3x for reliability
                color_cmd = self._prepareSinglePacketData(LedCommand.COLOR,
                                                          [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                                           0x00, 0x00, 0xFF, 0x7F])
                commands.extend([color_cmd, color_cmd, color_cmd])
        elif self._is_rgbic:
            # RGBIC models: use brightness command with mapped range
            if ATTR_BRIGHTNESS in kwargs:
                if brightness <= 0:
                    device_brightness = 0x14
                else:
                    device_brightness = int(0x14 + (brightness * (0xfe - 0x14) / 255))
                    device_brightness = min(0xfe, max(0x14, device_brightness))
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [device_brightness]))

            if ATTR_RGB_COLOR in kwargs:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.RGBIC_SEGMENT, red, green, blue, 0x00, 0x00,
                                                               0xFF, 0x7F]))
        else:
            # Non-segmented strips: use standard brightness command
            if ATTR_BRIGHTNESS in kwargs:
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))

            if ATTR_RGB_COLOR in kwargs:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if len(effect) > 0:
                search = EFFECT_PARSE.search(effect)

                # Parse effect indexes
                categoryIndex = int(search.group(1))
                sceneIndex = int(search.group(2))
                lightEffectIndex = int(search.group(3))
                specialEffectIndex = int(search.group(4))

                json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
                category = json_data['data']['categories'][categoryIndex]
                scene = category['scenes'][sceneIndex]
                lightEffect = scene['lightEffects'][lightEffectIndex]
                specialEffect = lightEffect['specialEffect'][specialEffectIndex]

                if self._is_segmented:
                    # H617C and similar: use simple scene mode command
                    # Format: 0x33 0x05 0x04 sceneCode (2 bytes little endian)
                    scene_code = scene.get('sceneCode', sceneIndex)
                    scene_cmd = self._prepareSinglePacketData(LedCommand.COLOR,
                        [LedMode.SCENE_SELECT, scene_code & 0xFF, (scene_code >> 8) & 0xFF])
                    # Send 3x for reliability
                    commands.extend([scene_cmd, scene_cmd, scene_cmd])
                    _LOGGER.info("H617C effect: using scene mode with code %d", scene_code)
                else:
                    # Standard models: use multi-packet protocol
                    for command in prepareMultiplePacketsData(0xa3,
                        array.array('B', [0x02]),
                        array.array('B',
                                    base64.b64decode(specialEffect['scenceParam'])
                                    )):
                        commands.append(command)

        # Use single connection for all commands
        client = await self._connectBluetooth()
        if client is None:
            _LOGGER.error("Failed to connect to BLE device %s", self._mac)
            return
        try:
            for command in commands:
                await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)
                if self._is_segmented:
                    await asyncio.sleep(0.15)  # Small delay between commands for reliability
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def async_turn_off(self, **kwargs) -> None:
        client = await self._connectBluetooth()
        if client is None:
            _LOGGER.error("Failed to connect to BLE device %s", self._mac)
            return
        try:
            power_off_cmd = self._prepareSinglePacketData(LedCommand.POWER, [0x0])
            # Send 3x with delays for reliability on segmented models
            if self._is_segmented:
                for _ in range(3):
                    await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, power_off_cmd, False)
                    await asyncio.sleep(0.2)
            else:
                await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, power_off_cmd, False)
            self._state = False
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _connectBluetooth(self) -> BleakClient | None:
        for i in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(BleakClient, self._ble_device, self.unique_id)
                return client
            except Exception as e:
                _LOGGER.warning("BLE connection attempt %d failed for %s: %s", i + 1, self._mac, str(e))
                continue
        _LOGGER.error("All BLE connection attempts failed for %s", self._mac)
        return None

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
