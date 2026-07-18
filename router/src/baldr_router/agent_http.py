from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .agent_api import (
    AgentContractError,
    AgentInvocation,
    AgentTransportError,
    ResolvedAgent,
)
from .agent_execution import build_execution_invocation, consume_execution_messages

INVOCATION_CONTRACT = "baldr-agent-invocation"
RESULT_CONTRACT = "baldr-agent-result"
HTTP_CONTRACT_VERSION = 1
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_http_endpoint(url: str, *, allow_insecure_loopback: bool) -> str:
    endpoint = str(url or "").strip()
    if len(endpoint) > 2048:
        raise AgentContractError("Agent HTTP endpoint is too long.")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.fragment:
        raise AgentContractError(
            "Agent HTTP endpoints cannot contain credentials or fragments."
        )
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise AgentContractError("Agent HTTP endpoints require an http(s) URL.")
    if parsed.scheme == "http" and not (
        allow_insecure_loopback and _is_loopback(parsed.hostname)
    ):
        raise AgentContractError(
            "Agent HTTP endpoints require HTTPS; plain HTTP is limited to explicitly enabled loopback pilots."
        )
    return urllib.parse.urlunsplit(parsed)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


class JsonHttpClient:
    def __init__(
        self,
        *,
        allow_insecure_loopback: bool = False,
        max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
    ) -> None:
        self.allow_insecure_loopback = allow_insecure_loopback
        self.max_response_bytes = max(1024, min(int(max_response_bytes), 8 * 1024 * 1024))
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _timeout(raw: str | int | float | None) -> float:
        try:
            value = float(raw or 30)
        except (TypeError, ValueError) as exc:
            raise AgentContractError("Agent HTTP timeout must be numeric.") from exc
        if not 0.1 <= value <= 300:
            raise AgentContractError("Agent HTTP timeout must be between 0.1 and 300 seconds.")
        return value

    @staticmethod
    def _authorization_header(auth_env: str) -> str:
        name = str(auth_env or "").strip()
        if not name:
            return ""
        if not _ENV_NAME.fullmatch(name):
            raise AgentContractError(
                "Agent HTTP authorization_env must name an uppercase environment variable."
            )
        value = os.environ.get(name, "").strip()
        if not value:
            raise AgentContractError(
                f"Agent HTTP credential environment variable {name!r} is unavailable."
            )
        return value if " " in value else f"Bearer {value}"

    def request_json(
        self,
        *,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None = None,
        auth_env: str = "",
        timeout_seconds: str | int | float | None = None,
    ) -> dict[str, Any]:
        endpoint = validate_http_endpoint(
            url, allow_insecure_loopback=self.allow_insecure_loopback
        )
        body = None
        if payload is not None:
            body = json.dumps(
                dict(payload), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(body) > 2 * 1024 * 1024:
                raise AgentContractError("Agent HTTP request exceeds the 2 MiB limit.")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "baldr-router-agent-gateway/1",
        }
        authorization = self._authorization_header(auth_env)
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self._opener.open(
                request, timeout=self._timeout(timeout_seconds)
            ) as response:
                content_type = str(response.headers.get("Content-Type") or "")
                if "application/json" not in content_type.lower():
                    raise AgentTransportError(
                        "Agent HTTP response is not JSON.", retryable=False
                    )
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or 500 <= exc.code <= 599
            raise AgentTransportError(
                f"Agent HTTP request failed with status {exc.code}.",
                retryable=retryable,
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentTransportError(
                "Agent HTTP endpoint is unavailable.", retryable=True
            ) from exc
        if len(raw) > self.max_response_bytes:
            raise AgentTransportError(
                "Agent HTTP response exceeds the configured size limit.",
                retryable=False,
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AgentTransportError(
                "Agent HTTP response is not valid UTF-8 JSON.", retryable=False
            ) from exc
        if not isinstance(decoded, dict):
            raise AgentTransportError(
                "Agent HTTP response must be an object.", retryable=False
            )
        return decoded


class HttpJsonAgentConnector:
    """Invoke an externally hosted agent through the Baldr HTTP JSON v1 contract."""

    transport = "http-json"

    def __init__(self, client: JsonHttpClient | None = None) -> None:
        self.client = client or JsonHttpClient(
            allow_insecure_loopback=_env_enabled(
                "BALDR_AGENT_ALLOW_INSECURE_LOOPBACK"
            )
        )

    def invoke(
        self, resolved: ResolvedAgent, invocation: AgentInvocation
    ) -> dict[str, Any]:
        if invocation.can_write:
            raise AgentContractError(
                "Agent HTTP JSON v1 is read-only because it does not expose a shared workspace boundary."
            )
        target = resolved.manifest.target
        endpoint = str(target.get("endpoint") or "")
        if not endpoint:
            raise AgentContractError(
                f"HTTP agent {resolved.reference} has no target.endpoint."
            )
        if target.get("protocol") == "agent-execution-v1":
            try:
                timeout = int(str(target.get("timeout_seconds") or "30"))
            except ValueError as exc:
                raise AgentContractError(
                    "Agent HTTP timeout_seconds must be an integer for agent-execution-v1."
                ) from exc
            request = build_execution_invocation(
                resolved,
                invocation,
                timeout_seconds=timeout,
                include_workspace_root=False,
            )
            response = self.client.request_json(
                method="POST",
                url=endpoint,
                payload=request,
                auth_env=str(target.get("authorization_env") or ""),
                timeout_seconds=timeout,
            )
            return consume_execution_messages(
                [response],
                request=request,
                invocation=invocation,
            )
        payload = {
            "contract": INVOCATION_CONTRACT,
            "version": HTTP_CONTRACT_VERSION,
            "agent": {
                "ref": str(resolved.reference),
                "digest": resolved.manifest.digest,
            },
            "invocation": {
                "task": invocation.task,
                "workflow": invocation.workflow,
                "step_name": invocation.step_name,
                "report_kind": invocation.report_kind,
                "can_write": invocation.can_write,
                "sandbox": invocation.sandbox,
                "profile_name": invocation.profile_name,
                "session_key": invocation.session_key,
                "resume_session_id": invocation.resume_session_id,
                "durable_run_id": invocation.durable_run_id,
                "durable_step_id": invocation.durable_step_id,
                "durable_attempt_id": invocation.durable_attempt_id,
                "requested_capabilities": list(invocation.requested_capabilities),
            },
        }
        response = self.client.request_json(
            method="POST",
            url=endpoint,
            payload=payload,
            auth_env=str(target.get("authorization_env") or ""),
            timeout_seconds=target.get("timeout_seconds"),
        )
        if (
            response.get("contract") != RESULT_CONTRACT
            or response.get("version") != HTTP_CONTRACT_VERSION
        ):
            raise AgentContractError(
                "Agent HTTP response does not implement baldr-agent-result v1."
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AgentContractError("Agent HTTP result must be an object.")
        return dict(result)
