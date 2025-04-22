from __future__ import annotations

import array
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

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
import base64
from . import Hub

_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile("\[(\d+)/(\d+)/(\d+)/(\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199']

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


# H6001 uses the same BLE commands as other non-segmented models.
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
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]

        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                                               0x00, 0x00, 0xFF, 0x7F]))
            else:
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

                # Prepare packets to send big payload in separated chunks
                for command in prepareMultiplePacketsData(0xa3,
                                                          array.array('B', [0x02]),
                                                          array.array('B',
                                                                      base64.b64decode(specialEffect['scenceParam'])
                                                                      )):
                    commands.append(command)

        for command in commands:
            client = await self._connectBluetooth()
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)

    async def async_turn_off(self, **kwargs) -> None:
        client = await self._connectBluetooth()
        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC,
                                     self._prepareSinglePacketData(LedCommand.POWER, [0x0]), False)
        self._state = False

    async def _connectBluetooth(self) -> BleakClient:
        for i in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(BleakClient, self._ble_device, self.unique_id)
                return client
            except:
                continue

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
