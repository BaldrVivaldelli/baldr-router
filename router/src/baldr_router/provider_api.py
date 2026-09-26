from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import RoleConfig
from .provider_activity import ProviderActivitySink


@dataclass(frozen=True)
class ProviderCapabilities:
    """Capabilities and enforcement strength exposed by one provider adapter."""

    supports_read_only: bool = True
    supports_workspace_write: bool = False
    supports_structured_output: bool = True
    supports_sessions: bool = False
    # Whether a declared tool list actually narrows what this provider may use.
    # A provider that ignores one should say so, rather than let a restriction
    # be configured that changes nothing.
    supports_tool_restriction: bool = False
    read_only_enforcement: str = "advisory"
    write_enforcement: str = "advisory"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderRunRequest:
    role_name: str
    role: RoleConfig
    cwd: Path
    prompt: str
    workflow: str
    report_kind: str
    extra_env: dict[str, str] | None = None

    # Resolved execution profile. These fields are provider-neutral; adapters
    # consume only the values they understand.
    profile_name: str = "inline"
    model: str = ""
    reasoning_effort: str = ""
    agent: str = ""
    effort: str = ""
    runner: str = ""
    # What this step may use and how it should work, as declared by the agent
    # that was resolved for it. Both only ever narrow: the role decides whether
    # the step may write at all, and neither of these can widen that.
    tools: str = ""
    instructions: str = ""
    session_scope: str = ""
    session_key: str = ""
    resume_session_id: str | None = None
    durable_run_id: str = ""
    durable_step_id: str = ""
    durable_attempt_id: str = ""
    activity_sink: ProviderActivitySink | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    """Stable provider contract used by the orchestration core."""

    name: str
    aliases: tuple[str, ...]
    capabilities: ProviderCapabilities

    def status(self) -> dict[str, Any]:
        """Return provider availability/auth/configuration information."""

    def run(self, request: ProviderRunRequest) -> dict[str, Any]:
        """Execute one role step and return a provider result payload."""
