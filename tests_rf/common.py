#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

import json
import logging
import warnings
from pathlib import Path

from serial.serialutil import SerialException
from serial.tools import list_ports

from ramses_rf import Gateway
from ramses_rf.schemas import SCH_GLOBAL_CONFIG
from ramses_rf.system import System
from tests_rf.mock import CTL_ID, MOCKED_PORT, MockDeviceCtl, MockGateway

# import tracemalloc
# tracemalloc.start()

warnings.filterwarnings("ignore", category=DeprecationWarning)

DEBUG_MODE = False
DEBUG_ADDR = "0.0.0.0"
DEBUG_PORT = 5678

if DEBUG_MODE:
    import debugpy

    if not debugpy.is_client_connected():
        debugpy.listen(address=(DEBUG_ADDR, DEBUG_PORT))
        print(f"Debugger listening on {DEBUG_ADDR}:{DEBUG_PORT}, waiting for client...")
        debugpy.wait_for_client()

logging.disable(logging.WARNING)  # usu. WARNING


TEST_DIR = Path(__file__).resolve().parent

test_ports = {MOCKED_PORT: MockGateway}
if ports := [
    c for c in list_ports.comports() if c.device[-7:-1] in ("ttyACM", "ttyUSB")
]:
    test_ports[ports[0].device] = Gateway

rf_test_failed = False  # global


def abort_if_rf_test_fails(fnc):
    """Abort all remaining RF tests once any RF test fails."""

    async def check_serial_port(test_port, *args, **kwargs):
        global rf_test_failed
        if rf_test_failed:
            raise SerialException

        try:
            await fnc(test_port, *args, **kwargs)
        except SerialException:
            rf_test_failed = True
            raise

        except AssertionError:
            rf_test_failed = True
            raise

    return check_serial_port


def find_test_tcs(gwy: Gateway) -> System:
    if isinstance(gwy, MockGateway):
        return gwy.system_by_id["01:000730"]
    systems = [s for s in gwy.systems if s.id != "01:000730"]
    return systems[0] if systems else gwy.system_by_id["01:000730"]


async def load_test_gwy(port_name, gwy_class, config_file: str, **kwargs) -> Gateway:
    """Create a system state from a packet log (using an optional configuration)."""

    config = SCH_GLOBAL_CONFIG({k: v for k, v in kwargs.items() if k[:1] != "_"})

    try:
        with open(config_file) as f:
            config.update(json.load(f))
    except FileNotFoundError:
        pass

    config = SCH_GLOBAL_CONFIG(config)

    gwy = gwy_class(port_name, **config)
    await gwy.start(start_discovery=False)  # may: SerialException

    if hasattr(
        gwy.pkt_transport.serial, "mock_devices"
    ):  # needs ser instance, so after gwy.start()
        gwy.pkt_transport.serial.mock_devices = [MockDeviceCtl(gwy, CTL_ID)]

    return gwy
