from __future__ import annotations

import json
import logging
import os
import sys

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit import rtc
from livekit.plugins import openai

from bridge_tools import (
    ENV_PATH,
    ROOT,
    SessionState,
    check_bridge_health as check_bridge_health_impl,
    dispatch_codex_request as dispatch_codex_request_impl,
    format_tool_result,
    get_task_messages as get_task_messages_impl,
    get_task_status as get_task_status_impl,
    instance_registry_summary,
    list_instances as list_instances_impl,
    respond_to_task as respond_to_task_impl,
    route_request as route_request_impl,
)
from telephony import (
    dial_out_from_session,
    load_twilio_sip_config,
    parse_job_metadata,
    run_cli as run_telephony_cli,
    start_outbound_call_for_job,
)


load_dotenv(ENV_PATH, override=False)

LOGGER = logging.getLogger("livekit_codex_agent")
PROMPT_PATH = ROOT / "prompts" / "system_prompt.txt"
AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "codex-bridge-livekit").strip() or "codex-bridge-livekit"


def build_system_prompt() -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8").strip()
    return template.replace("{{INSTANCE_REGISTRY_SUMMARY}}", instance_registry_summary())


class CodexBridgeAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=build_system_prompt())

    @function_tool()
    async def list_instances(self, context: RunContext, only_live: bool = False) -> str:
        """List registered Codex-capable instances and their current bridge/runtime status.

        Args:
            only_live: If true, only include instances whose bridge health check is currently live.
        """

        return format_tool_result(await list_instances_impl(only_live=only_live))

    @function_tool()
    async def check_bridge_health(self, context: RunContext, instance_id: str = "") -> str:
        """Check one instance bridge or all instance bridges for live health.

        Args:
            instance_id: Optional instance id such as hack, STT-A10, or TTS-H100.
        """

        return format_tool_result(await check_bridge_health_impl(instance_id=instance_id))

    @function_tool()
    async def route_codex_request(
        self,
        context: RunContext,
        request_text: str,
        target_instance_id: str = "",
    ) -> str:
        """Plan which registered instance should handle a request without starting the task.

        Args:
            request_text: The engineering request to route.
            target_instance_id: Optional explicit instance id to force.
        """

        return format_tool_result(
            await route_request_impl(request_text=request_text, target_instance_id=target_instance_id)
        )

    @function_tool()
    async def dispatch_codex_request(
        self,
        context: RunContext,
        request_text: str,
        target_instance_id: str = "",
    ) -> str:
        """Dispatch an engineering task to the Codex bridge on the selected server.

        Args:
            request_text: The exact engineering task to run.
            target_instance_id: Optional explicit instance id to force.
        """

        state = context.userdata
        return format_tool_result(
            await dispatch_codex_request_impl(
                state=state,
                request_text=request_text,
                target_instance_id=target_instance_id,
            )
        )

    @function_tool()
    async def get_task_status(self, context: RunContext, task_id: str = "") -> str:
        """Get the current bridge task status and any captured final output.

        Args:
            task_id: Optional task id. If omitted, use the latest task from this session.
        """

        return format_tool_result(await get_task_status_impl(state=context.userdata, task_id=task_id))

    @function_tool()
    async def get_task_messages(self, context: RunContext, task_id: str = "") -> str:
        """Get user-facing task messages, blockers, approvals, and follow-up questions.

        Args:
            task_id: Optional task id. If omitted, use the latest task from this session.
        """

        return format_tool_result(await get_task_messages_impl(state=context.userdata, task_id=task_id))

    @function_tool()
    async def respond_to_task(
        self,
        context: RunContext,
        message: str,
        task_id: str = "",
        in_reply_to: str = "",
    ) -> str:
        """Answer a pending Codex task question or approval request.

        Args:
            message: The response or approval text to send.
            task_id: Optional task id. If omitted, use the latest task from this session.
            in_reply_to: Optional bridge message id being answered.
        """

        return format_tool_result(
            await respond_to_task_impl(
                state=context.userdata,
                message=message,
                task_id=task_id,
                in_reply_to=in_reply_to,
            )
        )

    @function_tool()
    async def call_phone_number(
        self,
        context: RunContext,
        phone_number: str,
        participant_name: str = "",
    ) -> str:
        """Dial a phone number into the current LiveKit room through the configured SIP trunk.

        Args:
            phone_number: E.164 phone number such as +14155550100.
            participant_name: Optional display name for the outbound phone participant.
        """

        state = context.userdata
        if state.job_ctx is None:
            raise ValueError("No active LiveKit job context is available for phone dialing.")
        if not state.outbound_trunk_id:
            raise ValueError(
                "Outbound SIP trunk is not configured. Set LIVEKIT_SIP_OUTBOUND_TRUNK_ID first."
            )

        result = await dial_out_from_session(
            ctx=state.job_ctx,
            phone_number=phone_number,
            trunk_id=state.outbound_trunk_id,
            participant_name=participant_name,
        )
        return format_tool_result(
            {
                "ok": True,
                "message": (
                    f"Started dialing {result['to_number']} into this room through trunk "
                    f"{result['trunk_id']}."
                ),
                "call": result,
            }
        )


async def entrypoint(ctx: JobContext) -> None:
    job_metadata = parse_job_metadata(ctx.job.metadata)
    telephony_config = load_twilio_sip_config()
    await ctx.connect()

    session_state = SessionState(
        job_ctx=ctx,
        outbound_trunk_id=telephony_config.outbound_trunk_id,
    )
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
            voice=os.environ.get("OPENAI_REALTIME_VOICE", "sage"),
        ),
        userdata=session_state,
    )
    session_state.agent_session = session

    await session.start(
        agent=CodexBridgeAssistant(),
        room=ctx.room,
    )

    outbound_call = None
    if str(job_metadata.get("direction", "")).strip().lower() == "outbound":
        outbound_call = await start_outbound_call_for_job(ctx, metadata=job_metadata)
        LOGGER.info("Started outbound SIP call: %s", json.dumps(outbound_call, sort_keys=True))

    if outbound_call:
        intro_message = str(outbound_call.get("intro_message", "")).strip()
        if intro_message:
            try:
                await ctx.wait_for_participant(
                    identity=str(outbound_call.get("participant_identity", "")).strip(),
                    kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                )
                await session.generate_reply(
                    instructions=(
                        "A phone callee just answered. "
                        f"Briefly say this first: {intro_message}"
                    ),
                    allow_interruptions=True,
                )
            except Exception:
                LOGGER.exception("Failed while waiting for outbound SIP callee to answer")
        return

    await session.generate_reply(
        instructions=(
            "Introduce yourself briefly as the server-side dev agent. "
            "Mention that you can inspect server availability, dispatch Codex work, "
            "report task progress, and call a phone number into this LiveKit room when asked."
        ),
        allow_interruptions=True,
    )


if __name__ == "__main__":
    if run_telephony_cli(sys.argv):
        raise SystemExit(0)
    logging.basicConfig(level=logging.INFO)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
        )
    )
