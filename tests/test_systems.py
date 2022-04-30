#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Test the payload parsers and corresponding output (schema, traits, params, status).
"""

from pathlib import Path, PurePath

import pytest

from ramses_rf import Gateway
from ramses_rf.protocol.message import Message
from ramses_rf.protocol.packet import Packet
from tests.common import (
    TEST_DIR,
    assert_expected,
    assert_expected_set,
    load_expected_results,
    load_test_system,
    shuffle_dict,
)

WORK_DIR = f"{TEST_DIR}/systems"


@pytest.fixture
async def gwy() -> Gateway:  # NOTE: async to get running loop
    gwy = Gateway("/dev/null", config={})
    gwy.config.disable_sending = True
    return gwy


def id_fnc(param):
    return PurePath(param).name


def pytest_generate_tests(metafunc):
    folders = [f for f in Path(WORK_DIR).iterdir() if f.is_dir()]
    metafunc.parametrize("dir_name", folders, ids=id_fnc)


def test_packets_from_log_file(gwy, dir_name):
    def proc_log_line(gwy, pkt_line):
        pkt_line, pkt_dict = pkt_line.split("#", maxsplit=1)

        if not pkt_line[27:].strip():
            return

        msg = Message(gwy, Packet.from_file(gwy, pkt_line[:26], pkt_line[27:]))

        assert msg.payload == eval(pkt_dict)

    with open(f"{dir_name}/packet.log") as f:
        while line := (f.readline()):
            if line.strip():
                proc_log_line(gwy, line)


async def test_systems_from_log_file(dir_name):

    expected: dict = load_expected_results(dir_name)
    gwy: Gateway = await load_test_system(dir_name)

    gwy.serial_port = "/dev/null"  # HACK: needed to pause engine
    schema, _ = gwy._get_state(include_expired=True)

    assert_expected(schema, expected.get("schema"))
    assert_expected_set(gwy, expected)


async def test_shuffle_from_log_file(dir_name):

    expected: dict = load_expected_results(dir_name)
    gwy: Gateway = await load_test_system(dir_name)

    gwy.serial_port = "/dev/null"  # HACK: needed to pause engine
    _, packets = gwy._get_state(include_expired=True)

    packets = shuffle_dict(packets)
    await gwy._set_state(packets)

    assert_expected_set(gwy, expected)


async def test_schemax_with_log_file(dir_name):

    expected: dict = load_expected_results(dir_name)
    gwy: Gateway = await load_test_system(dir_name, expected["schema"])

    assert_expected_set(gwy, expected)
