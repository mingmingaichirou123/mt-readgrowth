#!/usr/bin/env python3
"""Initialize or audit a project shell bound to an installed MT-readgrowth Skill."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from daily_reading import (
    DailyFlowFailure,
    WORKSPACE_CONFIG,
    WORKSPACE_DIRECTORIES,
    initialize_workspace,
)


WORKSPACE_NAME = "reading-workspace"
AGENT_GUIDE_NAME = "AGENTS.md"
MANAGED_START = "<!-- mt-readgrowth-project-binding:v1:start -->"
MANAGED_END = "<!-- mt-readgrowth-project-binding:v1:end -->"


class ProjectShellFailure(RuntimeError):
    """Raised when a project shell cannot be created or verified safely."""


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def agent_guide_template() -> Path:
    return skill_root() / "assets" / "project-shell" / AGENT_GUIDE_NAME


def managed_agent_guide_block() -> str:
    template = agent_guide_template()
    if not template.is_file():
        raise ProjectShellFailure(f"project-shell Agent guide template is missing: {template}")
    text = template.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
    if text.count(MANAGED_START) != 1 or text.count(MANAGED_END) != 1:
        raise ProjectShellFailure("project-shell Agent guide template has invalid managed markers")
    if not text.startswith(MANAGED_START) or not text.endswith(MANAGED_END):
        raise ProjectShellFailure("project-shell Agent guide template must contain only the managed binding block")
    return text


def _managed_block(text: str) -> str | None:
    normalized = text.replace("\r\n", "\n")
    if normalized.count(MANAGED_START) != 1 or normalized.count(MANAGED_END) != 1:
        return None
    start = normalized.index(MANAGED_START)
    end_start = normalized.find(MANAGED_END, start + len(MANAGED_START))
    if end_start < 0:
        return None
    end = end_start + len(MANAGED_END)
    return normalized[start:end]


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _resolve_project_paths(project_root: Path) -> tuple[Path, Path, Path]:
    project = project_root.expanduser().resolve()
    installed_skill = skill_root().resolve()
    if (
        project == installed_skill
        or installed_skill in project.parents
        or project in installed_skill.parents
    ):
        raise ProjectShellFailure(
            "project shell and installed Skill must be separate; neither path may contain the other"
        )
    if project.exists() and not project.is_dir():
        raise ProjectShellFailure(f"project root is not a directory: {project}")

    guide = project / AGENT_GUIDE_NAME
    workspace = project / WORKSPACE_NAME
    if guide.exists() or guide.is_symlink():
        if guide.is_symlink() or not guide.is_file() or guide.resolve() != guide:
            raise ProjectShellFailure(f"project Agent guide must be a regular in-project file: {guide}")
    if workspace.exists() or workspace.is_symlink():
        if workspace.is_symlink() or not workspace.is_dir() or workspace.resolve() != workspace:
            raise ProjectShellFailure(f"reading-workspace must be a regular in-project directory: {workspace}")
    return project, guide, workspace


def _validate_existing_guide(guide: Path, expected_block: str) -> bool:
    if not guide.exists():
        return False
    current = guide.read_text(encoding="utf-8")
    current_block = _managed_block(current)
    if current_block is None:
        raise ProjectShellFailure(
            f"{AGENT_GUIDE_NAME} already exists without the MT-readgrowth managed binding; "
            "review and merge it manually instead of overwriting"
        )
    if current_block != expected_block:
        raise ProjectShellFailure(
            f"the MT-readgrowth managed binding in {AGENT_GUIDE_NAME} was modified; "
            "review it manually before retrying"
        )
    return True


def initialize_project_shell(project_root: Path) -> dict[str, Any]:
    project, guide, workspace = _resolve_project_paths(project_root)
    expected_block = managed_agent_guide_block()
    guide_exists = _validate_existing_guide(guide, expected_block)

    project.mkdir(parents=True, exist_ok=True)
    initialization = initialize_workspace(workspace)
    if not guide_exists:
        _atomic_text(guide, expected_block + "\n")

    changed = bool(not guide_exists or initialization["result"] != "unchanged")
    return {
        "result": "initialized" if changed else "unchanged",
        "project_root": str(project),
        "agent_guide": str(guide),
        "agent_guide_created": not guide_exists,
        "workspace": str(workspace),
        "workspace_initialization": initialization["result"],
        "skill_root": str(skill_root().resolve()),
        "reading_data_created": False,
        "credential_created": False,
        "open_this_directory": str(project),
        "next_action": "open the project root in the Agent; do not open reading-workspace as the project root",
    }


def audit_project_shell(project_root: Path) -> dict[str, Any]:
    project, guide, workspace = _resolve_project_paths(project_root)
    if not project.is_dir():
        raise ProjectShellFailure(f"project root is missing: {project}")
    if not guide.is_file():
        raise ProjectShellFailure(f"project Agent guide is missing: {guide}")
    _validate_existing_guide(guide, managed_agent_guide_block())

    config = workspace / "workspace.yaml"
    if not config.is_file() or config.is_symlink() or config.resolve().parent != workspace:
        raise ProjectShellFailure(f"workspace configuration is missing or unsafe: {config}")
    current_config = config.read_text(encoding="utf-8").replace("\r\n", "\n")
    if current_config.rstrip("\n") + "\n" != WORKSPACE_CONFIG:
        raise ProjectShellFailure("workspace.yaml conflicts with the MT-readgrowth workspace contract")

    missing_directories = []
    unsafe_directories = []
    for relative in WORKSPACE_DIRECTORIES:
        directory = workspace / relative
        if not directory.is_dir():
            missing_directories.append(relative)
        elif directory.is_symlink() or workspace not in directory.resolve().parents:
            unsafe_directories.append(relative)
    if missing_directories:
        raise ProjectShellFailure(
            "project workspace is missing required directories: " + ", ".join(missing_directories)
        )
    if unsafe_directories:
        raise ProjectShellFailure(
            "project workspace contains linked or escaping directories: " + ", ".join(unsafe_directories)
        )

    return {
        "result": "valid",
        "project_root": str(project),
        "agent_guide": str(guide),
        "workspace": str(workspace),
        "skill_root": str(skill_root().resolve()),
        "binding": "user-level-skill-plus-project-agent-guide",
        "workspace_data_enumerated": False,
        "personal_content_read": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "audit"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = (
            initialize_project_shell(args.project_root)
            if args.command == "init"
            else audit_project_shell(args.project_root)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DailyFlowFailure, ProjectShellFailure, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
