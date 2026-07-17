from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "baldr-router"


def xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".config"


def app_config_dir() -> Path:
    return xdg_config_home() / APP_NAME


def config_path() -> Path:
    return app_config_dir() / "config.toml"


def secrets_path() -> Path:
    return app_config_dir() / "secrets.toml"


@dataclass
class RouterConfig:
    default_provider: str = "codex"
    default_workflow: str = "architect-implement-review"


@dataclass
class CodexConfig:
    # Provider-level fallbacks. A role/execution profile may override all of
    # these without coupling Baldr to any concrete model name.
    model: str = ""
    reasoning_effort: str = ""
    approval_policy: str = "never"
    sandbox: str = "workspace-write"
    timeout_seconds: int = 1800
    skip_git_repo_check: bool = False
    runner: str = "exec-json"  # exec-json | app-server | sdk
    session_scope: str = "workspace"  # workspace | workflow | task | global


@dataclass
class KiroCliConfig:
    enabled: bool = False
    command: str = "kiro-cli"
    default_agent: str = "baldr-worker"
    default_effort: str = "high"
    timeout_seconds: int = 1800
    require_api_key: bool = True
    api_key_env: str = "KIRO_API_KEY"


@dataclass
class Context7Config:
    enabled: bool = False
    # off: disabled, codex-mcp: only let Codex use Context7 MCP,
    # router-cache: router prefetches/caches docs and injects them into prompts,
    # hybrid: both router-cache and Codex MCP.
    mode: str = "hybrid"  # off | codex-mcp | router-cache | hybrid
    api_key_source: str = "env:CONTEXT7_API_KEY"  # env:... | local-file
    install_codex_mcp: bool = False
    cache_ttl_hours: int = 48
    inject_docs: bool = True
    max_libraries: int = 3
    max_chars: int = 9000
    fast: bool = True


@dataclass
class WorkspaceConfig:
    trusted_roots: list[str] = field(default_factory=list)
    trusted_non_git_roots: list[str] = field(default_factory=list)
    require_git_repository: bool = True
    allow_home_root: bool = False
    allow_runtime_roots: bool = True
    deny_sensitive_paths: bool = True
    # The default follows the permission-gated direct model used by coding
    # agents: architecture is read-only, then an explicit workflow
    # authorization unlocks writes in the selected workspace. ``auto`` and
    # ``worktree`` remain available for legacy isolated runs.
    write_isolation: str = "in-place"
    publish_worktree_changes: bool = False
    cleanup_successful_worktrees: bool = True
    retain_failed_worktrees: bool = True
    # Direct authorized work preserves the current Git state instead of
    # requiring stash/commit. Same-path safety remains the provider/workflow
    # responsibility, as in the native Codex/Kiro workspace model.
    dirty_workspace_policy: str = "in-place"

    # Durable shadow workspace policy. Manifests and content-addressed blobs are
    # the portable source of truth; the private Git repository is auxiliary.
    shadow_max_files: int = 100000
    shadow_max_total_bytes: int = 5 * 1024 * 1024 * 1024
    shadow_max_single_file_bytes: int = 512 * 1024 * 1024
    shadow_max_depth: int = 64
    shadow_max_symlinks: int = 10000
    shadow_exclude_generated: bool = True
    shadow_generated_directories: list[str] = field(
        default_factory=lambda: [
            "node_modules",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".gradle",
            "target",
            "dist",
            "build",
        ]
    )
    shadow_secret_patterns: list[str] = field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "*.p12",
            "*.pfx",
            "credentials.json",
            "secrets.toml",
        ]
    )
    shadow_exclude_patterns: list[str] = field(default_factory=list)
    shadow_include_patterns: list[str] = field(default_factory=list)
    cleanup_successful_shadow_workspaces: bool = True
    retain_failed_shadow_workspaces: bool = True
    shadow_success_retention_hours: int = 0
    shadow_failed_retention_days: int = 30
    shadow_conflict_retention_days: int = 90


@dataclass
class TelemetryConfig:
    enabled: bool = True
    keep_raw_events: bool = False
    max_events_returned: int = 60


@dataclass
class ProbeConfig:
    enabled: bool = True
    cache_ttl_minutes: int = 30
    max_files: int = 20000
    max_manifest_bytes: int = 1048576
    max_dependency_names: int = 200
    scan_max_depth: int = 8


@dataclass
class VerificationConfig:
    enabled: bool = True
    run_on_setup: bool = True
    run_on_client_doctor: bool = True
    setup_mode: str = "quick"  # quick | full
    evidence_retention_days: int = 30
    default_repeat: int = 1
    required_consecutive_passes: int = 3
    include_provider_smoke: bool = False
    timeout_seconds: int = 90


@dataclass
class QualificationConfig:
    enabled: bool = True
    required_consecutive_passes: int = 3
    require_client_receipt: bool = True
    require_managed_runtime_receipt: bool = True
    require_real_canaries_for_release: bool = False
    required_repositories: int = 2
    required_tasks_per_repository: int = 5
    retention_days: int = 90


@dataclass
class ArtifactPrivacyConfig:
    # Private artifacts are required for durable recovery, but are kept out of
    # SQLite inline columns by default and written as chmod-0600 content files.
    private_artifacts_external: bool = True
    raw_artifact_retention_days: int = 30
    include_private_artifacts_in_evidence: bool = False


@dataclass
class SafetyConfig:
    max_depth: int = 1
    max_rounds: int = 2
    prevent_router_reentry: bool = True
    prevent_same_provider_recursion: bool = True
    default_timeout_seconds: int = 1800


@dataclass
class DurabilityConfig:
    enabled: bool = True
    database_path: str = ""  # empty => XDG state/baldr-router/baldr.sqlite3
    journal_mode: str = "WAL"
    synchronous: str = "FULL"
    busy_timeout_ms: int = 5000
    lease_seconds: int = 45
    heartbeat_seconds: int = 5
    recovery_on_start: bool = True
    artifact_inline_limit_bytes: int = 32768
    retain_terminal_days: int = 90
    maintenance_on_start: bool = True
    maintenance_interval_minutes: int = 60
    backup_before_migrate: bool = True
    verify_artifact_hashes: bool = True
    wal_checkpoint_mode: str = "PASSIVE"


@dataclass
class SessionsConfig:
    ttl_hours: int = 24
    max_turns: int = 20
    invalidate_on_model_change: bool = True
    invalidate_on_provider_version_change: bool = True
    invalidate_on_repository_identity_change: bool = True


@dataclass
class ExecutionProfileConfig:
    """Provider/model execution configuration reusable across phases.

    The same profile may back all three phases, or each phase may reference an
    arbitrary ordered list of profiles. Empty fields inherit provider defaults.
    """

    provider: str = "codex"
    model: str = ""
    reasoning_effort: str = ""
    agent: str = ""
    effort: str = ""
    runner: str = ""
    session_scope: str = ""
    enabled: bool = True
    description: str = ""


@dataclass
class RoleConfig:
    # Legacy/inline execution profile. It remains useful for a single profile
    # and for backwards-compatible role overrides.
    provider: str = "codex"
    model: str = ""
    reasoning_effort: str = ""
    agent: str = ""
    effort: str = ""
    runner: str = ""
    session_scope: str = ""

    # Reusable named profiles. This permits 1 shared profile for every phase,
    # or n/m/l profiles for architecture/implementation/review respectively.
    profiles: list[str] = field(default_factory=list)
    strategy: str = "first-success"  # first-success | all
    min_successes: int = 1
    # Deterministic reducer applied when a phase has multiple successful profiles.
    resolution: str = ""
    min_approvals: int = 1

    can_write: bool = False
    sandbox: str = "read-only"
    description: str = ""


@dataclass
class WorkflowConfig:
    version: int = 1
    max_rounds: int = 2
    require_structured_output: bool = True
    require_review: bool = True
    description: str = ""


@dataclass
class AppConfig:
    router: RouterConfig
    codex: CodexConfig
    kiro_cli: KiroCliConfig
    context7: Context7Config
    workspace: WorkspaceConfig
    telemetry: TelemetryConfig
    probe: ProbeConfig
    verification: VerificationConfig
    qualification: QualificationConfig
    artifact_privacy: ArtifactPrivacyConfig
    safety: SafetyConfig
    durability: DurabilityConfig
    sessions: SessionsConfig
    execution_profiles: dict[str, ExecutionProfileConfig]
    roles: dict[str, RoleConfig]
    workflows: dict[str, WorkflowConfig]

    @classmethod
    def defaults(cls) -> "AppConfig":
        # One reusable profile backs every phase by default. Users can replace
        # each role's list with any number of profiles without changing the
        # public setup/status/run facade.
        shared = ExecutionProfileConfig(
            provider="codex",
            description="Shared provider defaults inherited by every phase.",
        )
        return cls(
            router=RouterConfig(),
            codex=CodexConfig(),
            kiro_cli=KiroCliConfig(),
            context7=Context7Config(),
            workspace=WorkspaceConfig(),
            telemetry=TelemetryConfig(),
            probe=ProbeConfig(),
            verification=VerificationConfig(),
            qualification=QualificationConfig(),
            artifact_privacy=ArtifactPrivacyConfig(),
            safety=SafetyConfig(),
            durability=DurabilityConfig(),
            sessions=SessionsConfig(),
            execution_profiles={"default": shared},
            roles={
                "architect": RoleConfig(
                    profiles=["default"],
                    can_write=False,
                    sandbox="read-only",
                    resolution="primary-with-advisors",
                    description="Plans the work, identifies risks, and defines acceptance criteria.",
                ),
                "implementer": RoleConfig(
                    profiles=["default"],
                    can_write=True,
                    sandbox="workspace-write",
                    resolution="first-success",
                    description="Implements the plan, edits files, and runs relevant verification.",
                ),
                "reviewer": RoleConfig(
                    profiles=["default"],
                    can_write=False,
                    sandbox="read-only",
                    resolution="any-blocker",
                    min_approvals=1,
                    description="Reviews the diff against the plan and flags blockers.",
                ),
            },
            workflows={
                "architect-implement-review": WorkflowConfig(
                    version=1,
                    max_rounds=2,
                    require_structured_output=True,
                    require_review=True,
                    description=(
                        "Architect plans, implementer applies changes, reviewer validates "
                        "the diff. Blockers get controlled fix rounds."
                    ),
                )
            },
        )


def _merge_dataclass(instance: Any, values: dict[str, Any]) -> Any:
    known = set(asdict(instance).keys())
    for key, value in values.items():
        if key in known:
            setattr(instance, key, value)
    return instance


def _merge_role(default: RoleConfig, values: dict[str, Any]) -> RoleConfig:
    role = RoleConfig(**asdict(default))
    return _merge_dataclass(role, values)


def _merge_workflow(default: WorkflowConfig, values: dict[str, Any]) -> WorkflowConfig:
    workflow = WorkflowConfig(**asdict(default))
    return _merge_dataclass(workflow, values)


def _merge_profile(
    default: ExecutionProfileConfig, values: dict[str, Any]
) -> ExecutionProfileConfig:
    profile = ExecutionProfileConfig(**asdict(default))
    return _merge_dataclass(profile, values)


def load_config(path: Path | None = None) -> AppConfig:
    p = path or config_path()
    cfg = AppConfig.defaults()
    if not p.exists():
        return cfg
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    scalar_tables = {
        "router": "router",
        "codex": "codex",
        "kiro_cli": "kiro_cli",
        "context7": "context7",
        "workspace": "workspace",
        "telemetry": "telemetry",
        "probe": "probe",
        "verification": "verification",
        "qualification": "qualification",
        "artifact_privacy": "artifact_privacy",
        "safety": "safety",
        "durability": "durability",
        "sessions": "sessions",
    }
    for table, attribute in scalar_tables.items():
        values = data.get(table)
        if isinstance(values, dict):
            current = getattr(cfg, attribute)
            setattr(cfg, attribute, _merge_dataclass(current, values))

    if isinstance(data.get("execution_profiles"), dict):
        for name, values in data["execution_profiles"].items():
            if isinstance(values, dict):
                default = cfg.execution_profiles.get(name, ExecutionProfileConfig())
                cfg.execution_profiles[name] = _merge_profile(default, values)

    if isinstance(data.get("roles"), dict):
        for name, values in data["roles"].items():
            if isinstance(values, dict):
                default = cfg.roles.get(name, RoleConfig())
                cfg.roles[name] = _merge_role(default, values)

    if isinstance(data.get("workflows"), dict):
        for name, values in data["workflows"].items():
            if isinstance(values, dict):
                default = cfg.workflows.get(name, WorkflowConfig())
                cfg.workflows[name] = _merge_workflow(default, values)
    return cfg


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_bool(value: bool) -> str:
    return str(bool(value)).lower()


def _toml_key(value: str) -> str:
    """Render one TOML key segment without turning literal dots into nesting."""

    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_str(value)


def _dump_dataclass_table(title: str, values: Any) -> list[str]:
    lines = [f"[{title}]"]
    for key, value in asdict(values).items():
        if isinstance(value, bool):
            lines.append(f"{key} = {_toml_bool(value)}")
        elif isinstance(value, int):
            lines.append(f"{key} = {int(value)}")
        elif isinstance(value, float):
            lines.append(f"{key} = {value}")
        elif isinstance(value, list):
            items = ", ".join(_toml_str(str(item)) for item in value)
            lines.append(f"{key} = [{items}]")
        else:
            lines.append(f"{key} = {_toml_str(str(value))}")
    lines.append("")
    return lines


def dump_config(cfg: AppConfig) -> str:
    lines: list[str] = []
    lines += _dump_dataclass_table("router", cfg.router)
    lines += _dump_dataclass_table("codex", cfg.codex)
    lines += _dump_dataclass_table("kiro_cli", cfg.kiro_cli)
    lines += _dump_dataclass_table("context7", cfg.context7)
    lines += _dump_dataclass_table("workspace", cfg.workspace)
    lines += _dump_dataclass_table("telemetry", cfg.telemetry)
    lines += _dump_dataclass_table("probe", cfg.probe)
    lines += _dump_dataclass_table("verification", cfg.verification)
    lines += _dump_dataclass_table("qualification", cfg.qualification)
    lines += _dump_dataclass_table("artifact_privacy", cfg.artifact_privacy)
    lines += _dump_dataclass_table("safety", cfg.safety)
    lines += _dump_dataclass_table("durability", cfg.durability)
    lines += _dump_dataclass_table("sessions", cfg.sessions)

    for profile_name in sorted(cfg.execution_profiles):
        lines += _dump_dataclass_table(
            f"execution_profiles.{_toml_key(profile_name)}",
            cfg.execution_profiles[profile_name],
        )

    for role_name in sorted(cfg.roles):
        lines += _dump_dataclass_table(
            f"roles.{_toml_key(role_name)}", cfg.roles[role_name]
        )

    for workflow_name in sorted(cfg.workflows):
        lines += _dump_dataclass_table(
            f"workflows.{_toml_key(workflow_name)}", cfg.workflows[workflow_name]
        )

    return "\n".join(lines)


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg), encoding="utf-8")
    return p
