"""Team mode: a lead agent plans the work and delegates it to specialized agents.

The lead explores the project and calls `run_subagents` to dispatch tasks to role
agents (test writer, developer, reviewer, documenter). Tasks in one call run in
parallel threads; each sub-agent has its own conversation and tool set and reports
back to the lead. Unlike the single assistant, a team run keeps its request open:
calls that need approval block until the user answers via /api/agent/approve.
"""
import itertools
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from agent.runner import build_system_prompt, close_dangling_tool_uses, describe_api_error, sanitize_assistant_content, stream_turn
from agent.tools import TOOLS_BY_NAME, ToolContext, ToolError, ToolSpec, validate_input

LEAD_MAX_TURNS = 30
SUBAGENT_MAX_TURNS = 40
MAX_PARALLEL_TASKS = 6
APPROVAL_TIMEOUT_SECONDS = 30 * 60
LOCK_WAIT_SECONDS = 600  # sub-agents queue up for the worktree lock instead of failing

READ_TOOLS = ["list_files", "read_file", "search_files", "list_nodes", "get_node", "get_compiled_sql"]
WRITE_TOOLS = ["write_file", "edit_file"]

ROLES: Dict[str, Dict[str, Any]] = {
    "tester": {
        "label": "Test writer",
        "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt", "run_query"],
        "prompt": """Your role: TEST WRITER (test-driven development).
Write the tests that specify the expected behavior BEFORE the implementation exists or changes:
- dbt unit tests (`unit_tests:` in YAML, dbt >= 1.8) with small given/expect fixtures for transformation logic;
- data tests on the model's columns (unique, not_null, accepted_values, relationships) and singular tests in tests/ for business rules.
Follow the project's existing test conventions and file layout. Do not write or change model SQL: that is the developer's job.
Validate that the YAML parses (run_dbt parse). Report the exact test names and the files you created so the developer can run them.""",
    },
    "developer": {
        "label": "Developer",
        "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt", "run_query"],
        "prompt": """Your role: DEVELOPER.
Implement or change the model(s) you are assigned, following the project's conventions (layering, naming, CTE style, ref/source).
If tests exist for your model, make them pass: compile, then build/test with a narrow selector (e.g. `--select model_name`), read failures, fix, and repeat.
Do not weaken or delete tests to make them pass; if a test looks wrong, say so in your report.""",
    },
    "reviewer": {
        "label": "Reviewer",
        "tools": READ_TOOLS + ["run_dbt", "run_query"],
        "prompt": """Your role: REVIEWER. You cannot edit files.
Review the assigned models and tests for correctness (joins, grain, duplicates, null handling, incremental logic), performance, test coverage and adherence to project conventions.
Compile and run the tests to confirm the current state. Report concrete findings with file and line, ordered by severity, and say clearly whether the work is ready.""",
    },
    "documenter": {
        "label": "Documenter",
        "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt"],
        "prompt": """Your role: DOCUMENTER.
Write or improve model and column descriptions in the YAML properties files (and doc blocks if the project uses them), matching the project's style.
Do not change SQL logic. Validate with run_dbt parse.""",
    },
}

SUBAGENT_GUIDELINES = """You are one member of an agent team working on a task assigned by the team lead.
You cannot ask the user or the lead questions: make reasonable decisions and state them.
Other agents may be working on other models in parallel: only touch the files your task is about.
Finish with a concise report: what you did, files created or changed, validation results (commands and outcome), and open issues."""

LEAD_GUIDELINES = f"""TEAM MODE: you are the team lead. You do not edit files yourself; you plan the work and delegate it with run_subagents to specialized agents:
{chr(10).join(f"- {name}: {role['label']}" for name, role in ROLES.items())}

How to lead:
- First explore enough of the project to plan (files, existing models, conventions, sources). Ask the user a question only if the request is genuinely ambiguous.
- Split the work per model. Tasks given in a single run_subagents call run in parallel, so only parallelize tasks that touch different files (one agent per model or per properties file).
- Default workflow, test-driven: 1) testers write the tests for each model, 2) developers implement each model until its tests pass, 3) a reviewer checks the result. Skip phases that do not apply (e.g. a pure documentation request).
- Sub-agents do not see this conversation: give each one complete, self-contained instructions (goal, model name and file path, relevant columns/sources, conventions, the test names to satisfy, and what to report).
- Read the reports. If tests fail or the reviewer finds problems, dispatch follow-up tasks.
- Finish with a summary for the user: what each agent did, files changed, test status, and anything left to decide."""

RUN_SUBAGENTS_SPEC = ToolSpec(
    name="run_subagents",
    description=(
        "Dispatch tasks to specialized agents. All tasks in one call run in parallel and the call returns when "
        f"they are all finished, with each agent's report. Max {MAX_PARALLEL_TASKS} tasks per call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "enum": list(ROLES)},
                        "title": {"type": "string", "description": "Short label shown to the user, e.g. 'Tests for stg_orders'."},
                        "instructions": {"type": "string", "description": "Complete, self-contained instructions for the agent."},
                    },
                    "required": ["role", "title", "instructions"],
                },
            },
        },
        "required": ["tasks"],
    },
    execute=lambda ctx, args: "",  # handled by TeamSession
)

LEAD_TOOLS = READ_TOOLS + ["run_dbt"]


class Cancelled(Exception):
    pass


def _tool_definition(spec: ToolSpec) -> Dict[str, Any]:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema, "eager_input_streaming": True}


def _final_text(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message["role"] == "assistant":
            texts = [b.get("text", "") for b in message["content"] if b.get("type") == "text"]
            if any(t.strip() for t in texts):
                return "\n".join(texts).strip()
    return "(no report)"


class TeamSession:
    """One team run: an event queue for the SSE response, approvals and cancellation."""

    def __init__(self, ctx: ToolContext, auto_approve: bool):
        self.id = uuid.uuid4().hex
        self.ctx = ctx
        self.auto_approve = auto_approve
        self.events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.cancelled = threading.Event()
        self._approvals: Dict[str, Tuple[threading.Event, Dict[str, bool]]] = {}
        self._approvals_lock = threading.Lock()
        self._agent_ids = itertools.count(1)
        self.system = build_system_prompt(ctx.project_path)

    # -- events, approvals, cancellation -----------------------------------

    def emit(self, agent: str, event: Dict[str, Any]) -> None:
        self.events.put({**event, "agent": agent})

    def resolve_approval(self, tool_id: str, approved: bool) -> bool:
        with self._approvals_lock:
            pending = self._approvals.get(tool_id)
        if not pending:
            return False
        done, decision = pending
        decision["approved"] = approved
        done.set()
        return True

    def cancel(self) -> None:
        self.cancelled.set()
        with self._approvals_lock:
            for done, _ in self._approvals.values():
                done.set()

    def _wait_for_approval(self, agent: str, block: Dict[str, Any]) -> bool:
        done, decision = threading.Event(), {"approved": False}
        with self._approvals_lock:
            self._approvals[block["id"]] = (done, decision)
        self.emit(agent, {"type": "approval_required", "tools": [{"id": block["id"], "name": block["name"], "input": block.get("input", {})}]})
        try:
            done.wait(APPROVAL_TIMEOUT_SECONDS)
            return decision["approved"] and not self.cancelled.is_set()
        finally:
            with self._approvals_lock:
                self._approvals.pop(block["id"], None)

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    # -- agent loop ----------------------------------------------------------

    def _execute(self, agent: str, block: Dict[str, Any], allowed: List[str], lead: bool) -> Dict[str, Any]:
        tool_id, name, args = block["id"], block["name"], block.get("input", {})
        spec = RUN_SUBAGENTS_SPEC if lead and name == "run_subagents" else TOOLS_BY_NAME.get(name) if name in allowed else None
        is_error = False

        if spec is None:
            output, is_error = f"Tool not available to this agent: {name}", True
        elif (error := validate_input(spec, args)) is not None:
            output, is_error = f"Invalid input: {error}", True
        elif spec is RUN_SUBAGENTS_SPEC:
            self.emit(agent, {"type": "tool_running", "id": tool_id, "name": name})
            output, is_error = self._run_subagents(tool_id, args["tasks"])
        elif spec.needs_approval(args) and not self.auto_approve and not self._wait_for_approval(agent, block):
            self._check_cancelled()
            output, is_error = "The user declined this action.", True
        else:
            self._check_cancelled()
            self.emit(agent, {"type": "tool_running", "id": tool_id, "name": name})
            try:
                output = spec.execute(self.ctx, args)
                if spec.changes_project(args):
                    self.emit(agent, {"type": "project_changed", "tool": name, "path": args.get("path")})
            except ToolError as e:
                output, is_error = str(e), True
            except Exception as e:
                output, is_error = f"Tool failed: {e}", True

        self.emit(agent, {"type": "tool_result", "id": tool_id, "name": name, "content": output, "is_error": is_error})
        result = {"type": "tool_result", "tool_use_id": tool_id, "content": output}
        if is_error:
            result["is_error"] = True
        return result

    def run_loop(self, agent: str, system: str, allowed: List[str], messages: List[Dict[str, Any]], max_turns: int, lead: bool = False) -> List[Dict[str, Any]]:
        """Run one agent until it stops calling tools.

        `messages` is updated in place, so the caller keeps the work done so far if the
        loop is stopped or fails. Returns the same list.
        """
        tools = [_tool_definition(TOOLS_BY_NAME[name]) for name in allowed]
        if lead:
            tools.append(_tool_definition(RUN_SUBAGENTS_SPEC))
        json_retries = 0

        for _ in range(max_turns):
            self._check_cancelled()
            try:
                turn = stream_turn(system, messages, tools)
                try:
                    while True:
                        self._check_cancelled()
                        self.emit(agent, next(turn))
                except StopIteration as stop:
                    final = stop.value
                finally:
                    turn.close()  # releases the HTTP stream if we stop early
                json_retries = 0
            except ValueError:
                json_retries += 1
                if json_retries > 2:
                    raise ToolError("The model kept producing malformed tool input.")
                self.emit(agent, {"type": "notice", "message": "Malformed tool input received, retrying..."})
                continue

            content = sanitize_assistant_content([b.to_dict(mode="json") for b in final.content])
            messages.append({"role": "assistant", "content": content})
            if final.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if final.stop_reason == "refusal":
                raise ToolError("The model declined this request.")
            if final.stop_reason == "max_tokens" and tool_uses:
                messages.pop()
                raise ToolError("The response was cut off (max_tokens) while writing a tool call.")
            if not tool_uses:
                return messages

            for block in tool_uses:
                self.emit(agent, {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input", {})})
            results = [self._execute(agent, block, allowed, lead) for block in tool_uses]
            messages.append({"role": "user", "content": results})

        raise ToolError(f"Stopped after {max_turns} steps.")

    def _run_subagent(self, agent: str, parent_tool_id: str, task: Dict[str, Any]) -> str:
        role = ROLES[task["role"]]
        self.emit(agent, {"type": "agent_start", "role": task["role"], "label": role["label"], "title": task["title"], "parent_tool_id": parent_tool_id})
        system = f"{self.system}\n\n{SUBAGENT_GUIDELINES}\n\n{role['prompt']}"
        status, report = "done", ""
        try:
            history = self.run_loop(agent, system, role["tools"], [{"role": "user", "content": task["instructions"]}], SUBAGENT_MAX_TURNS)
            report = _final_text(history)
        except Cancelled:
            status, report = "cancelled", "Cancelled by the user."
        except ToolError as e:
            status, report = "error", str(e)
        except anthropic.AnthropicError as e:
            status, report = "error", describe_api_error(e)
        except Exception as e:
            status, report = "error", f"Agent failed: {e}"
        self.emit(agent, {"type": "agent_done", "status": status, "summary": report})
        return f"## {role['label']}: {task['title']} [{status}]\n{report}"

    def _run_subagents(self, tool_id: str, tasks: Any) -> Tuple[str, bool]:
        if not tasks or len(tasks) > MAX_PARALLEL_TASKS:
            return f"Provide between 1 and {MAX_PARALLEL_TASKS} tasks.", True
        for task in tasks:
            if not isinstance(task, dict) or task.get("role") not in ROLES or not all(isinstance(task.get(k), str) and task.get(k) for k in ("title", "instructions")):
                return f"Each task needs a role ({', '.join(ROLES)}), a title and instructions.", True

        # Ids are unique across the runs of a conversation (the UI keeps every run's lanes)
        agents = [f"a{next(self._agent_ids)}-{self.id[:8]}" for _ in tasks]
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            reports = list(pool.map(lambda pair: self._run_subagent(pair[0], tool_id, pair[1]), zip(agents, tasks)))
        self._check_cancelled()
        return "\n\n".join(reports), False

    def run(self, messages: List[Dict[str, Any]], user_message: str) -> None:
        """Run the lead agent. Pushes events to the queue, then None when finished."""
        try:
            if not user_message:
                self.emit("lead", {"type": "error", "message": "Nothing to do: send a message."})
                return
            history = list(messages) + [{"role": "user", "content": [{"type": "text", "text": user_message}]}]
            system = f"{self.system}\n\n{LEAD_GUIDELINES}"
            try:
                self.run_loop("lead", system, LEAD_TOOLS, history, LEAD_MAX_TURNS, lead=True)
                self.emit("lead", {"type": "messages", "messages": history})
                self.emit("lead", {"type": "done", "stop_reason": "end_turn"})
                return
            except Cancelled:
                failure = {"type": "notice", "message": "Stopped."}
            except ToolError as e:
                failure = {"type": "error", "message": str(e)}
            except anthropic.AnthropicError as e:
                failure = {"type": "error", "message": describe_api_error(e)}
            # Keep the work done so far: sub-agents may already have changed files
            self.emit("lead", {"type": "messages", "messages": close_dangling_tool_uses(history)})
            self.emit("lead", failure)
        except Exception as e:
            self.emit("lead", {"type": "error", "message": f"Team run failed: {e}"})
        finally:
            self.events.put(None)


_sessions: Dict[str, TeamSession] = {}
_sessions_lock = threading.Lock()


def start_session(ctx: ToolContext, auto_approve: bool, messages: List[Dict[str, Any]], user_message: str) -> TeamSession:
    ctx.lock_wait_seconds = LOCK_WAIT_SECONDS
    session = TeamSession(ctx, auto_approve)
    ctx.cancelled = session.cancelled
    with _sessions_lock:
        _sessions[session.id] = session
    threading.Thread(target=session.run, args=(messages, user_message), daemon=True).start()
    return session


def get_session(session_id: str) -> Optional[TeamSession]:
    with _sessions_lock:
        return _sessions.get(session_id)


def end_session(session: TeamSession) -> None:
    session.cancel()
    with _sessions_lock:
        _sessions.pop(session.id, None)
