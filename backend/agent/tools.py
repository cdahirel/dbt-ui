"""Tools exposed to the dbt agent.

Each tool has a JSON schema (sent to Claude) and a Python executor. Tools run
with the same privileges as the regular dbt-ui endpoints and are confined to
the project directory.
"""
import fnmatch
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from utils.dbt_utils import get_dbt_env, parse_dbt_manifest
from utils.venv_utils import get_venv_dbt_path
from utils.subprocess_utils import run_command
from utils.operation_lock import acquire_lock, release_lock, get_lock_status

# Directories never worth showing to the agent
IGNORED_DIRS = {".git", ".dbt-ui-venv", "target", "dbt_packages", "logs", "node_modules", "__pycache__", ".venv"}

# Max characters returned by a single tool call (tool results go back into the context)
MAX_TOOL_OUTPUT = 30000

# dbt commands the agent may run; the ones that change the warehouse need approval
SAFE_DBT_COMMANDS = {"parse", "compile", "ls", "test", "deps", "debug"}
MUTATING_DBT_COMMANDS = {"run", "seed", "build", "snapshot"}

SELECTOR_PATTERN = re.compile(r'^[a-zA-Z0-9_.:+\-*/,@\s]*$')
TARGET_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]{1,100}$')

# Jinja allowed in run_query: only {{ ref('x') }} / {{ source('a', 'b') }}. Anything else
# (e.g. {% do run_query(...) %}) would execute arbitrary SQL while dbt compiles the query.
ALLOWED_JINJA = re.compile(r"""\{\{\s*(ref|source)\(\s*(['"])[\w.\-]+\2(\s*,\s*(['"])[\w.\-]+\4)?\s*\)\s*\}\}""")


class ToolError(Exception):
    """Raised by a tool executor; the message is returned to Claude as an error result."""


@dataclass
class ToolContext:
    project_path: Path
    env_vars: Dict[str, str]
    target: str = ""
    # How long a dbt command waits for another operation on the worktree to finish
    lock_wait_seconds: int = 0
    # Set when the run is stopped; a command still waiting for the lock is then abandoned
    cancelled: Optional[threading.Event] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    execute: Callable[[ToolContext, Dict[str, Any]], str]
    # Returns True if this particular call needs user approval (skipped when auto-approve is on)
    needs_approval: Callable[[Dict[str, Any]], bool] = lambda args: False
    # Returns True if this call needs approval even when auto-approve is on
    always_ask: Callable[[Dict[str, Any]], bool] = lambda args: False
    # True if a successful call may change project files or dbt artifacts
    changes_project: Callable[[Dict[str, Any]], bool] = lambda args: False

    @property
    def may_need_approval(self) -> bool:
        """Whether some calls of this tool can require approval (shown in the team editor)."""
        return self.needs_approval is not ToolSpec.needs_approval

    def must_ask(self, args: Dict[str, Any], auto_approve: bool) -> bool:
        """Whether this call has to wait for the user's decision."""
        return self.always_ask(args) or (self.needs_approval(args) and not auto_approve)


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    # Keep the tail: dbt puts errors and summaries at the end of its output
    return f"[... {len(text) - limit} characters truncated ...]\n" + text[-limit:]


def resolve_in_project(ctx: ToolContext, rel_path: str) -> Path:
    """Resolve a model-supplied relative path, rejecting anything outside the project."""
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        raise ToolError("Invalid path")
    root = ctx.project_path.resolve()
    target = (root / rel_path.strip().lstrip("/")).resolve()
    if target != root and not target.is_relative_to(root):
        raise ToolError("Path escapes the project directory")
    # .dbt-ui holds dbt-ui settings such as agent teams: agents must not change their own config
    if any(part in (".git", ".dbt-ui-venv", ".dbt-ui") for part in target.relative_to(root).parts):
        raise ToolError("Access to this directory is not allowed")
    return target


def dbt_executable(ctx: ToolContext) -> str:
    try:
        return str(get_venv_dbt_path(ctx.project_path))
    except FileNotFoundError:
        return "dbt"


def load_manifest(ctx: ToolContext) -> Dict[str, Any]:
    manifest = parse_dbt_manifest(ctx.project_path)
    if not manifest:
        raise ToolError("target/manifest.json not found. Run run_dbt with command 'parse' first.")
    return manifest


def all_manifest_nodes(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    nodes = dict(manifest.get("nodes", {}))
    nodes.update(manifest.get("sources", {}))
    return nodes


def find_node(manifest: Dict[str, Any], name: str) -> Optional[tuple]:
    """Find a node by unique_id, name, or 'source_name.table' for sources."""
    nodes = all_manifest_nodes(manifest)
    if name in nodes:
        return name, nodes[name]
    for unique_id, node in nodes.items():
        if node.get("resource_type") == "test":
            continue
        if node.get("name") == name:
            return unique_id, node
        if node.get("resource_type") == "source" and f"{node.get('source_name')}.{node.get('name')}" == name:
            return unique_id, node
    return None


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

def list_files(ctx: ToolContext, args: Dict[str, Any]) -> str:
    base = resolve_in_project(ctx, args.get("directory", "") or "")
    max_depth = min(max(int(args.get("max_depth", 3)), 1), 8)
    if not base.is_dir():
        raise ToolError(f"Not a directory: {args.get('directory')}")

    root = ctx.project_path.resolve()
    lines: List[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        depth = len(current.relative_to(base).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        rel = current.relative_to(root)
        indent = "  " * depth
        if current != base:
            lines.append(f"{'  ' * (depth - 1)}{rel.name}/")
        for filename in sorted(filenames):
            lines.append(f"{indent}{filename}")
        if len(lines) > 2000:
            lines.append("[... listing truncated, use a narrower directory ...]")
            break
    return "\n".join(lines) or "(empty directory)"


def read_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = resolve_in_project(ctx, args["path"])
    if not path.is_file():
        raise ToolError(f"File not found: {args['path']}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("Binary file, cannot be read as text")

    lines = content.splitlines()
    start = max(int(args.get("start_line", 1)), 1)
    end = int(args.get("end_line", 0)) or len(lines)
    selected = lines[start - 1:end]
    numbered = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(selected, start=start))
    header = f"{args['path']} (lines {start}-{min(end, len(lines))} of {len(lines)})\n"
    return truncate(header + numbered)


def search_files(ctx: ToolContext, args: Dict[str, Any]) -> str:
    try:
        pattern = re.compile(args["pattern"], re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")
    glob = args.get("glob") or "*"
    root = ctx.project_path.resolve()

    matches: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for filename in filenames:
            file_path = Path(dirpath) / filename
            rel = str(file_path.relative_to(root))
            if not (fnmatch.fnmatch(filename, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line_no, line in enumerate(f, start=1):
                        if pattern.search(line):
                            matches.append(f"{rel}:{line_no}: {line.rstrip()[:300]}")
                            if len(matches) >= 200:
                                matches.append("[... more matches truncated, refine the pattern ...]")
                                return "\n".join(matches)
            except (UnicodeDecodeError, OSError):
                continue
    return "\n".join(matches) or "No matches"


def write_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = resolve_in_project(ctx, args["path"])
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return f"{'Updated' if existed else 'Created'} {args['path']} ({len(args['content'])} characters)"


def edit_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = resolve_in_project(ctx, args["path"])
    if not path.is_file():
        raise ToolError(f"File not found: {args['path']}")
    content = path.read_text(encoding="utf-8")
    count = content.count(args["old_str"])
    if count == 0:
        raise ToolError("old_str not found in file. Read the file again and copy the text exactly.")
    if count > 1:
        raise ToolError(f"old_str occurs {count} times; include more surrounding context to make it unique.")
    path.write_text(content.replace(args["old_str"], args["new_str"], 1), encoding="utf-8")
    return f"Edited {args['path']}"


def list_nodes(ctx: ToolContext, args: Dict[str, Any]) -> str:
    manifest = load_manifest(ctx)
    resource_type = args.get("resource_type") or ""
    name_filter = (args.get("name_contains") or "").lower()

    rows: List[str] = []
    for unique_id, node in sorted(all_manifest_nodes(manifest).items()):
        rtype = node.get("resource_type")
        if resource_type and rtype != resource_type:
            continue
        if not resource_type and rtype == "test":
            continue  # tests are noisy; ask for them explicitly
        if name_filter and name_filter not in unique_id.lower():
            continue
        parents = [d.split(".")[-1] for d in node.get("depends_on", {}).get("nodes", [])]
        materialized = node.get("config", {}).get("materialized", "")
        description = (node.get("description") or "").strip().replace("\n", " ")[:100]
        parts = [unique_id, node.get("original_file_path", "")]
        if materialized:
            parts.append(f"materialized={materialized}")
        if parents:
            parts.append(f"depends_on=[{', '.join(parents)}]")
        if description:
            parts.append(f"desc={description}")
        rows.append(" | ".join(parts))
    return truncate("\n".join(rows) or "No matching nodes")


def get_node(ctx: ToolContext, args: Dict[str, Any]) -> str:
    manifest = load_manifest(ctx)
    found = find_node(manifest, args["name"])
    if not found:
        raise ToolError(f"Node '{args['name']}' not found in manifest (try list_nodes)")
    unique_id, node = found

    child_map = manifest.get("child_map", {})
    children = child_map.get(unique_id, [])
    tests = [c for c in children if c.startswith("test.")]
    downstream = [c for c in children if not c.startswith("test.")]

    lines = [
        f"unique_id: {unique_id}",
        f"resource_type: {node.get('resource_type')}",
        f"file: {node.get('original_file_path')}",
        f"relation: {node.get('relation_name') or '.'.join(filter(None, [node.get('database'), node.get('schema'), node.get('alias') or node.get('identifier')]))}",
    ]
    config = node.get("config", {})
    for key in ("materialized", "unique_key", "incremental_strategy", "tags"):
        if config.get(key):
            lines.append(f"{key}: {config[key]}")
    if node.get("description"):
        lines.append(f"description: {node['description']}")
    lines.append(f"depends_on: {node.get('depends_on', {}).get('nodes', [])}")
    lines.append(f"downstream: {downstream}")
    lines.append(f"tests: {tests}")
    columns = node.get("columns", {})
    if columns:
        lines.append("columns:")
        for col_name, col in columns.items():
            extra = f" ({col['data_type']})" if col.get("data_type") else ""
            desc = f" - {col['description']}" if col.get("description") else ""
            lines.append(f"  - {col_name}{extra}{desc}")
    else:
        lines.append("columns: none documented")
    return truncate("\n".join(lines))


def get_compiled_sql(ctx: ToolContext, args: Dict[str, Any]) -> str:
    manifest = load_manifest(ctx)
    found = find_node(manifest, args["name"])
    if not found:
        raise ToolError(f"Node '{args['name']}' not found in manifest")
    _, node = found
    compiled_path = node.get("compiled_path")
    if compiled_path:
        full = ctx.project_path / compiled_path
        if full.is_file():
            return truncate(full.read_text(encoding="utf-8"))
    raise ToolError("No compiled SQL found. Run run_dbt with command 'compile' and this node as selector first.")


def run_dbt(ctx: ToolContext, args: Dict[str, Any]) -> str:
    command = args["command"]
    if command not in SAFE_DBT_COMMANDS | MUTATING_DBT_COMMANDS:
        raise ToolError(f"Unsupported dbt command: {command}")
    select = (args.get("select") or "").strip()
    if not SELECTOR_PATTERN.match(select):
        raise ToolError("Selector contains forbidden characters")

    cmd = [dbt_executable(ctx), command, "--project-dir", str(ctx.project_path), "--profiles-dir", str(ctx.project_path)]
    if select and command not in ("parse", "deps", "debug"):
        cmd.extend(["--select", select])
    # An explicit target (e.g. a deployment to prod) overrides the one selected in the UI
    target = (args.get("target") or "").strip() or ctx.target
    if target and not TARGET_PATTERN.match(target):
        raise ToolError("Invalid target name")
    if target and command != "deps":
        cmd.extend(["--target", target])
    if args.get("full_refresh") and command in ("run", "seed", "build"):
        cmd.append("--full-refresh")
    operation = f"Assistant: dbt {command} {select}".strip() + (f" (target {target})" if target else "")
    return run_locked(ctx, cmd, operation, timeout=600)


def run_query(ctx: ToolContext, args: Dict[str, Any]) -> str:
    sql = args["sql"].strip().rstrip(";").strip()
    if ";" in sql:
        raise ToolError("Only a single SELECT statement is allowed")
    remaining = ALLOWED_JINJA.sub("", sql)
    if any(token in remaining for token in ("{{", "{%", "{#")):
        raise ToolError("Only {{ ref('...') }} and {{ source('...', '...') }} Jinja is allowed in run_query")
    limit = min(max(int(args.get("limit", 20)), 1), 500)
    cmd = [
        dbt_executable(ctx), "show", "--inline", sql, "--limit", str(limit),
        "--project-dir", str(ctx.project_path), "--profiles-dir", str(ctx.project_path),
    ]
    if ctx.target:
        cmd.extend(["--target", ctx.target])
    return run_locked(ctx, cmd, "Assistant: dbt show", timeout=180)


def run_locked(ctx: ToolContext, cmd: List[str], operation: str, timeout: int) -> str:
    """Run a dbt command while holding the worktree lock shared with the rest of the UI."""
    path_str = str(ctx.project_path)
    deadline = time.monotonic() + ctx.lock_wait_seconds
    while not acquire_lock(path_str, operation):
        if ctx.cancelled is not None and ctx.cancelled.is_set():
            raise ToolError("Cancelled while waiting for another operation to finish")
        if time.monotonic() >= deadline:
            raise ToolError(f"Another operation is already running: {get_lock_status(path_str)['operation']}. Try again later.")
        time.sleep(1)
    try:
        result = run_command(cmd, ctx.project_path, timeout=timeout, env=get_dbt_env(ctx.project_path, ctx.env_vars))
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout} seconds")
    except FileNotFoundError:
        raise ToolError("dbt executable not found. Create the virtual environment from the sidebar first.")
    finally:
        release_lock(path_str)

    output = (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip()
    status = "succeeded" if result.success else f"failed (exit code {result.returncode})"
    return truncate(f"Command {status}.\n{output}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_files",
        description="List files in the dbt project as an indented tree. Skips target/, dbt_packages/, logs/ and hidden directories.",
        input_schema={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Directory relative to the project root. Empty for the root."},
                "max_depth": {"type": "integer", "description": "How many directory levels to descend (default 3)."},
            },
            "required": [],
        },
        execute=list_files,
    ),
    ToolSpec(
        name="read_file",
        description="Read a text file from the project, with line numbers. Use start_line/end_line for large files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
        execute=read_file,
    ),
    ToolSpec(
        name="search_files",
        description="Search project files with a case-insensitive regular expression. Returns file:line: text matches.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "glob": {"type": "string", "description": "Optional filename or path glob, e.g. '*.sql' or 'models/staging/*'."},
            },
            "required": ["pattern"],
        },
        execute=search_files,
    ),
    ToolSpec(
        name="write_file",
        description="Create or fully overwrite a file in the project. Prefer edit_file for changes to existing files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "content": {"type": "string", "description": "Complete new file content."},
            },
            "required": ["path", "content"],
        },
        execute=write_file,
        needs_approval=lambda args: True,
        changes_project=lambda args: True,
    ),
    ToolSpec(
        name="edit_file",
        description="Replace one exact, unique occurrence of old_str with new_str in an existing file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "old_str": {"type": "string", "description": "Exact text to replace; must occur exactly once."},
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
        },
        execute=edit_file,
        needs_approval=lambda args: True,
        changes_project=lambda args: True,
    ),
    ToolSpec(
        name="list_nodes",
        description="List dbt resources from target/manifest.json with their file, materialization, parents and description. Tests are excluded unless resource_type is 'test'.",
        input_schema={
            "type": "object",
            "properties": {
                "resource_type": {"type": "string", "enum": ["model", "seed", "source", "snapshot", "test", "analysis"]},
                "name_contains": {"type": "string", "description": "Case-insensitive filter on the unique_id."},
            },
            "required": [],
        },
        execute=list_nodes,
    ),
    ToolSpec(
        name="get_node",
        description="Get details of one dbt resource: relation, config, columns, upstream/downstream nodes and attached tests.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Node name (e.g. 'stg_orders'), 'source_name.table', or unique_id."},
            },
            "required": ["name"],
        },
        execute=get_node,
    ),
    ToolSpec(
        name="get_compiled_sql",
        description="Return the compiled SQL of a model or test from target/compiled (run 'compile' first if missing).",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        execute=get_compiled_sql,
    ),
    ToolSpec(
        name="run_dbt",
        description=(
            "Run a dbt command in the project. parse/compile/ls/test/deps/debug are read-only for the warehouse; "
            "run/seed/build/snapshot materialize data and require user approval. Use 'parse' after editing to validate, "
            "and a selector to keep runs small. Without `target`, the target selected in the UI is used."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": sorted(SAFE_DBT_COMMANDS | MUTATING_DBT_COMMANDS)},
                "select": {"type": "string", "description": "dbt node selector, e.g. 'stg_orders+' or 'tag:daily'."},
                "full_refresh": {"type": "boolean"},
                "target": {"type": "string", "description": "profiles.yml target to use instead of the one selected in the UI, e.g. 'prod' for a deployment."},
            },
            "required": ["command"],
        },
        execute=run_dbt,
        needs_approval=lambda args: args.get("command") in MUTATING_DBT_COMMANDS,
        # Any command on an explicitly chosen target (e.g. a prod deployment) is confirmed by the user
        always_ask=lambda args: bool((args.get("target") or "").strip()),
        # ls/test/debug leave files and the manifest unchanged: no UI refresh needed
        changes_project=lambda args: args.get("command") not in ("ls", "test", "debug"),
    ),
    ToolSpec(
        name="run_query",
        description="Run a single read-only SELECT against the warehouse with `dbt show --inline` and return the rows as a text table. The only Jinja allowed is {{ ref('model') }} and {{ source('source', 'table') }}.",
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "description": "Max rows (default 20, max 500)."},
            },
            "required": ["sql"],
        },
        execute=run_query,
    ),
]

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}

_JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def validate_input(spec: Any, args: Any) -> Optional[str]:
    """Validate tool input against its schema. Returns an error message or None.

    Tool inputs are streamed eagerly, so the API does not validate them for us.
    """
    if not isinstance(args, dict):
        return "Tool input must be a JSON object"
    props = spec.input_schema.get("properties", {})
    for key in spec.input_schema.get("required", []):
        if key not in args:
            return f"Missing required parameter '{key}'"
    for key, value in args.items():
        if key not in props:
            return f"Unknown parameter '{key}'"
        expected = _JSON_TYPES.get(props[key].get("type"))
        if expected is int and isinstance(value, bool):
            return f"Parameter '{key}' must be an integer"
        if expected and not isinstance(value, expected):
            return f"Parameter '{key}' must be of type {props[key]['type']}"
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"Parameter '{key}' must be one of {props[key]['enum']}"
    return None


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "eager_input_streaming": True,
        }
        for tool in TOOLS
    ]
