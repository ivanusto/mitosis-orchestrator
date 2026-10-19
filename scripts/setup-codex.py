#!/usr/bin/env python3
"""Mitosis Orchestrator setup script for OpenAI Codex CLI.

Installs Mitosis agent roles, rules, and configuration profiles into ~/.codex:
1. Backs up ~/.codex/config.toml to ~/.codex/config.toml.bak
2. Copies rules/AGENTS.md to ~/.codex/AGENTS.md
3. Installs codex-agents/*.toml into ~/.codex/agents/
4. Non-destructively configures [profiles.mitosis-scout], [profiles.mitosis-worker],
   and [mcp_servers.mitosis] (with absolute path to mitosis-mcp-server.py) in ~/.codex/config.toml
5. Validates TOML integrity and verifies all mounted files exist
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_MCP_SERVER_SCRIPT = (
    Path.home()
    / ".gemini"
    / "config"
    / "plugins"
    / "mitosis-orchestrator"
    / "scripts"
    / "mitosis-mcp-server.py"
)


def update_or_append_mcp_server(content: str, script_path: str) -> str:
    """Updates [mcp_servers.mitosis] to use the absolute script path, or appends it."""
    mcp_section = f"""[mcp_servers.mitosis]
command = "python"
args = ['{script_path}']
"""
    # Pattern matching existing [mcp_servers.mitosis] section until next section or EOF
    pattern = r'\[mcp_servers\.mitosis\][\s\S]*?(?=\n\[|\Z)'
    if re.search(r'\[mcp_servers\.mitosis\]', content):
        # Use lambda to prevent re.sub from interpreting backslashes in replacement string
        content = re.sub(pattern, lambda _: mcp_section.rstrip(), content, count=1)
    else:
        content = content.rstrip() + "\n\n" + mcp_section
    return content


def setup_codex(
    codex_home: Path | None = None,
    repo_root: Path | None = None,
    mcp_script: Path | None = None,
) -> list[Path]:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    if codex_home is None:
        env_home = os.environ.get("CODEX_HOME")
        codex_home = Path(env_home).resolve() if env_home else DEFAULT_CODEX_HOME

    if mcp_script is None:
        mcp_script = DEFAULT_MCP_SERVER_SCRIPT
        if not mcp_script.exists():
            local_candidate = repo_root / "scripts" / "mitosis-mcp-server.py"
            if local_candidate.exists():
                mcp_script = local_candidate

    codex_home.mkdir(parents=True, exist_ok=True)
    agents_dir = codex_home / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    installed_files: list[Path] = []

    # 1. Back up ~/.codex/config.toml
    config_file = codex_home / "config.toml"
    backup_file = codex_home / "config.toml.bak"
    if config_file.exists():
        shutil.copy2(config_file, backup_file)
        print(f"[OK] Backed up {config_file} -> {backup_file}")
        installed_files.append(backup_file)

    # 2. Copy rules/AGENTS.md to ~/.codex/AGENTS.md
    source_agents_md = repo_root / "rules" / "AGENTS.md"
    target_agents_md = codex_home / "AGENTS.md"
    if not source_agents_md.exists():
        raise FileNotFoundError(f"Source rules/AGENTS.md not found at {source_agents_md}")
    shutil.copy2(source_agents_md, target_agents_md)
    print(f"[OK] Copied {source_agents_md} -> {target_agents_md}")
    installed_files.append(target_agents_md)

    # 3. Install codex-agents/*.toml to ~/.codex/agents/
    source_agents_dir = repo_root / "codex-agents"
    agent_tomls = sorted(source_agents_dir.glob("*.toml"))
    if not agent_tomls:
        raise FileNotFoundError(f"No agent TOML files found in {source_agents_dir}")

    for toml_path in agent_tomls:
        with open(toml_path, "r", encoding="utf-8") as f:
            tomllib.loads(f.read())
        dest = agents_dir / toml_path.name
        shutil.copy2(toml_path, dest)
        print(f"[OK] Installed agent role {toml_path.name} -> {dest}")
        installed_files.append(dest)

    # 4. Non-destructively configure profiles & MCP server in ~/.codex/config.toml
    existing_content = ""
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            existing_content = f.read()

    try:
        parsed_config = tomllib.loads(existing_content) if existing_content.strip() else {}
    except Exception as e:
        raise ValueError(f"Existing {config_file} contains invalid TOML: {e}") from e

    profiles = parsed_config.get("profiles", {})

    missing_profile_sections: list[str] = []
    if "mitosis-scout" not in profiles:
        missing_profile_sections.append(
            '[profiles.mitosis-scout]\nmodel = "gpt-5.6-luna"\nmodel_reasoning_effort = "low"\nsandbox_mode = "read-only"\n'
        )
    if "mitosis-worker" not in profiles:
        missing_profile_sections.append(
            '[profiles.mitosis-worker]\nmodel = "codex-mini-latest"\nmodel_reasoning_effort = "medium"\nsandbox_mode = "workspace-write"\n'
        )

    updated_content = existing_content
    if missing_profile_sections:
        append_text = "\n" + "\n".join(missing_profile_sections)
        updated_content = updated_content.rstrip() + "\n" + append_text

    # Ensure [mcp_servers.mitosis] has the absolute path
    updated_content = update_or_append_mcp_server(updated_content, str(mcp_script))

    # Validate before writing
    tomllib.loads(updated_content)
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(updated_content)
    print(f"[OK] Updated Mitosis profiles and MCP server in {config_file}")
    installed_files.append(config_file)

    # 5. Final Verification of all mounted files
    expected_files = [
        target_agents_md,
        agents_dir / "explorer.toml",
        agents_dir / "flash-lite-scout.toml",
        agents_dir / "worker.toml",
        agents_dir / "flash-worker.toml",
        config_file,
    ]

    for ef in expected_files:
        if not ef.exists():
            raise RuntimeError(f"Verification failed: mounted file missing: {ef}")
        if ef.suffix == ".toml":
            with open(ef, "r", encoding="utf-8") as f:
                tomllib.loads(f.read())

    print("\n--- Auto-Mount Verification Complete ---")
    print(f"Mounted Codex Home: {codex_home}")
    for ef in expected_files:
        print(f"  - Verified: {ef.name} ({ef.stat().st_size} bytes)")

    return installed_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-mount Mitosis Orchestrator into ~/.codex")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        help="Custom Codex home directory (default: ~/.codex or CODEX_HOME)",
    )
    parser.add_argument(
        "--mcp-script",
        type=Path,
        default=None,
        help="Custom path to mitosis-mcp-server.py",
    )
    args = parser.parse_args()

    try:
        setup_codex(codex_home=args.codex_home, mcp_script=args.mcp_script)
        print("\nMitosis Orchestrator v1.2.0 successfully mounted into Codex CLI!")
        return 0
    except Exception as exc:
        print(f"ERROR: Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
