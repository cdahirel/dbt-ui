"""Agent team configurations: the built-in default team and user-defined teams.

A team is a set of roles (instructions, allowed tools, optional model) plus a workflow
that tells the lead how to sequence them. Custom teams are stored per project in
`.dbt-ui/agent-teams.json`, so they can be committed and shared with the project.
"""
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.tools import TOOLS_BY_NAME

TEAMS_FILE = Path(".dbt-ui") / "agent-teams.json"
DEFAULT_TEAM_ID = "default"

# "" means the assistant's configured model (DBT_UI__AGENT_MODEL)
ROLE_MODELS = ["", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]

MAX_ROLES = 8
ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

READ_TOOLS = ["list_files", "read_file", "search_files", "list_nodes", "get_node", "get_compiled_sql"]
WRITE_TOOLS = ["write_file", "edit_file"]

DEFAULT_TEAM: Dict[str, Any] = {
    "id": DEFAULT_TEAM_ID,
    "name": "dbt TDD team",
    "description": "Test writer, developer, reviewer, ops and documenter working test-first.",
    "workflow": """Test-driven, per model:
1. Test writers write the tests for each model.
2. Developers implement each model until its tests pass.
3. A reviewer checks the result; dispatch follow-up work if needed.
Skip phases that do not apply (e.g. a pure documentation request goes to the documenter only).
Setup, environment, packages, CI/CD and deployment requests go to ops. Deploy (e.g. build on the production target) only when the user asks for it, after the changes are reviewed.""",
    "roles": [
        {
            "id": "tester",
            "label": "Test writer",
            "description": "Writes unit tests and data tests first; never changes model SQL.",
            "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt", "run_query"],
            "model": "",
            "instructions": """Test-driven development: write the tests that specify the expected behavior BEFORE the implementation exists or changes:
- dbt unit tests (`unit_tests:` in YAML, dbt >= 1.8) with small given/expect fixtures for transformation logic;
- data tests on the model's columns (unique, not_null, accepted_values, relationships) and singular tests in tests/ for business rules.
Follow the project's existing test conventions and file layout. Do not write or change model SQL: that is the developer's job.
Validate that the YAML parses (run_dbt parse). Report the exact test names and the files you created so the developer can run them.""",
        },
        {
            "id": "developer",
            "label": "Developer",
            "description": "Implements models until their tests pass.",
            "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt", "run_query"],
            "model": "",
            "instructions": """Implement or change the model(s) you are assigned, following the project's conventions (layering, naming, CTE style, ref/source).
If tests exist for your model, make them pass: compile, then build/test with a narrow selector (e.g. `--select model_name`), read failures, fix, and repeat.
Do not weaken or delete tests to make them pass; if a test looks wrong, say so in your report.""",
        },
        {
            "id": "reviewer",
            "label": "Reviewer",
            "description": "Reviews models and tests, runs them, reports findings. Read-only.",
            "tools": READ_TOOLS + ["run_dbt", "run_query"],
            "model": "",
            "instructions": """You cannot edit files.
Review the assigned models and tests for correctness (joins, grain, duplicates, null handling, incremental logic), performance, test coverage and adherence to project conventions.
Compile and run the tests to confirm the current state. Report concrete findings with file and line, ordered by severity, and say clearly whether the work is ready.""",
        },
        {
            "id": "ops",
            "label": "Ops / Infra",
            "description": "Gets the project running and deployed: profiles and targets, packages, environment variables, CI/CD, scheduled jobs, deployments.",
            "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt"],
            "model": "",
            "instructions": """Make the project easy to run and to deploy.
Setup and diagnostics: check the connection and configuration with run_dbt debug, install packages with run_dbt deps, verify that the project parses; fix profiles.yml targets, packages.yml / dependencies.yml and dbt_project.yml settings.
Secrets: never write credentials in files. Use {{ env_var('NAME') }} in profiles.yml and list the variables the user must set (dbt-ui's environment variables panel, or the CI secrets).
CI/CD and scheduling: write or update pipeline files inside the project (e.g. a GitHub Actions workflow running `dbt deps`, `dbt build` with slim CI on modified models, docs generation; selectors.yml for scheduled jobs). Follow what the repository already uses.
Deployments: run_dbt with `target` set to the production target (e.g. build --select <models> --target prod) only when the task explicitly asks for a deployment; keep the selection as narrow as the task allows and never use full_refresh unless asked. The user approves every deployment.
Report what you changed, the commands you ran and their outcome, and any manual step left for the user (secrets to add, schedules to enable).""",
        },
        {
            "id": "documenter",
            "label": "Documenter",
            "description": "Writes model and column descriptions; never changes SQL.",
            "tools": READ_TOOLS + WRITE_TOOLS + ["run_dbt"],
            "model": "",
            "instructions": """Write or improve model and column descriptions in the YAML properties files (and doc blocks if the project uses them), matching the project's style.
Do not change SQL logic. Validate with run_dbt parse.""",
        },
    ],
}

# Field -> max length
_TEAM_LIMITS = {"name": 60, "description": 300, "workflow": 4000}
_ROLE_LIMITS = {"label": 40, "description": 200, "instructions": 6000}


class TeamValidationError(ValueError):
    pass


def available_tools() -> List[Dict[str, Any]]:
    """Tools a role can be given, for the team editor."""
    return [
        {"name": spec.name, "description": spec.description, "may_need_approval": spec.name in ("write_file", "edit_file", "run_dbt")}
        for spec in TOOLS_BY_NAME.values()
    ]


def _str(value: Any, field: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TeamValidationError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise TeamValidationError(f"{field} is required")
    if len(value) > limit:
        raise TeamValidationError(f"{field} is too long (max {limit} characters)")
    return value


def validate_team(team: Any) -> Dict[str, Any]:
    """Validate and normalize a user-submitted team. Raises TeamValidationError."""
    if not isinstance(team, dict):
        raise TeamValidationError("Team must be an object")
    clean: Dict[str, Any] = {
        "name": _str(team.get("name"), "Team name", _TEAM_LIMITS["name"]),
        "description": _str(team.get("description", ""), "Team description", _TEAM_LIMITS["description"], required=False),
        "workflow": _str(team.get("workflow", ""), "Workflow", _TEAM_LIMITS["workflow"], required=False),
    }
    team_id = team.get("id") or ""
    if team_id and (not isinstance(team_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", team_id)):
        raise TeamValidationError("Invalid team id")
    clean["id"] = team_id

    roles = team.get("roles")
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES:
        raise TeamValidationError(f"A team needs between 1 and {MAX_ROLES} roles")
    seen = set()
    clean["roles"] = []
    for role in roles:
        if not isinstance(role, dict):
            raise TeamValidationError("Each role must be an object")
        label = _str(role.get("label"), "Role name", _ROLE_LIMITS["label"])
        role_id = role.get("id") or ""
        if not isinstance(role_id, str) or not ROLE_ID_PATTERN.match(role_id):
            raise TeamValidationError(f"Invalid id for role '{label}' (lowercase letters, digits and _)")
        if role_id in seen:
            raise TeamValidationError(f"Two roles use the id '{role_id}'")
        seen.add(role_id)
        tools = role.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) and t in TOOLS_BY_NAME for t in tools):
            raise TeamValidationError(f"Role '{label}' has an unknown tool")
        model = role.get("model", "")
        if model not in ROLE_MODELS:
            raise TeamValidationError(f"Role '{label}' has an unsupported model")
        clean["roles"].append({
            "id": role_id,
            "label": label,
            "description": _str(role.get("description", ""), f"Description of '{label}'", _ROLE_LIMITS["description"], required=False),
            "instructions": _str(role.get("instructions"), f"Instructions of '{label}'", _ROLE_LIMITS["instructions"]),
            "tools": [t for t in TOOLS_BY_NAME if t in tools],  # stable order, no duplicates
            "model": model,
        })
    return clean


def _teams_path(project_path: Path) -> Path:
    return project_path / TEAMS_FILE


def load_custom_teams(project_path: Path) -> List[Dict[str, Any]]:
    """Custom teams of a project. Invalid entries are skipped rather than breaking the panel."""
    path = _teams_path(project_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        print(f"[agent-teams] Could not read {path}: {e}")
        return []
    teams = []
    for team in data.get("teams", []) if isinstance(data, dict) else []:
        try:
            clean = validate_team(team)
        except TeamValidationError as e:
            print(f"[agent-teams] Skipping invalid team in {path}: {e}")
            continue
        if clean["id"] and clean["id"] != DEFAULT_TEAM_ID:
            teams.append(clean)
    return teams


def list_teams(project_path: Path) -> List[Dict[str, Any]]:
    return [{**DEFAULT_TEAM, "builtin": True}] + [{**team, "builtin": False} for team in load_custom_teams(project_path)]


def get_team(project_path: Path, team_id: str) -> Optional[Dict[str, Any]]:
    if not team_id or team_id == DEFAULT_TEAM_ID:
        return DEFAULT_TEAM
    return next((team for team in load_custom_teams(project_path) if team["id"] == team_id), None)


def _write_custom_teams(project_path: Path, teams: List[Dict[str, Any]]) -> None:
    path = _teams_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"teams": teams}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_team(project_path: Path, team: Any) -> Dict[str, Any]:
    """Create (no id) or update a custom team. The default team cannot be changed."""
    clean = validate_team(team)
    if clean["id"] == DEFAULT_TEAM_ID:
        raise TeamValidationError("The default team cannot be modified; duplicate it instead")
    teams = load_custom_teams(project_path)
    if clean["id"]:
        if not any(t["id"] == clean["id"] for t in teams):
            raise TeamValidationError("Team not found")
        teams = [clean if t["id"] == clean["id"] else t for t in teams]
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", clean["name"].lower()).strip("-")[:40] or "team"
        clean["id"] = f"{slug}-{uuid.uuid4().hex[:6]}"
        teams.append(clean)
    _write_custom_teams(project_path, teams)
    return {**clean, "builtin": False}


def delete_team(project_path: Path, team_id: str) -> None:
    teams = load_custom_teams(project_path)
    remaining = [t for t in teams if t["id"] != team_id]
    if len(remaining) == len(teams):
        raise TeamValidationError("Team not found")
    _write_custom_teams(project_path, remaining)
