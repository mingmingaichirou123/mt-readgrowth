#!/usr/bin/env python3
"""Verify, package, or install the standalone MT-readgrowth Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True


SKILL_NAME = "mt-readgrowth"
LICENSE_MARKER = "MT-READGROWTH NON-COMMERCIAL LICENSE"
MINIMUM_PYTHON = (3, 10)
RUNTIME_TOP_LEVEL = {"SKILL.md", "LICENSE", "agents", "assets", "references", "scripts"}
IGNORED_PARTS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORED_NAMES = {".DS_Store"}
FORBIDDEN_NAMES = {".weread-api-key", ".env"}


class DistributionError(RuntimeError):
    """Raised when a Skill distribution is unsafe or malformed."""


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def require_supported_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise DistributionError(f"Python {required}+ is required")


def runtime_files(source: Path) -> list[Path]:
    source = source.resolve()
    files: list[Path] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if not relative.parts or relative.parts[0] not in RUNTIME_TOP_LEVEL:
            continue
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.name in IGNORED_NAMES or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise DistributionError(f"distribution must not contain symlinks: {relative.as_posix()}")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(source).as_posix())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(source: Path, files: Iterable[Path] | None = None) -> dict[str, str]:
    source = source.resolve()
    selected = list(files) if files is not None else runtime_files(source)
    return {path.relative_to(source).as_posix(): sha256(path) for path in selected}


def frontmatter_value(text: str, key: str) -> str | None:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        return None
    value = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", match.group(1))
    return value.group(1).strip().strip("\"'") if value else None


def validate_source(source: Path | None = None) -> dict[str, Any]:
    require_supported_python()
    source = (source or source_root()).resolve()
    skill_file = source / "SKILL.md"
    license_file = source / "LICENSE"
    if not skill_file.is_file():
        raise DistributionError(f"SKILL.md is missing: {skill_file}")
    if not license_file.is_file():
        raise DistributionError(f"standalone LICENSE is missing: {license_file}")

    skill_text = skill_file.read_text(encoding="utf-8")
    if frontmatter_value(skill_text, "name") != SKILL_NAME:
        raise DistributionError(f"SKILL.md name must be {SKILL_NAME}")
    if not frontmatter_value(skill_text, "description"):
        raise DistributionError("SKILL.md description is required")

    license_text = license_file.read_text(encoding="utf-8")
    if LICENSE_MARKER not in license_text:
        raise DistributionError(f"standalone LICENSE must contain {LICENSE_MARKER}")
    if not re.search(r"(?m)^Copyright \(c\) \d{4}(?:-\d{4})? \S.*$", license_text):
        raise DistributionError("standalone LICENSE must preserve a named project copyright holder")

    repository_license = source.parent / "LICENSE"
    if (source.parent / "README.md").is_file() and repository_license.is_file():
        if repository_license.read_bytes() != license_file.read_bytes():
            raise DistributionError("repository and standalone LICENSE files differ")

    files = runtime_files(source)
    relative_names = [path.relative_to(source).as_posix() for path in files]
    for path, relative in zip(files, relative_names):
        if path.name in FORBIDDEN_NAMES or relative.startswith("reading-workspace/"):
            raise DistributionError(f"private runtime data must not be distributed: {relative}")
        if path.suffix.lower() in {".epub", ".txt"}:
            raise DistributionError(f"source books must not be distributed: {relative}")
        if path.suffix.lower() in {".md", ".py", ".yaml", ".yml", ".json", ".txt"}:
            text = path.read_text(encoding="utf-8")
            if re.search(r"(?i)[A-Z]:\\Users\\[^\\\s]+", text):
                raise DistributionError(f"author-machine absolute path found: {relative}")

    required = {
        "SKILL.md",
        "LICENSE",
        "agents/openai.yaml",
        "assets/project-shell/AGENTS.md",
        "scripts/project_shell.py",
    }
    missing = sorted(required.difference(relative_names))
    if missing:
        raise DistributionError(f"required distribution files are missing: {', '.join(missing)}")

    return {
        "result": "valid",
        "skill": SKILL_NAME,
        "source": str(source),
        "file_count": len(files),
        "python_minimum": ".".join(str(part) for part in MINIMUM_PYTHON),
        "manifest_sha256": hashlib.sha256(
            json.dumps(file_manifest(source, files), sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def resolve_target(
    host: str,
    scope: str,
    *,
    target: Path | None = None,
    project_root: Path | None = None,
    home: Path | None = None,
) -> Path:
    if target is not None:
        return target.expanduser().resolve()

    project = (project_root or Path.cwd()).resolve()
    user_home = (home or Path.home()).expanduser().resolve()
    if host == "codex":
        return (user_home / ".agents" / "skills") if scope == "user" else (project / ".agents" / "skills")
    if host == "claude":
        return (user_home / ".claude" / "skills") if scope == "user" else (project / ".claude" / "skills")
    if host == "codebuddy":
        if scope != "project":
            raise DistributionError("CodeBuddy user Skills should be imported in its settings; use --scope project or package")
        return project / ".codebuddy" / "skills"
    if host == "generic":
        raise DistributionError("generic installation requires --target <host-skills-directory>")
    raise DistributionError(f"unsupported host: {host}")


def install_skill(
    target_root: Path,
    *,
    source: Path | None = None,
) -> dict[str, Any]:
    source = (source or source_root()).resolve()
    validation = validate_source(source)
    files = runtime_files(source)
    target_root = target_root.expanduser().resolve()
    destination = target_root / SKILL_NAME
    target_root.mkdir(parents=True, exist_ok=True)

    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            try:
                if file_manifest(destination) == file_manifest(source, files):
                    return {
                        "result": "already-installed",
                        "host_skills_directory": str(target_root),
                        "skill_directory": str(destination),
                        "validation": validation,
                    }
            except (DistributionError, OSError):
                pass
        raise DistributionError(
            f"destination already exists and differs: {destination}. "
            "Remove or rename it manually after reviewing its contents, then retry."
        )

    staging = target_root / f".{SKILL_NAME}.install-{uuid.uuid4().hex}"
    try:
        for path in files:
            relative = path.relative_to(source)
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        "result": "installed",
        "host_skills_directory": str(target_root),
        "skill_directory": str(destination),
        "installed_file_count": len(files),
        "workspace_created": False,
        "credential_created": False,
        "validation": validation,
    }


def package_skill(output: Path, *, source: Path | None = None) -> dict[str, Any]:
    source = (source or source_root()).resolve()
    validation = validate_source(source)
    files = runtime_files(source)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o644 & 0xFFFF) << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "result": "packaged",
        "archive": str(output),
        "archive_sha256": sha256(output),
        "packaged_file_count": len(files),
        "archive_root": "SKILL.md",
        "validation": validation,
    }


def default_package_path() -> Path:
    return source_root().parent / "dist" / f"{SKILL_NAME}.zip"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("verify", help="validate the standalone Skill source and privacy boundary")

    install_parser = subparsers.add_parser("install", help="copy the Skill into a supported host directory")
    install_parser.add_argument("--host", choices=["codex", "claude", "codebuddy", "generic"], required=True)
    install_parser.add_argument("--scope", choices=["user", "project"], default="user")
    install_parser.add_argument("--target", type=Path, help="custom parent directory that contains installed Skills")
    install_parser.add_argument("--project-root", type=Path, default=Path.cwd())

    package_parser = subparsers.add_parser("package", help="create a portable ZIP with SKILL.md at its root")
    package_parser.add_argument("--output", type=Path, default=default_package_path())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = validate_source()
        elif args.command == "install":
            target = resolve_target(
                args.host,
                args.scope,
                target=args.target,
                project_root=args.project_root,
            )
            result = install_skill(target)
            result.update({"host": args.host, "scope": args.scope})
        else:
            result = package_skill(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DistributionError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
