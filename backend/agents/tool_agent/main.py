"""Authenticated single-source runtime; Gateway and memory are mandatory."""
from __future__ import annotations

import os
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from langchain_core.messages import HumanMessage

from shared.memory.agentcore import build_invocation_config, create_checkpointer
from shared.presenters import build_final_text_surface
from shared.response_text import final_ai_text
from shared.runtime.app_factory import create_app
from shared.runtime.history import begin_turn, end_turn
from shared.runtime.identity import UnauthenticatedError, verify_gateway_token
from shared.runtime.session_guard import AuthorizedSession, guarded_entrypoint
from shared.tools.gateway_context import current_gateway_token, gateway_request_context, gateway_verifier
from tool_agent.graph import SourceUnavailableError, compile_graph
from tool_agent.profile import load_profile

app = create_app()


@lru_cache(maxsize=1)
def checkpointer():
    memory_id = os.environ.get("CEI_TOOL_AGENT_MEMORY_ID", "").strip()
    if not memory_id:
        raise RuntimeError("Tool-agent memory is not configured")
    return create_checkpointer(memory_id)


def source_thread(session_id: str, source_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"cei-tool:{session_id}:{source_id}"))


def authorize_grant(token: str, verifier, profile, user_id: str, session_id: str) -> None:
    try:
        verify_gateway_token(token, verifier, (profile.scope, "cei-tools/common"), user_id, session_id)
    except (ValueError, UnauthenticatedError) as exc:
        raise UnauthenticatedError("Invalid source grant") from exc


@app.entrypoint
@guarded_entrypoint
async def invoke(payload, context, session: AuthorizedSession):
    if os.environ.get("CEI_REQUIRE_TOOL_GATEWAY", "").lower() != "true":
        raise UnauthenticatedError("Gateway enforcement is required")
    try:
        profile = load_profile(payload.get("source_id"))
    except ValueError:
        return {"status": "error", "message": "Unknown or unavailable source."}
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 20_000:
        return {"status": "error", "message": "A valid prompt is required."}
    with gateway_request_context(context, session.user_id, session.session_id):
        token = current_gateway_token()
        if not token:
            raise UnauthenticatedError("Gateway credential is missing")
        authorize_grant(token, gateway_verifier(), profile, session.user_id, session.session_id)
        try:
            graph = compile_graph(profile, checkpointer())
        except SourceUnavailableError:
            return {"status": "error", "message": "This source is not available yet."}
        config = build_invocation_config(source_thread(session.session_id, profile.source_id), session.user_id)
        # Do not rehydrate a different source's chat history into this source-bound checkpoint.
        await begin_turn(user_id=session.user_id, session_id=session.session_id, prompt=prompt,
                         agent_type="tool_agent", mode=None, warm=True)
        result = await graph.ainvoke({"messages": [HumanMessage(content=prompt)], "tool_rounds": 0}, config)
        text = final_ai_text(result["messages"]) or "The source did not return an answer."
        ui = build_final_text_surface(text, surface_id="tool-agent-answer").model_dump(mode="json")
        envelope = {"status": "completed", "text": text, "result": text, "ui": ui}
        await end_turn(user_id=session.user_id, session_id=session.session_id, envelope=envelope, prompt=prompt)
        return envelope


if __name__ == "__main__":
    app.run()
