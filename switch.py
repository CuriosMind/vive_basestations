"""
Custom Vive Base Station switch platform for Home Assistant.

Place this file in:
  custom_components/vive_basestations/switch.py
alongside a minimal manifest.json.

Dependencies:
  - bleak
  - async_timeout
Optionally:
  - openvr (if you're still using it)
"""

import logging
import time
import asyncio
import async_timeout

from typing import Dict, Optional
from bleak import BleakScanner, BleakClient

# Home Assistant imports
import voluptuous as vol
from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.const import CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv

# If you want to handle config from configuration.yaml
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_SCAN_INTERVAL, default=30): cv.positive_int
})

_LOGGER = logging.getLogger(__name__)


class BaseStationDriver:
    """
    Manages scanning, connecting, and controlling Vive Base Stations (V1/V2).
    """

    # V1 Base Station UUIDs
    V1_UUIDS = {
        'CONTROL': "0000cb01-0000-1000-8000-00805f9b34fb",
    }

    # Example known V1 device IDs (MAC -> hex string)
    V1_DEVICE_IDS = {
        "74:F6:1C:46:9A:7D": "D49918F9",
        "74:F6:1C:46:9A:A0": "120A80FC"
    }

    # V2 Base Station UUIDs
    V2_UUIDS = {
        'POWER': "00001525-1212-efde-1523-785feabcd124",
    }

    def __init__(self):
        self.known_stations: Dict[str, dict] = {}  # address -> {'name', 'version', 'is_on'}
        self.current_clients: Dict[str, BleakClient] = {}
        self.loop = asyncio.get_event_loop()
        self._lock = asyncio.Lock()  # guard scanning/connect

    async def scan_for_base_stations(self, timeout=5.0):
        """Scan for available Vive Base Stations."""
        async with self._lock:
            _LOGGER.debug("Starting BLE scan for base stations...")
            devices = await BleakScanner.discover(timeout=timeout)

            found_stations = []
            for dev in devices:
                if not dev.name:
                    continue
                if "HTC BS" in dev.name:
                    version = "V1"
                elif "LHB-" in dev.name:
                    version = "V2"
                else:
                    continue

                found_stations.append(dev)
                # Store basic info
                self.known_stations[dev.address] = {
                    'name': dev.name,
                    'version': version,
                    'is_on': False
                }
                _LOGGER.info("Discovered %s base station: %s (%s)",
                             version, dev.name, dev.address)
            return found_stations

    async def connect_to_base_station(self, address: str) -> Optional[BleakClient]:
        """Connect to a specific base station by address."""
        if address in self.current_clients:
            client = self.current_clients[address]
            if client.is_connected:
                return client

        client = BleakClient(address, timeout=20.0)
        try:
            await client.connect()
            self.current_clients[address] = client
            _LOGGER.info("Connected to base station %s", address)
            return client
        except Exception as e:
            _LOGGER.error("Error connecting to %s: %s", address, e)
            return None

    async def disconnect_from_all(self):
        """Disconnect from all connected base stations."""
        for client in self.current_clients.values():
            try:
                if client.is_connected:
                    await client.disconnect()
            except:
                pass
        self.current_clients.clear()

    async def send_power_command(self, address: str, power_on: bool) -> bool:
        """Send power command to one base station."""
        station = self.known_stations.get(address)
        if not station:
            _LOGGER.error("Unknown base station: %s", address)
            return False

        # Ensure we have a connected client
        client = await self.connect_to_base_station(address)
        if not client:
            return False

        version = station['version']
        name = station['name']

        try:
            if version == 'V2':
                # V2 uses the 'POWER' characteristic
                char_uuid = self.V2_UUIDS['POWER']
                command = b'\x01' if power_on else b'\x00'
                await client.write_gatt_char(char_uuid, command, response=True)

            else:  # V1
                device_id = self.V1_DEVICE_IDS.get(address)
                if not device_id:
                    _LOGGER.error("No device ID found for V1 base station %s (%s)", name, address)
                    return False

                cmd_id = 0x00 if power_on else 0x02
                cmd_timeout = 0 if power_on else 1
                cmd = self.make_v1_command(device_id, cmd_id, cmd_timeout)
                if cmd is None:
                    return False
                await client.write_gatt_char(self.V1_UUIDS['CONTROL'], cmd, response=True)

            station['is_on'] = power_on
            _LOGGER.info("Base station %s set to %s", address, "ON" if power_on else "OFF")
            return True
        except Exception as e:
            _LOGGER.error("Error sending power command to %s (%s): %s", name, address, e)
            return False

    async def get_power_state(self, address: str) -> Optional[bool]:
        """Return current power state for station or None if error."""
        station = self.known_stations.get(address)
        if not station:
            return None

        client = await self.connect_to_base_station(address)
        if not client:
            return None

        version = station['version']
        try:
            if version == 'V2':
                data = await client.read_gatt_char(self.V2_UUIDS['POWER'])
                return data[0] == 0x01
            else:
                # V1 read
                data = await client.read_gatt_char(self.V1_UUIDS['CONTROL'])
                # Some V1 stations have different statuses. If data[0] != 0, it's likely ON.
                # This is simplified logic.
                return data[0] != 0x00
        except Exception as e:
            _LOGGER.error("Error reading power state for %s: %s", address, e)
            return None

    def make_v1_command(self, device_id: str, cmd_id: int, cmd_timeout: int = 0) -> Optional[bytearray]:
        """Create the V1 command packet (wake/sleep)."""
        try:
            device_id = device_id.replace('0x', '')
            device_id_int = int(device_id, 16)
            cmd = bytearray()
            cmd += (0x12).to_bytes(1, 'big')         # Wake prefix
            cmd += cmd_id.to_bytes(1, 'big')         # Command ID
            cmd += (cmd_timeout).to_bytes(2, 'big')  # Timeout
            cmd += device_id_int.to_bytes(4, 'little')
            cmd += (0).to_bytes(12, 'big')           # Padding
            return cmd
        except Exception as ex:
            _LOGGER.error("Failed to create V1 command: %s", ex)
            return None


class ViveBaseStationSwitch(SwitchEntity):
    """A Home Assistant Switch entity for controlling a Vive base station."""

    def __init__(self, driver: BaseStationDriver, address: str):
        self._driver = driver
        self._address = address
        self._name = driver.known_stations[address]['name']
        self._version = driver.known_stations[address]['version']
        self._is_on = driver.known_stations[address]['is_on']
        self._unique_id = f"vive_bs_{address.replace(':','_')}"

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def is_on(self):
        return self._is_on

    async def async_turn_on(self, **kwargs):
        success = await self._driver.send_power_command(self._address, True)
        if success:
            self._is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        success = await self._driver.send_power_command(self._address, False)
        if success:
            self._is_on = False
            self.async_write_ha_state()

    async def async_update(self):
        """Poll the base station's actual state."""
        state = await self._driver.get_power_state(self._address)
        if state is not None:
            self._is_on = state


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """
    Setup the Vive Base Station switch platform.

    Example YAML config:

    switch:
      - platform: vive_basestations
        scan_interval: 30
    """
    scan_interval = config.get(CONF_SCAN_INTERVAL, 30)

    _LOGGER.info("Setting up Vive Base Stations with scan interval=%s", scan_interval)

    driver = BaseStationDriver()
    # Scan once for devices
    await driver.scan_for_base_stations(timeout=5.0)

    # Create a switch entity for each discovered station
    entities = []
    for addr in driver.known_stations.keys():
        entities.append(ViveBaseStationSwitch(driver, addr))

    # Add them to HA
    async_add_entities(entities, update_before_add=True)

    # Optionally: set up a background task to keep them connected or re-scan
    # For example, re-scan or poll states periodically:
    # However, each switch will do an `async_update` on HA's schedule anyway.
    # So typically you rely on the normal entity update intervals.

    return True
