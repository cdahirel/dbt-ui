"""Project-level configuration utilities.

Reads .dbt-ui-config.yml from the dbt project root, if present.

Supported keys:
  dbt_executable:  path to a custom dbt launcher script (overrides venv/global dbt)
                   e.g. "./run_dbt.py" or "python ./launch_dbt.py"
  test_command:    shell command used by the "Run Tests" button
                   e.g. "pytest tests/ -v" (default)
"""
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


def read_project_config(project_path: Path) -> Dict[str, Any]:
    """Return the parsed contents of .dbt-ui-config.yml, or {} if absent/unreadable."""
    config_file = project_path / ".dbt-ui-config.yml"
    if not config_file.exists():
        return {}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[config_utils] Failed to read .dbt-ui-config.yml: {e}")
        return {}


def get_custom_dbt_executable(project_path: Path) -> Optional[str]:
    """Return the dbt_executable override from .dbt-ui-config.yml, or None."""
    return read_project_config(project_path).get("dbt_executable")


def get_test_command(project_path: Path) -> str:
    """Return the test_command from .dbt-ui-config.yml, defaulting to 'pytest tests/ -v'."""
    return read_project_config(project_path).get("test_command", "pytest tests/ -v")
