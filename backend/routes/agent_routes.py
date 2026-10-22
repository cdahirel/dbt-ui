"""AI assistant (agent) API routes."""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from models import AgentChatRequest
from agent.runner import MODEL, is_agent_configured, run_agent
from agent.tools import ToolContext
from routes.env_routes import get_env_vars_from_cookie
from utils.input_validation import validate_dbt_target, validate_file_path

router = APIRouter()


@router.get("/api/agent/status")
async def agent_status():
    """Report whether the assistant can reach the Anthropic API."""
    return {"configured": is_agent_configured(), "model": MODEL}


@router.post("/api/agent/chat")
def agent_chat(request: AgentChatRequest, http_request: Request):
    """Run the agent and stream its progress as Server-Sent Events.

    Each event is a JSON object (see agent/runner.py). The final `messages` event carries
    the updated conversation history, which the client sends back on the next request.
    """
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

    def event_stream():
        for event in run_agent(ctx, request.messages, user_message, request.approvals, request.auto_approve):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
