#!/usr/bin/env python3
"""Mitosis Orchestrator setup script for OpenAI Codex CLI (>= 0.134).

Installs Mitosis agent roles, rules, profiles, and the MCP server into $CODEX_HOME (default ~/.codex):
1. Backs up $CODEX_HOME/config.toml to $CODEX_HOME/config.toml.bak (a timestamped
   .bak-YYYYmmdd-HHMMSS once that exists, so the first backup is never overwritten)
   and a replaced $CODEX_HOME/AGENTS.md the same way
2. Copies rules/AGENTS.md to $CODEX_HOME/AGENTS.md
3. Installs codex-agents/*.toml role files into $CODEX_HOME/agents/
4. Installs codex-config/*.config.toml profile files into $CODEX_HOME/
   (Codex >= 0.134 reads `--profile <name>` from $CODEX_HOME/<name>.config.toml;
   the legacy [profiles.<name>] table in config.toml is no longer honoured)
5. Non-destructively configures [mcp_servers.mitosis] in $CODEX_HOME/config.toml
   using this interpreter and the absolute path of scripts/mitosis-mcp-server.py
6. Validates TOML integrity and verifies all mounted files exist
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


DEFAULT_CODEX_HOME = Path.home() / ".codex"
REPO_ROOT = Path(__file__).resolve().parent.parent


def plugin_version(repo_root: Path) -> str:
    try:
        with open(repo_root / "plugin.json", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def _backup_path(preferred: Path) -> Path:
    """The preferred .bak name, or a timestamped sibling once that exists."""
    if not preferred.exists():
        return preferred
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return preferred.with_name(f"{preferred.name}-{stamp}")


def _toml_string(value: str) -> str:
    """Render a TOML basic string, escaping backslashes and quotes (Windows paths)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_or_append_mcp_server(content: str, script_path: str, interpreter: str = "python3") -> str:
    """Updates [mcp_servers.mitosis] to use the absolute script path, or appends it."""
    mcp_section = (
        "[mcp_servers.mitosis]\n"
        f"command = {_toml_string(interpreter)}\n"
        f"args = [{_toml_string(script_path)}]\n"
    )
    pattern = r'\[mcp_servers\.mitosis\][\s\S]*?(?=\n\[|\Z)'
    if re.search(r'\[mcp_servers\.mitosis\]', content):
        content = re.sub(pattern, lambda _: mcp_section.rstrip(), content, count=1)
    else:
        content = content.rstrip() + "\n\n" + mcp_section
    return content


def setup_codex(
    codex_home: Path | None = None,
    repo_root: Path | None = None,
    mcp_script: Path | None = None,
    interpreter: str | None = None,
) -> list[Path]:
    if repo_root is None:
        repo_root = REPO_ROOT

    if codex_home is None:
        env_home = os.environ.get("CODEX_HOME")
        codex_home = Path(env_home).expanduser().resolve() if env_home else DEFAULT_CODEX_HOME

    if mcp_script is None:
        mcp_script = repo_root / "scripts" / "mitosis-mcp-server.py"
    mcp_script = Path(mcp_script).expanduser().resolve()
    if not mcp_script.exists():
        raise FileNotFoundError(f"MCP server script not found: {mcp_script}")

    if interpreter is None:
        interpreter = sys.executable or "python3"

    codex_home.mkdir(parents=True, exist_ok=True)
    agents_dir = codex_home / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    installed_files: list[Path] = []

    # 1. Back up config.toml
    config_file = codex_home / "config.toml"
    backup_file = _backup_path(codex_home / "config.toml.bak")
    if config_file.exists():
        shutil.copy2(config_file, backup_file)
        print(f"[OK] Backed up {config_file} -> {backup_file}")
        installed_files.append(backup_file)

    # 2. Copy rules/AGENTS.md
    source_agents_md = repo_root / "rules" / "AGENTS.md"
    target_agents_md = codex_home / "AGENTS.md"
    if not source_agents_md.exists():
        raise FileNotFoundError(f"Source rules/AGENTS.md not found at {source_agents_md}")
    if target_agents_md.exists() and target_agents_md.read_bytes() != source_agents_md.read_bytes():
        agents_backup = _backup_path(codex_home / "AGENTS.md.bak")
        shutil.copy2(target_agents_md, agents_backup)
        print(f"[OK] Backed up {target_agents_md} -> {agents_backup}")
        installed_files.append(agents_backup)
    shutil.copy2(source_agents_md, target_agents_md)
    print(f"[OK] Copied {source_agents_md} -> {target_agents_md}")
    installed_files.append(target_agents_md)

    # 3. Install codex-agents/*.toml role files
    source_agents_dir = repo_root / "codex-agents"
    agent_tomls = sorted(source_agents_dir.glob("*.toml"))
    if not agent_tomls:
        raise FileNotFoundError(f"No agent TOML files found in {source_agents_dir}")
    for toml_path in agent_tomls:
        with open(toml_path, "rb") as f:
            role = tomllib.load(f)
        if role.get("name") != toml_path.stem:
            raise ValueError(f"{toml_path.name}: 'name' must equal the file stem ('{toml_path.stem}')")
        dest = agents_dir / toml_path.name
        shutil.copy2(toml_path, dest)
        print(f"[OK] Installed agent role {toml_path.name} -> {dest}")
        installed_files.append(dest)

    # 4. Install profile files ($CODEX_HOME/<name>.config.toml)
    source_profiles_dir = repo_root / "codex-config"
    profile_files = sorted(source_profiles_dir.glob("*.config.toml"))
    if not profile_files:
        raise FileNotFoundError(f"No *.config.toml profile files found in {source_profiles_dir}")
    for profile_path in profile_files:
        with open(profile_path, "rb") as f:
            tomllib.load(f)
        dest = codex_home / profile_path.name
        shutil.copy2(profile_path, dest)
        print(f"[OK] Installed profile {profile_path.name} -> {dest}")
        installed_files.append(dest)

    # 5. Configure [mcp_servers.mitosis] in config.toml
    existing_content = ""
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            existing_content = f.read()
    try:
        tomllib.loads(existing_content) if existing_content.strip() else {}
    except Exception as e:
        raise ValueError(f"Existing {config_file} contains invalid TOML: {e}") from e

    updated_content = update_or_append_mcp_server(existing_content, str(mcp_script), interpreter)
    tomllib.loads(updated_content)
    tmp_config = config_file.with_name(config_file.name + ".mitosis-tmp")
    with open(tmp_config, "w", encoding="utf-8") as f:
        f.write(updated_content)
    os.replace(tmp_config, config_file)
    print(f"[OK] Configured [mcp_servers.mitosis] in {config_file}")
    installed_files.append(config_file)

    # 6. Final verification
    expected_files = [target_agents_md, config_file]
    expected_files += [agents_dir / p.name for p in agent_tomls]
    expected_files += [codex_home / p.name for p in profile_files]
    for ef in expected_files:
        if not ef.exists():
            raise RuntimeError(f"Verification failed: mounted file missing: {ef}")
        if ef.suffix == ".toml":
            with open(ef, "rb") as f:
                tomllib.load(f)

    print("\n--- Auto-Mount Verification Complete ---")
    print(f"Mounted Codex Home: {codex_home}")
    for ef in expected_files:
        print(f"  - Verified: {ef.name} ({ef.stat().st_size} bytes)")
    print("\nUsage: codex exec --profile mitosis-scout \"<lookup>\"  |  codex exec --profile mitosis-worker \"<task>\"")

    return installed_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-mount Mitosis Orchestrator into $CODEX_HOME (default ~/.codex)")
    parser.add_argument("--codex-home", type=Path, default=None, help="Custom Codex home directory (default: ~/.codex or CODEX_HOME)")
    parser.add_argument("--mcp-script", type=Path, default=None, help="Custom path to mitosis-mcp-server.py (default: this repo's copy)")
    parser.add_argument("--interpreter", type=str, default=None, help="Python interpreter for the MCP server (default: the one running this script)")
    args = parser.parse_args()

    try:
        setup_codex(codex_home=args.codex_home, mcp_script=args.mcp_script, interpreter=args.interpreter)
        print(f"\nMitosis Orchestrator v{plugin_version(REPO_ROOT)} successfully mounted into Codex CLI!")
        return 0
    except Exception as exc:
        print(f"ERROR: Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
