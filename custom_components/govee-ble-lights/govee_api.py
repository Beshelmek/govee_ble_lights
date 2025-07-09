import requests
import asyncio

import uuid


class GoveeAPI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://openapi.api.govee.com/router/api/v1"
        self.headers = {
            "Govee-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    async def list_devices(self):
        url = f"{self.base_url}/user/devices"
        response = await asyncio.to_thread(requests.get, url, headers=self.headers)
        return response.json()['data']

    async def list_scenes(self, sku: str, device: str):
        url = f"{self.base_url}/device/scenes"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
            }
        })
        return response.json()['payload']['capabilities'][0]['parameters']['options']

    async def toggle_power(self, sku: str, device: str, value: int):
        url = f"{self.base_url}/device/control"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': {
                    'type': 'devices.capabilities.on_off',
                    'instance': 'powerSwitch',
                    'value': value
                }
            }
        })
        return response.json()

    async def get_device_state(self, sku: str, device: str):
        url = f"{self.base_url}/device/state"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device
            }
        })
        return response.json()['payload']

    async def set_color_rgb(self, sku: str, device: str, r: int, g: int, b: int):
        url = f"{self.base_url}/device/control"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': {
                    'type': 'devices.capabilities.color_setting',
                    'instance': 'colorRgb',
                    'value': ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | ((b & 0xFF) << 0)
                }
            }
        })
        return response.json()

    async def set_color_temp(self, sku: str, device: str, kelvin: int):
        url = f"{self.base_url}/device/control"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': {
                    'type': 'devices.capabilities.color_setting',
                    'instance': 'colorTemperatureK',
                    'value': kelvin
                }
            }
        })
        return response.json()

    async def set_brightness(self, sku: str, device: str, value: int):
        url = f"{self.base_url}/device/control"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': {
                    'type': 'devices.capabilities.range',
                    'instance': 'brightness',
                    'value': value
                }
            }
        })
        return response.json()

    async def set_scene(self, sku: str, device: str, value: object):
        url = f"{self.base_url}/device/control"
        response = await asyncio.to_thread(requests.post, url, headers=self.headers, json={
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': {
                    'type': 'devices.capabilities.dynamic_scene',
                    'instance': 'lightScene',
                    'value': value
                }
            }
        })
        return response.json()
