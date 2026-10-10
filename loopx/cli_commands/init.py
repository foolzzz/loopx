from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def register_init_command(subparsers, add_subcommand_format):
    parser = subparsers.add_parser(
        "init",
        help="Set up providers and agents for first use.",
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use detected defaults without prompting.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing configuration files.",
    )
    parser.add_argument(
        "--runtime-root",
        help="Override the default ~/.loopx state directory.",
    )
    return parser


def handle_init_command(args, _print_payload):
    if getattr(args, "command", None) != "init":
        return None
    return _run_init(
        non_interactive=args.non_interactive,
        force=args.force,
        runtime_root_override=args.runtime_root,
    )


def _run_init(
    *,
    non_interactive: bool = False,
    force: bool = False,
    runtime_root_override: str | None = None,
) -> int:
    from ..paths import default_runtime_root

    runtime_root = Path(runtime_root_override) if runtime_root_override else default_runtime_root()
    providers_path = runtime_root / "providers.yaml"
    agents_dir = runtime_root / "agents"

    print()
    print("  LoopX Init")
    print("  ──────────")
    print()

    # --- Step 1: Detect available CLIs and credentials ---

    has_claude = shutil.which("claude") is not None
    has_codex = shutil.which("codex") is not None
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    print("  [1/3] Provider")
    print(f"    claude CLI: {'found' if has_claude else 'not found'}")
    print(f"    codex CLI:  {'found' if has_codex else 'not found'}")
    if has_anthropic_key:
        print("    ANTHROPIC_API_KEY: set")
    if has_openai_key:
        print("    OPENAI_API_KEY: set")
    print()

    provider = _choose_provider(
        has_claude=has_claude,
        has_codex=has_codex,
        has_anthropic_key=has_anthropic_key,
        has_openai_key=has_openai_key,
        non_interactive=non_interactive,
    )

    if provider is None:
        print("  No provider detected. Install claude CLI (claude.ai) or set ANTHROPIC_API_KEY.")
        return 1

    # --- Step 2: Write providers.yaml ---

    runtime_root.mkdir(parents=True, exist_ok=True)

    if providers_path.exists() and not force:
        if non_interactive:
            print(f"  {providers_path} already exists, skipping (use --force to overwrite)")
        else:
            answer = input(f"  {providers_path} exists. Overwrite? [y/N] ").strip().lower()
            if answer != "y":
                print("  Keeping existing providers.yaml")
            else:
                _write_providers(providers_path, provider)
    else:
        _write_providers(providers_path, provider)

    # --- Step 3: Write agent YAMLs ---

    print()
    print("  [2/3] Agents")

    agents = _default_agents(provider)

    for agent in agents:
        print(f"    {agent['role']:14s} {agent['runtime']} / {agent.get('model', 'default')}")

    if not non_interactive:
        print()
        answer = input("  Customize agent models? [y/N] ").strip().lower()
        if answer == "y":
            agents = _customize_agents(agents)

    agents_dir.mkdir(parents=True, exist_ok=True)

    for agent in agents:
        agent_path = agents_dir / f"{agent['name']}.yaml"
        if agent_path.exists() and not force:
            if non_interactive:
                print(f"    {agent_path.name} exists, skipping")
                continue
            answer = input(f"    {agent_path.name} exists. Overwrite? [y/N] ").strip().lower()
            if answer != "y":
                continue
        _write_agent(agent_path, agent)

    # --- Step 4: Verify ---

    print()
    print("  [3/3] Verify")

    ok = True
    if providers_path.exists():
        print(f"    providers: {providers_path}")
    else:
        print("    providers: MISSING")
        ok = False

    agent_files = sorted(agents_dir.glob("*.yaml")) if agents_dir.exists() else []
    print(f"    agents:    {len(agent_files)} configured")
    if not agent_files:
        ok = False

    print()
    if ok:
        print("  \033[32m✓ Ready\033[0m")
        print()
        print("  Next steps:")
        print("    loopx dispatch serve    # start the scheduler")
        print("    loopx dashboard         # open the web UI")
    else:
        print("  \033[31m✗ Incomplete — check the output above.\033[0m")
    print()

    return 0 if ok else 1


def _choose_provider(
    *,
    has_claude: bool,
    has_codex: bool,
    has_anthropic_key: bool,
    has_openai_key: bool,
    non_interactive: bool,
) -> dict | None:
    options = []

    if has_claude:
        options.append({
            "label": "anthropic (claude CLI login)",
            "name": "anthropic-login",
            "kind": "anthropic",
            "auth_type": "oauth_cli",
            "runtime": "claude-code",
        })
    if has_anthropic_key:
        options.append({
            "label": "anthropic (API key)",
            "name": "anthropic-key",
            "kind": "anthropic",
            "auth_type": "api_key",
            "auth_env": "ANTHROPIC_API_KEY",
            "runtime": "claude-code",
        })
    if has_openai_key:
        options.append({
            "label": "openai (API key)",
            "name": "openai-key",
            "kind": "openai",
            "auth_type": "api_key",
            "auth_env": "OPENAI_API_KEY",
            "runtime": "codex-cli",
        })

    if not options:
        return None

    if non_interactive or len(options) == 1:
        choice = options[0]
        print(f"    Using: {choice['label']}")
        return choice

    print("    Available providers:")
    for i, opt in enumerate(options, 1):
        print(f"      {i}. {opt['label']}")

    while True:
        answer = input(f"    Choose [1-{len(options)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            choice = options[int(answer) - 1]
            print(f"    Using: {choice['label']}")
            return choice
        print(f"    Enter a number 1-{len(options)}")


def _write_providers(path: Path, provider: dict) -> None:
    auth_line = f"type: {provider['auth_type']}"
    if provider.get("auth_env"):
        auth_line += f", env: {provider['auth_env']}"

    content = f"""providers:
  {provider['name']}:
    kind: {provider['kind']}
    auth: {{{auth_line}}}
"""
    path.write_text(content)
    print(f"    Wrote {path}")


def _default_agents(provider: dict) -> list[dict]:
    runtime = provider["runtime"]
    provider_name = provider["name"]

    if runtime == "claude-code":
        return [
            {
                "name": "orch",
                "role": "orchestrator",
                "runtime": "claude-code",
                "provider": provider_name,
                "model": "claude-opus-4-6",
            },
            {
                "name": "dev",
                "role": "developer",
                "runtime": "claude-code",
                "provider": provider_name,
                "model": "claude-sonnet-5",
                "permission_mode": "acceptEdits",
                "max_concurrency": 2,
            },
            {
                "name": "acc",
                "role": "acceptor",
                "runtime": "claude-code",
                "provider": provider_name,
                "model": "claude-sonnet-5",
            },
        ]
    else:
        return [
            {
                "name": "orch",
                "role": "orchestrator",
                "runtime": "codex-cli",
                "provider": provider_name,
            },
            {
                "name": "dev",
                "role": "developer",
                "runtime": "codex-cli",
                "provider": provider_name,
                "max_concurrency": 2,
            },
            {
                "name": "acc",
                "role": "acceptor",
                "runtime": "codex-cli",
                "provider": provider_name,
            },
        ]


def _customize_agents(agents: list[dict]) -> list[dict]:
    for agent in agents:
        current = agent.get("model", "default")
        answer = input(f"    {agent['role']} model [{current}]: ").strip()
        if answer:
            agent["model"] = answer
    return agents


def _write_agent(path: Path, agent: dict) -> None:
    lines = [
        f"role: {agent['role']}",
        f"runtime: {agent['runtime']}",
        f"provider: {agent['provider']}",
    ]
    if agent.get("model"):
        lines.append(f"model: {agent['model']}")
    if agent.get("permission_mode"):
        lines.append(f"permission_mode: {agent['permission_mode']}")
    if agent.get("max_concurrency") and agent["max_concurrency"] > 1:
        lines.append(f"max_concurrency: {agent['max_concurrency']}")

    path.write_text("\n".join(lines) + "\n")
    print(f"    Wrote {path.name}")
