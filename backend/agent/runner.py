"""Agent loop: streams a Claude conversation that can use the dbt tools.

The loop is stateless across HTTP requests: the client keeps the conversation
history (in Anthropic message format) and sends it back on every request.
When Claude asks for a tool that changes the project or the warehouse, the
loop stops with an `approval_required` event; the client resumes it by sending
the same history plus the user's decisions.
"""
import os
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import anthropic
import yaml

from agent.tools import TOOLS_BY_NAME, ToolContext, ToolError, tool_definitions, validate_input

MODEL = os.environ.get("DBT_UI__AGENT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("DBT_UI__AGENT_EFFORT", "high")
# Server-side refusal fallback (re-runs a declined request on another model)
FALLBACKS_ENABLED = os.environ.get("DBT_UI__AGENT_FALLBACKS", "true").lower() in ("true", "1", "yes")
# Models that accept the server-side refusal fallback ("default" form)
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 64000
MAX_TURNS = 30

Event = Dict[str, Any]

_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def is_agent_configured() -> bool:
    """Whether Anthropic credentials appear to be available to the backend.

    The SDK resolves credentials lazily, so this only checks the usual sources.
    """
    credential_vars = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID")
    if any(os.environ.get(var) for var in credential_vars):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def build_system_prompt(project_path: Path) -> str:
    project_name = project_path.name
    profile = ""
    try:
        with open(project_path / "dbt_project.yml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        project_name = config.get("name", project_name)
        profile = config.get("profile", "")
    except (OSError, yaml.YAMLError):
        pass

    return f"""You are the dbt assistant built into dbt-ui, a web IDE for dbt-core projects. You help analytics engineers understand, build, test and debug their dbt project by using the tools provided.

Project: {project_name}{f" (profile: {profile})" if profile else ""}
All file paths are relative to the project root.

How to work:
- Ground every answer in the actual project: explore with list_files, search_files, read_file, list_nodes and get_node before answering or changing anything. Do not guess model names, columns or conventions.
- When writing SQL or YAML, follow the conventions already used in the project (naming, layering such as staging/intermediate/marts, CTE style, how sources and tests are declared). Use ref() and source(), never hard-coded relations.
- After editing models, macros or YAML, validate with run_dbt (parse, or compile with a selector). If it fails, read the error, fix it and validate again.
- Prefer edit_file for targeted changes to existing files; use write_file for new files.
- run/seed/build/snapshot change data in the warehouse: only use them when they serve the user's request, keep the selector as narrow as possible, and never use full_refresh unless asked. The user is asked to approve these calls and every file change; if a call is declined, do not retry it, ask what they would prefer instead.
- To inspect data, use run_query with a small limit.
- If target/manifest.json is missing or stale, run run_dbt with command 'parse' first.

Communication:
- Reply in the user's language.
- Be concise. When you change files, finish with a short summary of what changed and how you validated it.
- Use Markdown; put SQL and YAML in fenced code blocks."""


def sanitize_assistant_content(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prepare an assistant turn for the history.

    After a mid-output refusal fallback, thinking and tool_use blocks emitted before the
    last `fallback` block belong to the declined attempt and must not be echoed back
    (or executed). Text blocks are kept; the fallback marker itself is dropped.
    """
    last_fallback = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    sanitized = []
    for i, block in enumerate(content):
        if block.get("type") == "fallback":
            continue
        if i < last_fallback and block.get("type") != "text":
            continue
        sanitized.append(block)
    return sanitized


def close_dangling_tool_uses(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make an interrupted history safe to continue.

    If the last assistant turn has tool calls without results (the run stopped before or
    while executing them), answer them as interrupted so the next request is valid.
    """
    pending = _pending_tool_uses(messages)
    if not pending:
        return messages
    results = [
        {"type": "tool_result", "tool_use_id": block["id"], "content": "Interrupted before this call ran.", "is_error": True}
        for block in pending
    ]
    return messages + [{"role": "user", "content": results}]


def _pending_tool_uses(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """tool_use blocks of the last assistant turn, if they have not been answered yet."""
    if not messages or messages[-1].get("role") != "assistant":
        return []
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _execute_tools(
    ctx: ToolContext,
    tool_uses: List[Dict[str, Any]],
    approvals: Dict[str, bool],
    auto_approve: bool,
) -> Generator[Event, None, List[Dict[str, Any]]]:
    """Run tool calls, yielding UI events. Returns the tool_result blocks."""
    results = []
    for block in tool_uses:
        tool_id, name, args = block["id"], block["name"], block.get("input", {})
        spec = TOOLS_BY_NAME.get(name)
        is_error = False

        if spec is None:
            output, is_error = f"Unknown tool: {name}", True
        elif (error := validate_input(spec, args)) is not None:
            output, is_error = f"Invalid input: {error}", True
        elif spec.needs_approval(args) and not auto_approve and not approvals.get(tool_id, False):
            output, is_error = "The user declined this action.", True
        else:
            yield {"type": "tool_running", "id": tool_id, "name": name}
            try:
                output = spec.execute(ctx, args)
                if spec.changes_project(args):
                    yield {"type": "project_changed", "tool": name, "path": args.get("path")}
            except ToolError as e:
                output, is_error = str(e), True
            except Exception as e:  # unexpected failure: report it to Claude instead of aborting the turn
                output, is_error = f"Tool failed: {e}", True

        yield {"type": "tool_result", "id": tool_id, "name": name, "content": output, "is_error": is_error}
        result = {"type": "tool_result", "tool_use_id": tool_id, "content": output}
        if is_error:
            result["is_error"] = True
        results.append(result)
    return results


def _needs_approval(tool_uses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    gated = []
    for block in tool_uses:
        spec = TOOLS_BY_NAME.get(block["name"])
        args = block.get("input", {})
        if spec and validate_input(spec, args) is None and spec.needs_approval(args):
            gated.append({"id": block["id"], "name": block["name"], "input": args})
    return gated


def stream_turn(
    system: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "",
) -> Generator[Event, None, Any]:
    """Stream one model response, yielding UI events. Returns the final message."""
    model = model or MODEL
    params: Dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=tools if tools is not None else tool_definitions(),
        messages=messages,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},
    )
    if FALLBACKS_ENABLED and model in FALLBACK_MODELS:
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"

    with get_client().beta.messages.stream(**params) as stream:
        for event in stream:
            if event.type == "content_block_start":
                block = event.content_block
                payload: Event = {"type": "block_start", "index": event.index, "block_type": block.type}
                if block.type == "tool_use":
                    payload.update(id=block.id, name=block.name)
                yield payload
            elif event.type == "text":
                yield {"type": "text_delta", "text": event.text}
            elif event.type == "thinking":
                yield {"type": "thinking_delta", "text": event.thinking}
        return stream.get_final_message()


def run_agent(
    ctx: ToolContext,
    messages: List[Dict[str, Any]],
    user_message: str,
    approvals: Dict[str, bool],
    auto_approve: bool,
) -> Generator[Event, None, None]:
    """Drive the agent until it finishes, needs approval, or fails. Yields UI events.

    `messages` is the prior history. `user_message` (optional) is new user input; if the
    history ends with unanswered tool calls, they are resolved first using `approvals`
    (a new message without approval counts as declining them).
    """
    system = build_system_prompt(ctx.project_path)
    messages = list(messages)

    try:
        user_content: List[Dict[str, Any]] = []
        pending = _pending_tool_uses(messages)
        if pending:
            user_content = yield from _execute_tools(ctx, pending, approvals, auto_approve)
        if user_message:
            user_content.append({"type": "text", "text": user_message})
        if not user_content:
            yield {"type": "error", "message": "Nothing to do: send a message."}
            return
        messages.append({"role": "user", "content": user_content})

        json_retries = 0
        for _ in range(MAX_TURNS):
            try:
                final = yield from stream_turn(system, messages)
                json_retries = 0
            except ValueError:
                # The SDK could not parse a streamed tool input at all; re-issue the turn
                json_retries += 1
                if json_retries > 2:
                    yield {"type": "messages", "messages": messages}
                    yield {"type": "error", "message": "The model kept producing malformed tool input. Please retry."}
                    return
                yield {"type": "notice", "message": "Malformed tool input received, retrying..."}
                continue

            content = sanitize_assistant_content([block.to_dict(mode="json") for block in final.content])
            messages.append({"role": "assistant", "content": content})

            if final.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in content if b.get("type") == "tool_use"]

            if final.stop_reason == "refusal":
                yield {"type": "messages", "messages": messages}
                yield {"type": "error", "message": "The model declined this request."}
                return
            if final.stop_reason == "max_tokens" and tool_uses:
                # A truncated tool input can look valid; never run it. Drop the turn.
                messages.pop()
                yield {"type": "messages", "messages": messages}
                yield {"type": "error", "message": "The response was cut off (max_tokens) while writing a tool call."}
                return
            if not tool_uses:
                yield {"type": "messages", "messages": messages}
                yield {"type": "done", "stop_reason": final.stop_reason}
                return

            # Announce complete tool inputs (they were streamed as partial JSON)
            for block in tool_uses:
                yield {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input", {})}

            gated = [] if auto_approve else _needs_approval(tool_uses)
            if gated:
                yield {"type": "messages", "messages": messages}
                yield {"type": "approval_required", "tools": gated}
                return

            results = yield from _execute_tools(ctx, tool_uses, approvals, auto_approve)
            messages.append({"role": "user", "content": results})

        yield {"type": "messages", "messages": messages}
        yield {"type": "error", "message": f"Stopped after {MAX_TURNS} steps. Send a message to continue."}

    except anthropic.AnthropicError as e:
        # Keep the work done so far (tool calls may already have changed files)
        yield {"type": "messages", "messages": close_dangling_tool_uses(messages)}
        yield {"type": "error", "message": describe_api_error(e)}


def describe_api_error(e: anthropic.AnthropicError) -> str:
    """User-facing message for an Anthropic SDK error."""
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic authentication failed. Set ANTHROPIC_API_KEY for the backend."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Please retry in a moment."
    if isinstance(e, anthropic.BadRequestError):
        return f"Invalid request: {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic API error ({e.status_code}): {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API."
    # e.g. missing credentials when the client is created
    return str(e)
