from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from livekit import api, rtc

from bridge_tools import ENV_PATH


load_dotenv(ENV_PATH, override=False)

PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
DEFAULT_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "codex-bridge-livekit").strip() or "codex-bridge-livekit"
DEFAULT_ROOM_PREFIX = os.environ.get("LIVEKIT_SIP_ROOM_PREFIX", "codex-phone").strip() or "codex-phone"
DEFAULT_INBOUND_TRUNK_NAME = os.environ.get("LIVEKIT_SIP_INBOUND_TRUNK_NAME", "twilio-inbound").strip() or "twilio-inbound"
DEFAULT_OUTBOUND_TRUNK_NAME = os.environ.get("LIVEKIT_SIP_OUTBOUND_TRUNK_NAME", "twilio-outbound").strip() or "twilio-outbound"
DEFAULT_DISPATCH_RULE_NAME = os.environ.get("LIVEKIT_SIP_DISPATCH_RULE_NAME", "twilio-inbound-dispatch").strip() or "twilio-inbound-dispatch"


@dataclass(frozen=True)
class LiveKitTwilioSipConfig:
    agent_name: str
    room_prefix: str
    inbound_trunk_name: str
    outbound_trunk_name: str
    dispatch_rule_name: str
    inbound_trunk_id: str
    outbound_trunk_id: str
    dispatch_rule_id: str
    inbound_numbers: tuple[str, ...]
    outbound_numbers: tuple[str, ...]
    allowed_addresses: tuple[str, ...]
    termination_uri: str
    auth_username: str
    auth_password: str
    media_encryption: int
    transport: int
    room_empty_timeout: int
    room_max_participants: int


def _csv_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    values = []
    for item in raw.split(","):
        candidate = item.strip()
        if candidate:
            values.append(candidate)
    return tuple(values)


def _get_enum_value(enum_descriptor: Any, value: str, fallback: str) -> int:
    name = str(value or "").strip().upper() or fallback
    options = enum_descriptor.values_by_name
    if name not in options:
        raise ValueError(f"Unsupported enum value: {name}")
    return int(options[name].number)


def normalize_phone_number(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Phone number is required.")
    compact = re.sub(r"[^\d+]", "", raw)
    if compact.startswith("00"):
        compact = f"+{compact[2:]}"
    if compact.startswith("+"):
        normalized = compact
    else:
        digits = re.sub(r"\D", "", compact)
        normalized = f"+{digits}"
    if not PHONE_PATTERN.match(normalized):
        raise ValueError("Phone number must be in E.164 format, for example +14155550100.")
    return normalized


def normalize_sip_uri(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("TWILIO_SIP_TERMINATION_URI is required for outbound calling.")
    if raw.startswith("sip:") or raw.startswith("sips:"):
        return raw
    return f"sip:{raw}"


def normalize_sip_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("TWILIO_SIP_TERMINATION_URI is required for outbound calling.")
    normalized = re.sub(r"^sips?:", "", raw, flags=re.IGNORECASE).strip().strip("/")
    if not normalized:
        raise ValueError("TWILIO_SIP_TERMINATION_URI must include a hostname.")
    return normalized


def parse_job_metadata(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_twilio_sip_config() -> LiveKitTwilioSipConfig:
    inbound_numbers = _csv_env("TWILIO_SIP_INBOUND_NUMBERS") or _csv_env("TWILIO_SIP_PHONE_NUMBERS")
    outbound_numbers = _csv_env("TWILIO_SIP_OUTBOUND_NUMBERS") or _csv_env("TWILIO_SIP_PHONE_NUMBERS")
    allowed_addresses = _csv_env("TWILIO_SIP_ALLOWED_ADDRESSES")
    termination_uri = os.environ.get("TWILIO_SIP_TERMINATION_URI", "").strip()
    auth_username = os.environ.get("TWILIO_SIP_AUTH_USERNAME", "").strip()
    auth_password = os.environ.get("TWILIO_SIP_AUTH_PASSWORD", "").strip()
    media_encryption = _get_enum_value(
        api.SIPMediaEncryption.DESCRIPTOR,
        os.environ.get("LIVEKIT_SIP_MEDIA_ENCRYPTION", ""),
        "SIP_MEDIA_ENCRYPT_ALLOW",
    )
    transport = _get_enum_value(
        api.SIPTransport.DESCRIPTOR,
        os.environ.get("LIVEKIT_SIP_TRANSPORT", ""),
        "SIP_TRANSPORT_TLS",
    )

    return LiveKitTwilioSipConfig(
        agent_name=DEFAULT_AGENT_NAME,
        room_prefix=DEFAULT_ROOM_PREFIX,
        inbound_trunk_name=DEFAULT_INBOUND_TRUNK_NAME,
        outbound_trunk_name=DEFAULT_OUTBOUND_TRUNK_NAME,
        dispatch_rule_name=DEFAULT_DISPATCH_RULE_NAME,
        inbound_trunk_id=os.environ.get("LIVEKIT_SIP_INBOUND_TRUNK_ID", "").strip(),
        outbound_trunk_id=os.environ.get("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "").strip(),
        dispatch_rule_id=os.environ.get("LIVEKIT_SIP_DISPATCH_RULE_ID", "").strip(),
        inbound_numbers=inbound_numbers,
        outbound_numbers=outbound_numbers,
        allowed_addresses=allowed_addresses,
        termination_uri=termination_uri,
        auth_username=auth_username,
        auth_password=auth_password,
        media_encryption=media_encryption,
        transport=transport,
        room_empty_timeout=int(os.environ.get("LIVEKIT_SIP_ROOM_EMPTY_TIMEOUT", "300") or "300"),
        room_max_participants=int(os.environ.get("LIVEKIT_SIP_ROOM_MAX_PARTICIPANTS", "3") or "3"),
    )


def validate_outbound_call_target(number: str) -> str:
    return normalize_phone_number(number)


def validate_provisioning_config(config: LiveKitTwilioSipConfig) -> None:
    if not config.inbound_numbers:
        raise ValueError(
            "Set TWILIO_SIP_INBOUND_NUMBERS or TWILIO_SIP_PHONE_NUMBERS before provisioning inbound telephony."
        )
    if not config.outbound_numbers:
        raise ValueError(
            "Set TWILIO_SIP_OUTBOUND_NUMBERS or TWILIO_SIP_PHONE_NUMBERS before provisioning outbound telephony."
        )
    if not config.auth_username or not config.auth_password:
        raise ValueError("Set TWILIO_SIP_AUTH_USERNAME and TWILIO_SIP_AUTH_PASSWORD before provisioning telephony.")
    if not config.allowed_addresses:
        raise ValueError(
            "Set TWILIO_SIP_ALLOWED_ADDRESSES for inbound Twilio Elastic SIP allowlisting."
        )
    normalize_sip_uri(config.termination_uri)


def require_outbound_trunk_id(config: LiveKitTwilioSipConfig) -> str:
    trunk_id = config.outbound_trunk_id.strip()
    if not trunk_id:
        raise ValueError(
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID is not set. Provision telephony first or export the outbound trunk ID."
        )
    return trunk_id


def _find_by_name(items: list[Any], wanted_id: str, wanted_name: str, id_field: str) -> Any | None:
    candidate_id = wanted_id.strip()
    candidate_name = wanted_name.strip()
    for item in items:
        item_id = str(getattr(item, id_field, "")).strip()
        item_name = str(getattr(item, "name", "")).strip()
        if candidate_id and item_id == candidate_id:
            return item
        if candidate_name and item_name == candidate_name:
            return item
    return None


async def _ensure_room_exists(
    lkapi: api.LiveKitAPI,
    *,
    room_name: str,
    metadata: str = "",
    empty_timeout: int = 300,
    max_participants: int = 3,
) -> api.Room:
    try:
        return await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                metadata=metadata,
                empty_timeout=empty_timeout,
                max_participants=max_participants,
            )
        )
    except api.TwirpError as exc:
        if exc.code != api.TwirpErrorCode.ALREADY_EXISTS:
            raise
        rooms = await lkapi.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
        for room in rooms.rooms:
            if room.name == room_name:
                return room
        raise


def build_outbound_dispatch_metadata(
    *,
    to_number: str,
    prompt: str = "",
    display_name: str = "",
    intro_message: str = "",
    trunk_id: str = "",
) -> str:
    payload = {
        "channel": "twilio_sip",
        "direction": "outbound",
        "outbound_call": {
            "to_number": validate_outbound_call_target(to_number),
            "participant_identity": f"sip-outbound-{int(time.time())}-{secrets.token_hex(3)}",
            "participant_name": display_name.strip() or "Phone caller",
            "prompt": str(prompt or "").strip(),
            "intro_message": str(intro_message or "").strip(),
            "trunk_id": str(trunk_id or "").strip(),
        },
    }
    return json.dumps(payload, separators=(",", ":"))


async def provision_twilio_sip() -> dict[str, Any]:
    config = load_twilio_sip_config()
    validate_provisioning_config(config)

    inbound_metadata = json.dumps({"provider": "twilio", "direction": "inbound"}, separators=(",", ":"))
    outbound_metadata = json.dumps({"provider": "twilio", "direction": "outbound"}, separators=(",", ":"))
    dispatch_metadata = json.dumps({"channel": "twilio_sip", "direction": "inbound"}, separators=(",", ":"))

    async with api.LiveKitAPI() as lkapi:
        inbound_list = await lkapi.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        existing_inbound = _find_by_name(
            list(inbound_list.items),
            config.inbound_trunk_id,
            config.inbound_trunk_name,
            "sip_trunk_id",
        )
        inbound_info = api.SIPInboundTrunkInfo(
            name=config.inbound_trunk_name,
            metadata=inbound_metadata,
            numbers=list(config.inbound_numbers),
            allowed_addresses=list(config.allowed_addresses),
            media_encryption=config.media_encryption,
        )
        if existing_inbound:
            inbound_info.sip_trunk_id = existing_inbound.sip_trunk_id
            inbound_trunk = await lkapi.sip.update_inbound_trunk(existing_inbound.sip_trunk_id, inbound_info)
        else:
            inbound_trunk = await lkapi.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(trunk=inbound_info)
            )

        outbound_list = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        existing_outbound = _find_by_name(
            list(outbound_list.items),
            config.outbound_trunk_id,
            config.outbound_trunk_name,
            "sip_trunk_id",
        )
        outbound_info = api.SIPOutboundTrunkInfo(
            name=config.outbound_trunk_name,
            metadata=outbound_metadata,
            address=normalize_sip_host(config.termination_uri),
            transport=config.transport,
            numbers=list(config.outbound_numbers),
            auth_username=config.auth_username,
            auth_password=config.auth_password,
            media_encryption=config.media_encryption,
        )
        if existing_outbound:
            outbound_info.sip_trunk_id = existing_outbound.sip_trunk_id
            outbound_trunk = await lkapi.sip.update_outbound_trunk(existing_outbound.sip_trunk_id, outbound_info)
        else:
            outbound_trunk = await lkapi.sip.create_outbound_trunk(
                api.CreateSIPOutboundTrunkRequest(trunk=outbound_info)
            )

        dispatch_list = await lkapi.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        existing_dispatch = _find_by_name(
            list(dispatch_list.items),
            config.dispatch_rule_id,
            config.dispatch_rule_name,
            "sip_dispatch_rule_id",
        )
        dispatch_info = api.SIPDispatchRuleInfo(
            name=config.dispatch_rule_name,
            metadata=dispatch_metadata,
            trunk_ids=[inbound_trunk.sip_trunk_id],
            rule=api.SIPDispatchRule(
                dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                    room_prefix=config.room_prefix,
                    no_randomness=False,
                )
            ),
            room_config=api.RoomConfiguration(
                empty_timeout=config.room_empty_timeout,
                max_participants=config.room_max_participants,
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=config.agent_name,
                        metadata=dispatch_metadata,
                    )
                ],
            ),
        )
        if existing_dispatch:
            dispatch_info.sip_dispatch_rule_id = existing_dispatch.sip_dispatch_rule_id
            dispatch_rule = await lkapi.sip.update_dispatch_rule(
                existing_dispatch.sip_dispatch_rule_id,
                dispatch_info,
            )
        else:
            dispatch_rule = await lkapi.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(dispatch_rule=dispatch_info)
            )

    return {
        "agent_name": config.agent_name,
        "inbound_trunk_id": inbound_trunk.sip_trunk_id,
        "outbound_trunk_id": outbound_trunk.sip_trunk_id,
        "dispatch_rule_id": dispatch_rule.sip_dispatch_rule_id,
        "room_prefix": config.room_prefix,
        "inbound_numbers": list(config.inbound_numbers),
        "outbound_numbers": list(config.outbound_numbers),
        "termination_uri": normalize_sip_host(config.termination_uri),
    }


async def dispatch_outbound_call(
    *,
    to_number: str,
    prompt: str = "",
    display_name: str = "",
    intro_message: str = "",
) -> dict[str, Any]:
    config = load_twilio_sip_config()
    trunk_id = require_outbound_trunk_id(config)
    normalized_number = validate_outbound_call_target(to_number)
    room_name = f"{config.room_prefix}-out-{int(time.time())}-{secrets.token_hex(3)}"
    metadata = build_outbound_dispatch_metadata(
        to_number=normalized_number,
        prompt=prompt,
        display_name=display_name,
        intro_message=intro_message,
        trunk_id=trunk_id,
    )

    async with api.LiveKitAPI() as lkapi:
        room = await _ensure_room_exists(
            lkapi,
            room_name=room_name,
            metadata=json.dumps({"channel": "twilio_sip", "direction": "outbound"}, separators=(",", ":")),
            empty_timeout=config.room_empty_timeout,
            max_participants=config.room_max_participants,
        )
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=config.agent_name,
                room=room_name,
                metadata=metadata,
            )
        )

    return {
        "room_name": room.name,
        "dispatch_id": dispatch.id,
        "agent_name": config.agent_name,
        "to_number": normalized_number,
        "outbound_trunk_id": trunk_id,
    }


async def start_outbound_call_for_job(
    ctx: Any,
    *,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    outbound = metadata.get("outbound_call")
    if not isinstance(outbound, dict):
        return None

    config = load_twilio_sip_config()
    to_number = validate_outbound_call_target(str(outbound.get("to_number", "")))
    trunk_id = str(outbound.get("trunk_id", "")).strip() or require_outbound_trunk_id(config)
    participant_identity = str(outbound.get("participant_identity", "")).strip()
    if not participant_identity:
        participant_identity = f"sip-outbound-{int(time.time())}-{secrets.token_hex(3)}"
    participant_name = str(outbound.get("participant_name", "")).strip() or "Phone caller"

    await ctx.add_sip_participant(
        call_to=to_number,
        trunk_id=trunk_id,
        participant_identity=participant_identity,
        participant_name=participant_name,
    )

    return {
        "to_number": to_number,
        "participant_identity": participant_identity,
        "participant_name": participant_name,
        "trunk_id": trunk_id,
        "intro_message": str(outbound.get("intro_message", "")).strip(),
    }


async def dial_out_from_session(
    *,
    ctx: Any,
    phone_number: str,
    trunk_id: str,
    participant_name: str = "",
) -> dict[str, Any]:
    normalized_number = validate_outbound_call_target(phone_number)
    participant_identity = f"sip-outbound-{int(time.time())}-{secrets.token_hex(3)}"
    await ctx.add_sip_participant(
        call_to=normalized_number,
        trunk_id=trunk_id,
        participant_identity=participant_identity,
        participant_name=participant_name.strip() or "Phone caller",
    )
    return {
        "to_number": normalized_number,
        "participant_identity": participant_identity,
        "trunk_id": trunk_id,
    }


async def maybe_run_cli(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[1] not in {"provision-twilio-sip", "dial"}:
        return False

    parser = argparse.ArgumentParser(prog=argv[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("provision-twilio-sip")

    dial_parser = subparsers.add_parser("dial")
    dial_parser.add_argument("phone_number")
    dial_parser.add_argument("--prompt", default="")
    dial_parser.add_argument("--display-name", default="")
    dial_parser.add_argument("--intro", default="")

    args = parser.parse_args(argv[1:])

    if args.command == "provision-twilio-sip":
        result = await provision_twilio_sip()
    else:
        result = await dispatch_outbound_call(
            to_number=args.phone_number,
            prompt=args.prompt,
            display_name=args.display_name,
            intro_message=args.intro,
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return True


def run_cli(argv: list[str]) -> bool:
    return asyncio.run(maybe_run_cli(argv))
