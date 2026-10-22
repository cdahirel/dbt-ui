"""AI assistant (agent) API routes."""
import json
import queue
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from models import AgentChatRequest, AgentApprovalRequest, AgentSessionRequest
from agent.runner import MODEL, is_agent_configured, run_agent
from agent.team import ROLES, end_session, get_session, start_session
from agent.tools import ToolContext
from routes.env_routes import get_env_vars_from_cookie
from utils.input_validation import validate_dbt_target, validate_file_path

router = APIRouter()


def _prepare(request: AgentChatRequest, http_request: Request):
    """Validate a chat request and build the tool context and user message."""
    path = Path(request.path).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Project path does not exist")

    user_message = request.user_message.strip()
    if user_message and request.selected_file:
        selected = validate_file_path(request.selected_file, "selected file")
        user_message = f"[Currently open in the editor: {selected}]\n\n{user_message}"

    ctx = ToolContext(
        project_path=path,
        env_vars=get_env_vars_from_cookie(http_request, str(path)),
        target=validate_dbt_target(request.target),
    )
    return ctx, user_message


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


HEARTBEAT_SECONDS = 15
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.get("/api/agent/status")
async def agent_status():
    """Report whether the assistant can reach the Anthropic API."""
    return {
        "configured": is_agent_configured(),
        "model": MODEL,
        "roles": {name: role["label"] for name, role in ROLES.items()},
    }


@router.post("/api/agent/chat")
def agent_chat(request: AgentChatRequest, http_request: Request):
    """Run the agent and stream its progress as Server-Sent Events.

    Each event is a JSON object (see agent/runner.py). The final `messages` event carries
    the updated conversation history, which the client sends back on the next request.
    """
    ctx, user_message = _prepare(request, http_request)

    def event_stream():
        for event in run_agent(ctx, request.messages, user_message, request.approvals, request.auto_approve):
            yield _sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api/agent/team")
def agent_team(request: AgentChatRequest, http_request: Request):
    """Run a team of agents (lead + parallel sub-agents) and stream their events as SSE.

    The first event is `session` with the id used by /api/agent/approve and /api/agent/cancel.
    Every event carries an `agent` field: "lead" or the sub-agent id.
    """
    ctx, user_message = _prepare(request, http_request)
    session = start_session(ctx, request.auto_approve, request.messages, user_message)

    def event_stream():
        try:
            yield _sse({"type": "session", "session_id": session.id, "agent": "lead"})
            while True:
                try:
                    event = session.events.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    # Agents can be silent for minutes (approvals, dbt runs): keep proxies from timing out
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield _sse(event)
        finally:
            # Client disconnected or run finished: stop any remaining agents
            end_session(session)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api/agent/approve")
async def agent_approve(request: AgentApprovalRequest):
    """Approve or decline a tool call that a team agent is waiting on."""
    session = get_session(request.session_id)
    if not session or not session.resolve_approval(request.tool_id, request.approved):
        raise HTTPException(status_code=404, detail="No pending approval for this call")
    return {"success": True}


@router.post("/api/agent/cancel")
async def agent_cancel(request: AgentSessionRequest):
    """Stop a running team."""
    session = get_session(request.session_id)
    if session:
        session.cancel()
    return {"success": True}
