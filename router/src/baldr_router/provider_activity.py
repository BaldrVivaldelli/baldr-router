from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


ProviderActivitySink = Callable[[str], None]

# This is a deliberately tiny public vocabulary. Provider payloads can contain
# prompts, commands, paths, model text, or secrets; none of those values may
# cross the activity boundary.
PUBLIC_ACTIVITY_CATEGORIES = frozenset(
    {"working", "analyzing", "researching", "changing", "verifying"}
)

_RESEARCH_TYPES = frozenset({"web_search", "web-search", "websearch", "browse"})
_CHANGE_TYPES = frozenset(
    {
        "file_change",
        "file-change",
        "filechange",
        "patch",
        "edit",
        "write_file",
        "writefile",
    }
)
_COMMAND_TYPES = frozenset(
    {"command_execution", "command-execution", "commandexecution"}
)
_VERIFICATION_TYPES = frozenset({"test", "check"})
_ANALYSIS_TYPES = frozenset({"reasoning"})
_WORK_TYPES = frozenset({"agent_message", "agent-message", "agentmessage"})
_ANALYSIS_LIFECYCLE = frozenset(
    {"thread.started", "thread/started", "turn.started", "turn/started"}
)


def emit_provider_activity(
    sink: ProviderActivitySink | None,
    category: str,
) -> bool:
    """Best-effort delivery of one allowlisted, payload-free activity marker."""

    if sink is None or category not in PUBLIC_ACTIVITY_CATEGORIES:
        return False
    try:
        sink(category)
    except Exception:
        # Progress is observational. It must never alter provider completion,
        # cancellation, lease fencing, or recovery behavior.
        return False
    return True


def generic_activity_for_role(role_name: str) -> str:
    """Return a truthful coarse marker for providers without streaming.

    A role name describes responsibility, not an observed action. Starting an
    implementer does not prove that a file changed, and starting a reviewer
    does not prove that a check ran. Keep the public claim deliberately weak
    until the runner emits a typed event with stronger evidence.
    """

    del role_name
    return "working"


def codex_public_activity(event: Mapping[str, Any]) -> str | None:
    """Map a Codex wire event to a static public category.

    Only protocol type names are inspected. Free-form text, commands, paths,
    arguments, tool results, and reasoning are deliberately ignored.
    """

    event_type = str(event.get("type") or "").strip().lower()
    method = str(event.get("method") or "").strip().lower()
    params = event.get("params")
    params_map = params if isinstance(params, Mapping) else {}
    item = event.get("item")
    if not isinstance(item, Mapping):
        item = params_map.get("item")
    item_map = item if isinstance(item, Mapping) else {}
    item_type = str(item_map.get("type") or "").strip().lower()

    # Match complete protocol identifiers only. Substring matching would turn
    # unrelated types such as ``latest_message`` or ``credit_received`` into
    # fabricated progress. Values can influence only a fixed allowlist member.
    protocol_types = {event_type, method, item_type}
    if protocol_types & _RESEARCH_TYPES:
        return "researching"
    if protocol_types & _CHANGE_TYPES:
        return "changing"

    # A command may inspect, test, build, edit, or delete. Its type proves only
    # that work is happening; it is not evidence of a change or verification.
    if protocol_types & _COMMAND_TYPES:
        return "working"
    if protocol_types & _VERIFICATION_TYPES:
        return "verifying"
    if protocol_types & _ANALYSIS_TYPES:
        return "analyzing"
    if protocol_types & _WORK_TYPES:
        return "working"
    if event_type in _ANALYSIS_LIFECYCLE or method in _ANALYSIS_LIFECYCLE:
        return "working"
    return None
