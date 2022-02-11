#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Decode/process a message (payload into JSON).
"""

import logging
import re
from datetime import timedelta as td
from functools import lru_cache
from typing import Any, Optional

from .address import Address
from .exceptions import InvalidPacketError, InvalidPayloadError
from .packet import fraction_expired
from .parsers import PAYLOAD_PARSERS, parser_unknown
from .ramses import CODE_IDX_COMPLEX, CODE_RQ_COMPLEX, RAMSES_CODES

# skipcq: PY-W2000
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    __dev_mode__,
)

# skipcq: PY-W2000
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    _0001,
    _0002,
    _0004,
    _0005,
    _0006,
    _0008,
    _0009,
    _000A,
    _000C,
    _000E,
    _0016,
    _0100,
    _0150,
    _01D0,
    _01E9,
    _0404,
    _0418,
    _042F,
    _0B04,
    _1030,
    _1060,
    _1081,
    _1090,
    _1098,
    _10A0,
    _10B0,
    _10E0,
    _10E1,
    _1100,
    _1260,
    _1280,
    _1290,
    _1298,
    _12A0,
    _12B0,
    _12C0,
    _12C8,
    _12F0,
    _1300,
    _1F09,
    _1F41,
    _1FC9,
    _1FD0,
    _1FD4,
    _2249,
    _22C9,
    _22D0,
    _22D9,
    _22F1,
    _22F3,
    _2309,
    _2349,
    _2389,
    _2400,
    _2401,
    _2410,
    _2420,
    _2D49,
    _2E04,
    _30C9,
    _3120,
    _313F,
    _3150,
    _31D9,
    _31DA,
    _31E0,
    _3200,
    _3210,
    _3220,
    _3221,
    _3223,
    _3B00,
    _3EF0,
    _3EF1,
    _PUZZ,
)

__all__ = ["Message"]

HVAC_ONLY_CODES = (_1298, _12A0, _12C8, _22F1, _22F3, _31D9, _31DA, _31E0)

CODE_NAMES = {k: v["name"] for k, v in RAMSES_CODES.items()}

MSG_FORMAT_10 = "|| {:10s} | {:10s} | {:2s} | {:16s} | {:^4s} || {}"
MSG_FORMAT_18 = "|| {:18s} | {:18s} | {:2s} | {:16s} | {:^4s} || {}"

DEV_MODE = __dev_mode__  # and False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


class Message:
    """The message class; will trap/log all invalid MSGs appropriately."""

    CANT_EXPIRE = -1
    IS_EXPIRING = 0.8  # expected lifetime == 1.0
    HAS_EXPIRED = 2.0  # incl. any value >= HAS_EXPIRED

    def __init__(self, gwy, pkt) -> None:
        """Create a message from a valid packet.

        Will raise InvalidPacketError if it is invalid.
        """
        self._gwy = gwy
        self._pkt = pkt

        self.src = pkt.src
        self.dst = pkt.dst
        self._addrs = pkt.addrs

        self.dtm = pkt.dtm

        self.verb = pkt.verb
        self.seqn = pkt.seqn
        self.code = pkt.code
        self.len = pkt.len

        self.code_name = CODE_NAMES.get(self.code, f"unknown_{self.code}")

        self._payload = self._validate(self._pkt.payload)  # ? raise InvalidPacketError

        self._str = None
        self._fraction_expired = None
        self._is_fragment = None

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        return repr(self._pkt)  # or str?

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""

        def ctx(pkt) -> str:
            ctx = {True: "[..]", False: "", None: "??"}.get(pkt._ctx, pkt._ctx)
            if not ctx and pkt.payload[:2] not in ("00", "FF"):
                return f"({pkt.payload[:2]})"
            return ctx

        def display_name(addr: Address) -> str:  # TODO: needs caching
            name = None
            if self._gwy._include.get(addr.id):
                if self._gwy.config.use_aliases:
                    name = self._gwy._include[addr.id].get("alias")
                if not name and (klass := self._gwy._include[addr.id].get("class")):
                    name = f"{klass}:{addr.id[3:]}"
            return (name or Address._friendly(addr.id))[:18]  # HACK

        if self._str is not None:
            return self._str

        if self.src.id == self._addrs[0].id:
            name_0 = display_name(self.src)
            name_1 = "" if self.dst is self.src else display_name(self.dst)
        else:
            name_0 = ""
            name_1 = display_name(self.src)

        _format = MSG_FORMAT_18 if self._gwy.config.use_aliases else MSG_FORMAT_10
        self._str = _format.format(
            name_0, name_1, self.verb, self.code_name, ctx(self._pkt), self.payload
        )
        return self._str

    def __eq__(self, other) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return (self.src, self.dst, self.verb, self.code, self._pkt.payload) == (
            other.src,
            other.dst,
            other.verb,
            other.code,
            other._pkt.payload,
        )

    def __lt__(self, other) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return self.dtm < other.dtm

    @property
    def payload(self) -> Any:  # Any[dict, List[dict]]:
        """Return the payload."""
        return self._payload

    @property
    def _has_payload(self) -> bool:
        """Return False if there is no payload (may falsely Return True).

        The message (i.e. the raw payload) may still have an idx.
        """

        return self._pkt._has_payload

    @property
    def _has_array(self) -> bool:
        """Return True if the message's raw payload is an array."""

        return self._pkt._has_array

    @property
    def _idx(self) -> Optional[dict]:
        """Return the zone_idx/domain_id of a message payload, if any.

        Used to identify the zone/domain that a message applies to. Returns an empty
        dict if there is none such, or None if undetermined.
        """

        #  I --- 01:145038 --:------ 01:145038 3B00 002 FCC8

        IDX_NAMES = {
            _0002: "other_idx",  # non-evohome: hometronics
            _0418: "log_idx",  # can be 2 DHW zones per system
            _10A0: "dhw_idx",  # can be 2 DHW zones per system
            _22C9: "ufh_idx",  # UFH circuit
            _2389: "other_idx",  # anachronistic
            _2D49: "other_idx",  # non-evohome: hometronics
            _31D9: "hvac_id",
            _31DA: "hvac_id",
            _3220: "msg_id",
        }  # ALSO: "domain_id", "zone_idx"

        if self._pkt._idx in (True, False) or self.code in CODE_IDX_COMPLEX:
            return {}  # above was: CODE_IDX_COMPLEX + [_3150]:

        if self.code in (_3220,):  # FIXME: should be _SIMPLE
            return {}

        #  I 068 03:201498 --:------ 03:201498 30C9 003 0106D6 # rare

        #  I --- 00:034798 --:------ 12:126457 2309 003 0201F4
        if not {self.src.type, self.dst.type} & {
            "01",
            "02",
            "03",  # ?remove (see above, rare)
            "12",
            "18",
            "22",
            "23",
        }:  # DEX
            assert self._pkt._idx == "00", "What!! (00)"
            return {}

        #  I 035 --:------ --:------ 12:126457 30C9 003 017FFF
        if self.src.type == self.dst.type and self.src.type not in (
            "01",
            "02",
            "03",  # ?remove (see above, rare)
            "18",
            "23",
        ):  # DEX
            assert self._pkt._idx == "00", "What!! (01)"
            return {}

        #  I --- 04:029362 --:------ 12:126457 3150 002 0162
        # if not getattr(self.src, "_is_controller", True) and not getattr(
        #     self.dst, "_is_controller", True
        # ):
        #     assert self._pkt._idx == "00", "What!! (10)"
        #     return {}

        #  I --- 04:029362 --:------ 12:126457 3150 002 0162
        # if not (
        #     getattr(self.src, "_is_controller", True)
        #     or getattr(self.dst, "_is_controller", True)
        # ):
        #     assert self._pkt._idx == "00", "What!! (11)"
        #     return {}

        if self.src.type == self.dst.type and not getattr(
            self.src, "_is_controller", True
        ):  # DEX
            assert self._pkt._idx == "00", "What!! (12)"
            return {}

        index_name = IDX_NAMES.get(
            self.code, "domain_id" if self._pkt._idx[:1] == "F" else "zone_idx"
        )

        return {index_name: self._pkt._idx}

    @property
    def _expired(self) -> bool:
        """Return True if the message is dated (or False otherwise)."""

        if self._fraction_expired is not None:
            if self._fraction_expired == self.CANT_EXPIRE:
                return False
            if self._fraction_expired > self.HAS_EXPIRED * 2:
                return True

        prev_fraction = self._fraction_expired

        if self.code == _1F09 and self.verb != RQ:
            # RQs won't have remaining_seconds, RP/Ws have only partial cycle times
            self._fraction_expired = fraction_expired(
                self._gwy._dt_now() - self.dtm,
                td(seconds=self.payload["remaining_seconds"]),
            )
        else:  # self._pkt._expired can be False (doesn't expire), wont be 0
            self._fraction_expired = self._pkt._expired or self.CANT_EXPIRE

        if self._fraction_expired < self.HAS_EXPIRED:
            return False

        # TODO: should renew?

        # only log expired packets once
        if prev_fraction is None or prev_fraction < self.HAS_EXPIRED:
            if (
                self.code == _1F09
                and self.verb != I_
                or self.code in (_0016, _3120, _313F)
                or self._gwy._engine_state is not None  # restoring from pkt log
            ):
                _logger = _LOGGER.info
            else:
                _logger = _LOGGER.warning  # if DEV_MODE else _LOGGER.info  # TODO
            _logger(f"{self._pkt} # has expired ({self._fraction_expired * 100:1.0f}%)")

        # elif self._fraction_expired >= self.IS_EXPIRING:  # this could log multiple times
        #     _LOGGER.error("%s # is expiring", self._pkt)

        # and self.dtm >= self._gwy._dt_now() - td(days=7)  # TODO: should be none >7d?
        return self._fraction_expired > self.HAS_EXPIRED

    @property
    def _is_fragment_WIP(self) -> bool:
        """Return True if the raw payload is a fragment of a message."""

        if self._is_fragment is not None:
            return self._is_fragment

        # packets have a maximum length of 48 (decimal)
        # if self.code == _000A and self.verb == I_:
        #     self._is_fragment = True if len(???.zones) > 8 else None
        # el
        if self.code == _0404 and self.verb == RP:
            self._is_fragment = True
        elif self.code == _22C9 and self.verb == I_:
            self._is_fragment = None  # max length 24!
        else:
            self._is_fragment = False

        return self._is_fragment

    def _validate(self, raw_payload) -> Optional[dict]:  # TODO: needs work
        """Validate the message, and parse the payload if so.

        Raise an exception (InvalidPacketError) if it is not valid.
        """

        try:  # parse the payload
            # TODO: only accept invalid packets to/from HGI when flag raised
            _check_msg_payload(self, self._pkt.payload)  # ? InvalidPayloadError

            if not self._has_payload or (
                self.verb == RQ and self.code not in CODE_RQ_COMPLEX
            ):
                # _LOGGER.error("%s", msg)
                return {}

            result = PAYLOAD_PARSERS.get(self.code, parser_unknown)(
                self._pkt.payload, self
            )

            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return {**self._idx, **result}

            raise TypeError(f"Invalid payload type: {type(result)}")

        except InvalidPacketError as exc:
            (_LOGGER.exception if DEV_MODE else _LOGGER.warning)(
                "%s < %s", self._pkt, exc
            )
            raise exc

        except AssertionError as exc:
            # beware: HGI80 can send 'odd' but parseable packets +/- get invalid reply
            (
                _LOGGER.exception
                if DEV_MODE and self.src.type != "18"  # DEX
                else _LOGGER.exception
            )("%s < %s", self._pkt, f"{exc.__class__.__name__}({exc})")
            raise InvalidPacketError(exc)

        except (AttributeError, LookupError, TypeError, ValueError) as exc:  # TODO: dev
            _LOGGER.exception(
                "%s < Coding error: %s", self._pkt, f"{exc.__class__.__name__}({exc})"
            )
            raise InvalidPacketError from exc

        except NotImplementedError as exc:  # parser_unknown (unknown packet code)
            _LOGGER.warning("%s < Unknown packet code (cannot parse)", self._pkt)
            raise InvalidPacketError from exc


@lru_cache(maxsize=256)
def re_compile_re_match(regex, string) -> bool:
    # TODO: confirm this does speed things up
    # Python has it's own caching of re.complile, _MAXCACHE = 512
    # https://github.com/python/cpython/blob/3.10/Lib/re.py
    return re.compile(regex).match(string)


def _check_msg_payload(msg: Message, payload) -> None:
    """Validate the packet's payload against its verb/code pair.

    Raise an InvalidPayloadError if the payload is invalid, otherwise simply return.

    The HGI80-compatible devices can do what they like, but a warning is logged.
    Some parsers may also raise InvalidPayloadError (e.g. 3220), albeit later on.
    """

    try:
        if msg.code not in RAMSES_CODES:
            raise InvalidPacketError(f"Unknown code: {msg.code}")

        try:
            regex = RAMSES_CODES[msg.code][msg.verb]
        except KeyError:
            raise InvalidPacketError(f"Unknown verb/code pair: {msg.verb}/{msg.code}")

        if not re_compile_re_match(regex, payload):
            raise InvalidPayloadError(f"Payload doesn't match '{regex}': {payload}")

    except InvalidPacketError as exc:  # incl. InvalidPayloadError
        if "18" not in (msg.src.type, msg.dst.type):  # DEX, HGI80 can do what it likes
            raise exc  # TODO: messy - these msgs not ignore
            _LOGGER.warning(f"{msg._pkt} < {exc}")

    # TODO: put this back, or leave it to the parser?
    # if msg.code == _3220:
    #     msg_id = int(payload[4:6], 16)
    #     if msg_id not in OPENTHERM_MESSAGES:  # parser uses OTB_MSG_IDS
    #         raise InvalidPayloadError(
    #             f"OpenTherm: Unsupported data-id: 0x{msg_id:02X} ({msg_id})"
    #         )
