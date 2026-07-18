from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .agent_api import (
    AgentContractError,
    AgentManifest,
    AgentNotFoundError,
    AgentRef,
    AgentTransportError,
)
from .codex import codex_model_catalog
from .agent_gateway import external_agent_catalog_status
from .agent_manager import (
    AgentManagerPublisher,
    HttpAgentManagerAdmin,
    agent_manager_status,
    write_agent_publication,
)
from .agent_manager_policy import policy_template
from .agent_registry import LocalAgentRegistryAdmin, local_agent_registry_path
from .agent_sources import (
    AgentManagerSource,
    AgentSourceContext,
    KiroAgentSource,
    ManifestAgentSource,
)
from .agent_sync import (
    AgentCatalogSynchronizer,
    agent_sync_state_status,
    local_agent_sync_state_path,
)
from .kiro_cli import kiro_cli_mcp_status
from .codex_config import install_context7_mcp_config, remove_context7_mcp_config
from .config import Context7Config, load_config, save_config
from .provider_registry import get_provider_registry, provider_status
from .workflows import (
    list_roles,
    list_workflows,
    run_workflow_impl,
    set_role_provider,
    workflow_status,
)
from .context7 import cache_status, clear_cache, lookup_docs_for_library
from .context7_setup import context7_onboarding_plan, enable_context7_env_source
from .extensions import extension_status
from .facade import facade_run, facade_setup_plan, facade_status_report
from .facade_contract import facade_contract_status
from .discovery.environment_probe import environment_probe
from .discovery.workspace_profile import workspace_profile, workspace_profile_status
from .evidence import latest_evidence, list_evidence
from .lab.matrix import run_lab_matrix
from .qualification.receipts import record_client_receipt
from .qualification.runner import (
    definitions_status as qualification_definitions_status,
    latest_qualification,
    list_qualifications,
    run_qualification,
    write_qualification_template,
)
from .validation.lifecycle import run_lifecycle_verification
from .durability.store import DurableStore
from .secrets import prompt_context7_key_and_store
from .status import doctor
from .telemetry import app_state_dir, recent_runs, telemetry_stats
from .workspace_policy import inspect_workspace, trust_workspace, untrust_workspace


VALID_CODEX_RUNNERS = {"exec-json", "app-server", "sdk"}
VALID_CONTEXT7_MODES = {"off", "codex-mcp", "router-cache", "hybrid"}


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _parse_role_profiles(values: list[str] | None) -> dict[str, list[str]] | None:
    if not values:
        return None
    result: dict[str, list[str]] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"--role-profile expects role=profile[,profile]: {raw!r}")
        role, profiles = raw.split("=", 1)
        role = role.strip()
        if role not in {"architect", "implementer", "reviewer"}:
            raise SystemExit(f"Unknown role in --role-profile: {role!r}")
        selected = [item.strip() for item in profiles.split(",") if item.strip()]
        if not selected:
            raise SystemExit(f"No profiles supplied for role {role!r}")
        result[role] = selected
    return result


def _parse_agent_overrides(
    values: list[str] | None, *, clear: bool = False
) -> dict[str, str] | None:
    if clear:
        if values:
            raise SystemExit(
                "--clear-agent-overrides cannot be combined with --agent-override"
            )
        return {}
    if not values:
        return None
    result: dict[str, str] = {}
    for raw in values:
        role, separator, reference = raw.partition("=")
        role = role.strip()
        reference = reference.strip()
        if not separator or not reference:
            raise SystemExit(f"--agent-override expects role=AgentRef: {raw!r}")
        if role not in {"architect", "implementer", "reviewer"}:
            raise SystemExit(f"Unknown role in --agent-override: {role!r}")
        if role in result:
            raise SystemExit(f"Duplicate --agent-override role: {role!r}")
        result[role] = reference
    return result


def _parse_json_object(raw: str | None, flag: str) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{flag} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{flag} must decode to an object")
    return value


def _parse_json_array(raw: str | None, flag: str) -> list[dict[str, object]] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{flag} must be valid JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SystemExit(f"{flag} must decode to an array of objects")
    return value


def _parse_agent_target(values: list[str] | None) -> dict[str, str]:
    target: dict[str, str] = {}
    for raw in values or []:
        key, separator, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise SystemExit(f"--target expects key=value: {raw!r}")
        if key in target:
            raise SystemExit(f"Duplicate --target key: {key!r}")
        target[key] = value
    if not target:
        raise SystemExit("At least one --target key=value is required")
    return target


def cmd_facade(args: argparse.Namespace) -> int:
    if args.intent == "contract":
        print_json(facade_contract_status())
        return 0

    client = getattr(args, "client", "generic-mcp")
    if args.intent == "setup":
        result = facade_setup_plan(
            getattr(args, "workspace_root", None),
            client=client,
            trust_current_workspace=getattr(args, "trust_workspace", False),
            workspace_safety_mode=getattr(args, "workspace_safety_mode", None),
            execution_preset=getattr(args, "execution_preset", None),
            context7_policy=getattr(args, "context_mode", None)
            or getattr(args, "context7_policy", None),
            role_profiles=_parse_role_profiles(getattr(args, "role_profile", None)),
            team_mode=getattr(args, "team_mode", None),
            agent_overrides=_parse_agent_overrides(
                getattr(args, "agent_override", None),
                clear=getattr(args, "clear_agent_overrides", False),
            ),
            allow_non_git=getattr(args, "allow_non_git", False),
            profile_definition=_parse_json_object(
                getattr(args, "profile_definition_json", None),
                "--profile-definition-json",
            ),
        )
    elif args.intent == "status":
        result = facade_status_report(
            getattr(args, "workspace_root", None),
            client=client,
            recent_limit=getattr(args, "recent_limit", 5),
            work_item_id=getattr(args, "work_item_id", None),
            work_item_limit=getattr(args, "work_item_limit", 100),
            include_archived=getattr(args, "include_archived", False),
            workbench_only=getattr(args, "workbench_only", False),
        )
    elif args.intent == "run":
        result = facade_run(
            args.workspace_root,
            args.task,
            client=client,
            extra_context=getattr(args, "extra_context", "") or "",
            architect_provider=getattr(args, "architect_provider", None),
            implementer_provider=getattr(args, "implementer_provider", None),
            reviewer_provider=getattr(args, "reviewer_provider", None),
            max_rounds=getattr(args, "max_rounds", None),
            context7_libraries=getattr(args, "context7_library", None),
            dry_run=getattr(args, "dry_run", False),
            idempotency_key=getattr(args, "idempotency_key", None),
            resume_run_id=getattr(args, "resume_run_id", None),
            reconciliation_action=getattr(args, "reconciliation_action", None),
            cancel=getattr(args, "cancel", False),
            cancel_reason=getattr(
                args, "cancel_reason", "Cancellation requested by client."
            ),
            work_item_action=getattr(args, "work_item_action", "execute"),
            work_item_id=getattr(args, "work_item_id", None),
            title=getattr(args, "title", None),
            workspace_mode=getattr(args, "workspace_mode", None),
            execution_preset=getattr(args, "execution_preset", None),
            context7_policy=getattr(args, "context_mode", None)
            or getattr(args, "context7_policy", None),
            role_profiles=_parse_role_profiles(getattr(args, "role_profile", None)),
            team_mode=getattr(args, "team_mode", None),
            agent_overrides=_parse_agent_overrides(
                getattr(args, "agent_override", None),
                clear=getattr(args, "clear_agent_overrides", False),
            ),
            remember_workspace=getattr(args, "remember_workspace", False),
            allow_non_git=getattr(args, "allow_non_git", False),
            attachments=_parse_json_array(
                getattr(args, "attachments_json", None), "--attachments-json"
            ),
            item_config=_parse_json_object(
                getattr(args, "item_config_json", None), "--item-config-json"
            ),
            phase_stage=getattr(args, "phase_stage", None),
            phase_round=getattr(args, "phase_round", None),
            phase_run_ordinal=getattr(args, "phase_run_ordinal", None),
            phase_cursor=getattr(args, "phase_cursor", None),
            phase_page_size=getattr(args, "phase_page_size", 20),
            deliverable_cursor=getattr(args, "deliverable_cursor", None),
            deliverable_page_size=getattr(args, "deliverable_page_size", 20),
        )
    else:  # pragma: no cover - argparse enforces the choices
        raise AssertionError(f"Unhandled facade intent: {args.intent}")

    print_json(result)
    if args.intent in {"setup", "status"}:
        return 0
    return 0 if result.get("ok") else 2


def cmd_workspace_status(args: argparse.Namespace) -> int:
    print_json(inspect_workspace(args.workspace_root, access=args.access))
    return 0


def cmd_workspace_trust(args: argparse.Namespace) -> int:
    result = trust_workspace(args.workspace_root, force=args.force)
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_workspace_untrust(args: argparse.Namespace) -> int:
    print_json(untrust_workspace(args.workspace_root))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print_json(doctor(args.workspace_root))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .server import run_mcp

    run_mcp()
    return 0


def cmd_set_provider(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.router.default_provider = args.provider
    saved = save_config(cfg)
    implemented = get_provider_registry().canonical_names()
    result = {
        "ok": True,
        "config_path": str(saved),
        "default_provider": cfg.router.default_provider,
        "implemented_providers": implemented,
    }
    if args.provider not in implemented:
        result["warning"] = (
            "Provider stored, but no adapter is implemented for it in this release."
        )
    print_json(result)
    return 0


def cmd_provider_status(args: argparse.Namespace) -> int:
    print_json(provider_status())
    return 0


def cmd_kiro_mcp_status(args: argparse.Namespace) -> int:
    del args
    result = kiro_cli_mcp_status()
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_provider_models(args: argparse.Namespace) -> int:
    provider = str(args.provider or "codex").strip().lower().replace("_", "-")
    if provider not in {"codex", "openai-codex"}:
        print_json(
            {
                "ok": False,
                "provider": provider,
                "reason": f"Model discovery is not available for provider {provider!r}.",
            }
        )
        return 2
    result = codex_model_catalog(force=bool(args.refresh))
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_agent_catalog(args: argparse.Namespace) -> int:
    result = external_agent_catalog_status(
        workspace_root=getattr(args, "workspace", None)
    )
    print_json(result)
    return 0 if result.get("ok") else 2


def _agent_sources_from_args(args: argparse.Namespace):
    cfg = load_config()
    selected = str(args.source or "kiro").strip().lower()
    sources = []
    if selected in {"kiro", "all"}:
        sources.append(KiroAgentSource())
    if selected in {"manager", "all"}:
        if cfg.agent_manager.enabled:
            sources.append(AgentManagerSource(cfg.agent_manager))
        elif selected == "manager":
            raise AgentNotFoundError("Agent Manager is not configured.")
    if selected == "file":
        if not args.path:
            raise AgentContractError("--path is required for a file source.")
        sources.append(
            ManifestAgentSource(
                path=Path(args.path),
                expected_source_id=args.expected_source_id,
            )
        )
    if selected == "endpoint":
        if not args.endpoint:
            raise AgentContractError("--endpoint is required for an endpoint source.")
        sources.append(
            ManifestAgentSource(
                endpoint=args.endpoint,
                authorization_env=args.authorization_env,
                timeout_seconds=args.timeout_seconds,
                allow_insecure_loopback=bool(args.allow_insecure_loopback),
                expected_source_id=args.expected_source_id,
            )
        )
    if not sources:
        raise AgentContractError(f"Unknown agent source: {selected!r}.")
    return sources


def _discover_agent_sources(args: argparse.Namespace):
    workspace = Path(args.workspace or ".").expanduser().resolve()
    return [
        source.discover(context=AgentSourceContext(workspace, limit=args.limit))
        for source in _agent_sources_from_args(args)
    ]


def cmd_agent_discover(args: argparse.Namespace) -> int:
    try:
        discovered = _discover_agent_sources(args)
    except (
        AgentContractError,
        AgentNotFoundError,
        AgentTransportError,
        OSError,
    ) as exc:
        return _agent_command_error(exc)
    documents = [result.to_dict() for result in discovered]
    print_json(
        {
            "ok": True,
            "source_count": len(documents),
            "candidate_count": sum(len(result.candidates) for result in discovered),
            "sources": documents,
        }
    )
    return 0


def cmd_agent_sync(args: argparse.Namespace) -> int:
    try:
        discovered = [result for result in _discover_agent_sources(args)]
        if args.missing_action == "revoke" and len(discovered) != 1:
            raise AgentContractError(
                "Irreversible revocation can synchronize only one named source at a time."
            )
        synchronizer = AgentCatalogSynchronizer(local_agent_registry_path())
        if args.apply:
            results = [
                synchronizer.apply(
                    result,
                    missing_action=args.missing_action,
                    confirm_revoke=args.confirm_revoke,
                    actor=args.actor,
                )
                for result in discovered
            ]
            mode = "apply"
        else:
            results = [synchronizer.preview(result).to_dict() for result in discovered]
            mode = "preview"
    except (
        AgentContractError,
        AgentNotFoundError,
        AgentTransportError,
        OSError,
    ) as exc:
        return _agent_command_error(exc)
    print_json(
        {
            "ok": True,
            "mode": mode,
            "source_count": len(results),
            "results": results,
        }
    )
    return 0


def cmd_agent_sync_status(args: argparse.Namespace) -> int:
    del args
    try:
        result = agent_sync_state_status(
            local_agent_sync_state_path(local_agent_registry_path().resolve())
        )
    except (AgentContractError, OSError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def _agent_command_error(error: Exception) -> int:
    print_json(
        {
            "ok": False,
            "error": {
                "code": "agent_registry_operation_failed",
                "message": str(error),
            },
        }
    )
    return 2


def _manifest_from_args(args: argparse.Namespace) -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse(args.reference),
        owner=args.owner,
        transport=args.transport,
        target=_parse_agent_target(args.target),
        capabilities=tuple(args.capability or ()),
        input_schema=args.input_schema,
        output_schema=args.output_schema,
        effect_mode=args.effect_mode,
        supports_sessions=bool(args.supports_sessions),
        supports_cancellation=bool(args.supports_cancellation),
        declared_digest=args.digest or "",
    )


def cmd_agent_publish(args: argparse.Namespace) -> int:
    try:
        manifest = _manifest_from_args(args)
        result = LocalAgentRegistryAdmin().publish(manifest)
    except (AgentContractError, AgentNotFoundError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_configure(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.agent_manager.enabled = True
    cfg.agent_manager.registry = args.registry
    cfg.agent_manager.base_url = args.base_url
    cfg.agent_manager.authorization_env = args.authorization_env
    cfg.agent_manager.allow_insecure_loopback = bool(args.allow_insecure_loopback)
    path = save_config(cfg)
    print_json(
        {
            "ok": True,
            "config_path": str(path),
            "agent_manager": {
                "enabled": True,
                "registry": cfg.agent_manager.registry,
                "base_url": cfg.agent_manager.base_url,
                "authorization_env": cfg.agent_manager.authorization_env,
                "allow_insecure_loopback": cfg.agent_manager.allow_insecure_loopback,
            },
        }
    )
    return 0


def cmd_agent_manager_status(args: argparse.Namespace) -> int:
    del args
    result = agent_manager_status(load_config().agent_manager)
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_agent_manager_audit(args: argparse.Namespace) -> int:
    try:
        result = HttpAgentManagerAdmin(load_config().agent_manager).audit(
            after=args.after,
            limit=args.limit,
        )
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_metrics(args: argparse.Namespace) -> int:
    del args
    try:
        result = HttpAgentManagerAdmin(load_config().agent_manager).metrics()
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_init_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = _manifest_from_args(args)
        output = write_agent_publication(
            Path(args.output),
            manifest,
            overwrite=bool(args.force),
        )
    except (AgentContractError, AgentNotFoundError) as exc:
        return _agent_command_error(exc)
    print_json(
        {
            "ok": True,
            "output": str(output),
            "reference": str(manifest.reference),
            "digest": manifest.digest,
        }
    )
    return 0


def cmd_agent_manager_validate_manifest(args: argparse.Namespace) -> int:
    try:
        result = AgentManagerPublisher.validate_file(Path(args.path))
    except AgentContractError as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_publish_file(args: argparse.Namespace) -> int:
    try:
        result = AgentManagerPublisher(load_config().agent_manager).publish_file(
            Path(args.path)
        )
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def _write_private_json(path: Path, document: dict[str, object]) -> Path:
    target = path.expanduser()
    if target.is_symlink() or target.exists():
        raise AgentContractError("Output already exists or is a symbolic link.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with target.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        target.chmod(0o600)
    except (FileExistsError, OSError) as exc:
        raise AgentContractError("Output could not be written safely.") from exc
    return target.resolve()


def cmd_agent_manager_init_policy(args: argparse.Namespace) -> int:
    try:
        document = policy_template(
            registry=args.registry,
            principal_id=args.principal_id,
            credential_env=args.credential_env,
            roles=tuple(args.role or ("admin",)),
            tenants=tuple(args.tenant or ("*",)),
            owners=tuple(args.owner_scope or ("*",)),
        )
        output = _write_private_json(Path(args.output), document)
    except AgentContractError as exc:
        return _agent_command_error(exc)
    print_json({"ok": True, "output": str(output), "policy": document})
    return 0


def _agent_manager_database(args: argparse.Namespace) -> Path:
    return (
        Path(args.database).expanduser()
        if args.database
        else app_state_dir() / "agent-manager.sqlite3"
    )


def cmd_agent_manager_doctor(args: argparse.Namespace) -> int:
    from .agent_manager_service import AgentManagerStore

    try:
        store = AgentManagerStore(
            _agent_manager_database(args),
            registry=args.registry,
        )
        result = {
            "ok": True,
            "database": str(store.path),
            "registry": store.registry,
            "schema_version": store.schema_version,
            "catalog": store.health(),
            "metrics": store.metrics(tenants=None),
        }
    except (AgentContractError, OSError, ValueError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_backup(args: argparse.Namespace) -> int:
    from .agent_manager_service import AgentManagerStore

    try:
        result = AgentManagerStore(
            _agent_manager_database(args),
            registry=args.registry,
        ).backup(Path(args.output))
    except (AgentContractError, OSError, ValueError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_publish(args: argparse.Namespace) -> int:
    try:
        result = HttpAgentManagerAdmin(load_config().agent_manager).publish(
            _manifest_from_args(args)
        )
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_set_enabled(args: argparse.Namespace) -> int:
    try:
        result = HttpAgentManagerAdmin(load_config().agent_manager).set_enabled(
            AgentRef.parse(args.reference), enabled=bool(args.enabled)
        )
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_revoke(args: argparse.Namespace) -> int:
    try:
        result = HttpAgentManagerAdmin(load_config().agent_manager).revoke(
            AgentRef.parse(args.reference)
        )
    except (AgentContractError, AgentNotFoundError, AgentTransportError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_manager_serve(args: argparse.Namespace) -> int:
    from .agent_manager_service import serve_agent_manager

    database = _agent_manager_database(args)
    print_json(
        {
            "ok": True,
            "listening": f"http://{args.host}:{args.port}",
            "registry": args.registry,
            "database": str(database),
            "authorization_env": args.authorization_env,
            "policy": str(Path(args.policy).expanduser()) if args.policy else None,
        }
    )
    sys.stdout.flush()
    serve_agent_manager(
        host=args.host,
        port=args.port,
        database=database,
        registry=args.registry,
        authorization_env=args.authorization_env,
        policy_path=Path(args.policy).expanduser() if args.policy else None,
    )
    return 0


def cmd_agent_inspect(args: argparse.Namespace) -> int:
    try:
        result = LocalAgentRegistryAdmin().inspect(args.reference)
    except (AgentContractError, AgentNotFoundError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_set_enabled(args: argparse.Namespace) -> int:
    try:
        result = LocalAgentRegistryAdmin().set_enabled(
            args.reference, enabled=bool(args.enabled)
        )
    except (AgentContractError, AgentNotFoundError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_revoke(args: argparse.Namespace) -> int:
    try:
        reference = str(AgentRef.parse(args.reference))
        try:
            active = DurableStore().active_runs_using_agent(reference)
        except Exception:
            raise AgentContractError(
                "Durable agent usage could not be checked before revocation."
            ) from None
        if active:
            raise AgentContractError(
                f"Agent {reference} is used by active durable runs: "
                + ", ".join(active[:5])
                + "."
            )
        if args.confirm_reference != reference:
            raise AgentContractError(
                "Irreversible revocation requires --confirm-reference to equal the AgentRef."
            )
        result = LocalAgentRegistryAdmin().revoke(reference)
    except (AgentContractError, AgentNotFoundError, RuntimeError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_agent_remove(args: argparse.Namespace) -> int:
    try:
        reference = str(AgentRef.parse(args.reference))
        active = DurableStore().active_runs_using_agent(reference)
        result = LocalAgentRegistryAdmin().remove(
            reference,
            active_run_ids=active,
        )
    except (AgentContractError, AgentNotFoundError, RuntimeError) as exc:
        return _agent_command_error(exc)
    print_json(result)
    return 0


def cmd_workflow_status(args: argparse.Namespace) -> int:
    print_json(workflow_status())
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    print_json(list_roles())
    return 0


def cmd_workflows(args: argparse.Namespace) -> int:
    print_json(list_workflows())
    return 0


def cmd_set_role_provider(args: argparse.Namespace) -> int:
    print_json(
        set_role_provider(
            args.role, args.provider, agent=args.agent, effort=args.effort
        )
    )
    return 0


def cmd_enable_kiro_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.kiro_cli.enabled = True
    cfg.kiro_cli.command = args.command
    cfg.kiro_cli.default_agent = args.agent
    cfg.kiro_cli.default_effort = args.effort
    cfg.kiro_cli.require_api_key = not args.no_require_api_key
    cfg.kiro_cli.api_key_env = args.api_key_env
    saved = save_config(cfg)
    print_json(
        {
            "ok": True,
            "config_path": str(saved),
            "kiro_cli": cfg.kiro_cli.__dict__,
            "note": "The kiro-cli provider is best used as an architect/reviewer/second-opinion provider. Avoid enabling baldr-router re-entry in child agents.",
        }
    )
    return 0


def cmd_disable_kiro_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.kiro_cli.enabled = False
    saved = save_config(cfg)
    print_json(
        {"ok": True, "config_path": str(saved), "kiro_cli": cfg.kiro_cli.__dict__}
    )
    return 0


def cmd_run_workflow(args: argparse.Namespace) -> int:
    result = run_workflow_impl(
        workspace_root=args.workspace_root,
        task=args.task,
        workflow=args.workflow,
        extra_context=args.extra_context or "",
        architect_provider=args.architect_provider,
        implementer_provider=args.implementer_provider,
        reviewer_provider=args.reviewer_provider,
        max_rounds=args.max_rounds,
        context7_libraries=args.context7_library,
        dry_run=args.dry_run,
        idempotency_key=args.idempotency_key,
        resume_run_id=args.resume_run_id,
        reconciliation_action=args.reconciliation_action,
        cancel=args.cancel,
        cancel_reason=args.cancel_reason,
        client_name=args.client,
    )
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_set_codex_runner(args: argparse.Namespace) -> int:
    if args.runner not in VALID_CODEX_RUNNERS:
        raise SystemExit(
            f"Invalid runner: {args.runner}. Valid: {', '.join(sorted(VALID_CODEX_RUNNERS))}"
        )
    cfg = load_config()
    cfg.codex.runner = args.runner
    cfg.codex.session_scope = args.session_scope
    saved = save_config(cfg)
    result = {
        "ok": True,
        "config_path": str(saved),
        "codex": {
            "runner": cfg.codex.runner,
            "session_scope": cfg.codex.session_scope,
        },
    }
    if args.runner in {"app-server", "sdk"}:
        result["note"] = (
            "This runner is experimental. exec-json remains the safest default."
        )
    print_json(result)
    return 0


def cmd_setup_context7(args: argparse.Namespace) -> int:
    if args.mode not in VALID_CONTEXT7_MODES:
        raise SystemExit(
            f"Invalid Context7 mode: {args.mode}. Valid: {', '.join(sorted(VALID_CONTEXT7_MODES))}"
        )
    cfg = load_config()
    source = args.source
    if args.mode == "off":
        cfg.context7.enabled = False
        cfg.context7.mode = "off"
        saved = save_config(cfg)
        print_json(
            {"ok": True, "config_path": str(saved), "context7": cfg.context7.__dict__}
        )
        return 0

    if source == "local-file":
        path = prompt_context7_key_and_store()
        print(f"Saved Context7 key to {path} with 0600 permissions.", file=sys.stderr)
    elif source.startswith("env:"):
        env_name = source.split(":", 1)[1]
        print(
            f"Using environment source: {env_name}. Make sure it is available to the router process.",
            file=sys.stderr,
        )
    else:
        raise SystemExit(f"Unsupported source: {source}")

    cfg.context7 = Context7Config(
        enabled=True,
        mode=args.mode,
        api_key_source=source,
        install_codex_mcp=bool(
            args.install_codex_mcp or args.mode in {"codex-mcp", "hybrid"}
        ),
        cache_ttl_hours=args.cache_ttl_hours,
        inject_docs=not args.no_inject_docs,
        max_libraries=args.max_libraries,
        max_chars=args.max_chars,
        fast=not args.no_fast,
    )
    saved = save_config(cfg)
    result = {"ok": True, "config_path": str(saved), "context7": cfg.context7.__dict__}
    if cfg.context7.install_codex_mcp:
        result["codex_mcp"] = install_context7_mcp_config(force=args.force)
    print_json(result)
    return 0


def cmd_disable_context7(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.context7.enabled = False
    cfg.context7.mode = "off"
    cfg.context7.install_codex_mcp = False
    saved = save_config(cfg)
    result = {"ok": True, "config_path": str(saved), "context7_enabled": False}
    if args.remove_codex_mcp:
        result["codex_mcp"] = remove_context7_mcp_config()
    print_json(result)
    return 0


def cmd_install_codex_context7(args: argparse.Namespace) -> int:
    print_json(install_context7_mcp_config(force=args.force))
    return 0


def cmd_remove_codex_context7(args: argparse.Namespace) -> int:
    print_json(remove_context7_mcp_config())
    return 0


def cmd_context7_lookup(args: argparse.Namespace) -> int:
    print_json(lookup_docs_for_library(args.library, args.query, fast=args.fast))
    return 0


def cmd_context7_cache_status(args: argparse.Namespace) -> int:
    print_json(cache_status())
    return 0


def cmd_context7_cache_clear(args: argparse.Namespace) -> int:
    print_json(clear_cache(older_than_hours=args.older_than_hours))
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    print_json(recent_runs(limit=args.limit))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print_json(telemetry_stats())
    return 0


def cmd_env_report(args: argparse.Namespace) -> int:
    print_json(environment_probe())
    return 0


def cmd_probe_workspace(args: argparse.Namespace) -> int:
    result = workspace_profile(
        args.workspace_root,
        refresh=args.refresh,
        require_trusted=not args.allow_untrusted,
    )
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_probe_status(args: argparse.Namespace) -> int:
    print_json(workspace_profile_status(args.workspace_root))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = run_lifecycle_verification(
        mode=args.mode,
        workspace_root=args.workspace_root,
        include_provider_smoke=args.include_provider_smoke,
        client_id=args.client,
        write_evidence=not args.no_evidence,
    )
    print_json(result)
    return 0 if result.get("ok") else 2


def cmd_evidence(args: argparse.Namespace) -> int:
    if args.latest:
        print_json(
            latest_evidence(kind=args.kind, successful_only=args.successful_only)
        )
    else:
        print_json(list_evidence(limit=args.limit))
    return 0


def cmd_lab(args: argparse.Namespace) -> int:
    result = run_lab_matrix(
        repeat=args.repeat,
        mode=args.mode,
        workspace_root=args.workspace_root,
        include_provider_smoke=args.include_provider_smoke,
        profile=args.profile,
    )
    print_json(result)
    return 0 if result.get("acceptance_met") else 2


def cmd_qualification(args: argparse.Namespace) -> int:
    if args.action == "definitions":
        print_json(qualification_definitions_status())
        return 0
    if args.action == "template":
        result = write_qualification_template(
            args.profile,
            args.output_dir,
            workspace_root=args.workspace_root,
        )
        print_json(result)
        return 0
    if args.action == "status":
        if args.latest:
            print_json(
                latest_qualification(
                    profile_id=args.profile,
                    qualified_only=args.qualified_only,
                )
            )
        else:
            print_json(list_qualifications(limit=args.limit))
        return 0
    if args.action == "client-receipt":
        try:
            facts = json.loads(args.facts_json) if args.facts_json else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--facts-json must be valid JSON: {exc}") from exc
        if not isinstance(facts, dict):
            raise SystemExit("--facts-json must decode to an object")
        print_json(
            record_client_receipt(
                client=args.client,
                client_version=args.client_version,
                facts=facts,
            )
        )
        return 0
    if args.action == "run":
        result = run_qualification(
            profile_id=args.profile,
            workspace_root=args.workspace_root,
            client_assertions_path=args.client_assertions,
            canary_results_path=args.canary_results,
            repeat=args.repeat,
            include_provider_smoke=not args.no_provider_smoke,
            client_id=args.client,
        )
        print_json(result)
        return 0 if result.get("ok") else 2
    raise AssertionError(f"Unhandled qualification action: {args.action}")


def cmd_extensions(args: argparse.Namespace) -> int:
    print_json(extension_status())
    return 0


def cmd_context7_onboarding(args: argparse.Namespace) -> int:
    print_json(context7_onboarding_plan())
    return 0


def cmd_enable_context7_env(args: argparse.Namespace) -> int:
    result = enable_context7_env_source(
        mode=args.mode,
        env_name=args.env_name,
        install_codex_mcp=args.install_codex_mcp,
        cache_ttl_hours=args.cache_ttl_hours,
        inject_docs=not args.no_inject_docs,
        max_libraries=args.max_libraries,
        max_chars=args.max_chars,
        fast=not args.no_fast,
        force_codex_mcp=args.force,
    )
    print_json(result)
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baldr-router")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "facade",
        help="Shared client facade with only setup, status, and run intents",
    )
    facade_sub = p.add_subparsers(dest="intent", required=True)

    f = facade_sub.add_parser("contract", help="Print the versioned facade contract")
    f.set_defaults(func=cmd_facade)

    f = facade_sub.add_parser("setup", help="Return the non-secret setup plan")
    f.add_argument("workspace_root", nargs="?")
    f.add_argument("--client", default="generic-mcp")
    f.add_argument(
        "--trust-workspace",
        action="store_true",
        help="Persistently trust this workspace after client/user consent",
    )
    f.add_argument(
        "--workspace-safety-mode",
        choices=["automatic", "worktree", "current", "non-git"],
    )
    f.add_argument("--execution-preset", choices=["fast", "balanced", "deep", "custom"])
    f.add_argument("--context-mode", choices=["auto", "on", "off"])
    f.add_argument(
        "--context7-policy", choices=["auto", "on", "off"], help=argparse.SUPPRESS
    )
    f.add_argument(
        "--role-profile",
        action="append",
        help="Assign role profiles as role=profile[,profile]",
    )
    f.add_argument("--team-mode", choices=["automatic", "configured"])
    f.add_argument(
        "--agent-override", action="append", help="Pin one stage as role=AgentRef"
    )
    f.add_argument("--clear-agent-overrides", action="store_true")
    f.add_argument(
        "--allow-non-git",
        action="store_true",
        help="Confirm reduced guarantees for a non-Git workspace",
    )
    f.add_argument(
        "--profile-definition-json",
        help="Create/update one execution profile through the setup intent",
    )
    f.set_defaults(func=cmd_facade)

    f = facade_sub.add_parser("status", help="Return a compact Baldr status report")
    f.add_argument("workspace_root", nargs="?")
    f.add_argument("--recent-limit", type=int, default=5)
    f.add_argument("--work-item-id")
    f.add_argument("--work-item-limit", type=int, default=100)
    f.add_argument("--include-archived", action="store_true")
    f.add_argument(
        "--workbench-only",
        action="store_true",
        help="Return only the durable workbench state without health diagnostics",
    )
    f.add_argument("--client", default="generic-mcp")
    f.set_defaults(func=cmd_facade)

    f = facade_sub.add_parser("run", help="Run the frozen orchestration workflow")
    f.add_argument("workspace_root")
    f.add_argument("task", nargs="?", default="")
    f.add_argument(
        "--work-item-action",
        choices=[
            "execute",
            "create",
            "draft",
            "create-item",
            "update",
            "update-item",
            "continue",
            "continue-item",
            "start",
            "start-item",
            "cancel",
            "cancel-item",
            "reconcile",
            "reconcile-item",
            "archive",
            "archive-item",
            "restore",
            "restore-item",
            "delete",
            "delete-item",
            "inspect-phase",
            "inspect-item-phase",
            "list-deliverables",
            "list-item-deliverables",
        ],
        default="execute",
    )
    f.add_argument("--work-item-id")
    f.add_argument(
        "--phase-stage",
        choices=["planning", "execution", "review"],
        help="Stage to inspect for inspect-item-phase",
    )
    f.add_argument("--phase-round", type=int, help="Zero-based phase round to inspect")
    f.add_argument(
        "--phase-run-ordinal",
        type=int,
        help="Durable attempt number; omit to inspect the latest matching attempt",
    )
    f.add_argument(
        "--phase-cursor", help="Opaque cursor returned by a prior phase page"
    )
    f.add_argument("--phase-page-size", type=int, default=20)
    f.add_argument(
        "--deliverable-cursor",
        help="Opaque cursor returned by a deliverable index page",
    )
    f.add_argument("--deliverable-page-size", type=int, default=20)
    f.add_argument("--title")
    f.add_argument(
        "--workspace-mode",
        choices=["automatic", "worktree", "current", "non-git"],
    )
    f.add_argument("--execution-preset", choices=["fast", "balanced", "deep", "custom"])
    f.add_argument("--context-mode", choices=["auto", "on", "off"])
    f.add_argument(
        "--context7-policy", choices=["auto", "on", "off"], help=argparse.SUPPRESS
    )
    f.add_argument(
        "--role-profile",
        action="append",
        help="Override item role profiles as role=profile[,profile]",
    )
    f.add_argument("--team-mode", choices=["automatic", "configured"])
    f.add_argument(
        "--agent-override", action="append", help="Pin one stage as role=AgentRef"
    )
    f.add_argument("--clear-agent-overrides", action="store_true")
    f.add_argument("--remember-workspace", action="store_true")
    f.add_argument("--allow-non-git", action="store_true")
    f.add_argument("--attachments-json", help="JSON array of attachment metadata")
    f.add_argument(
        "--item-config-json", help="JSON object with durable item execution metadata"
    )
    f.add_argument("--extra-context", default="")
    f.add_argument("--architect-provider")
    f.add_argument("--implementer-provider")
    f.add_argument("--reviewer-provider")
    f.add_argument("--max-rounds", type=int)
    f.add_argument("--context7-library", action="append")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--idempotency-key")
    f.add_argument("--resume-run-id")
    f.add_argument(
        "--reconciliation-action",
        choices=[
            "authorize_changes",
            "decline_changes",
            "resume_from_checkpoint",
            "accept_existing_changes",
            "discard_worktree",
            "inspect_shadow",
            "continue_from_shadow",
            "apply_shadow_changes",
            "discard_shadow",
            "mark_failed",
        ],
    )
    f.add_argument("--cancel", action="store_true")
    f.add_argument("--cancel-reason", default="Cancellation requested by client.")
    f.add_argument("--client", default="generic-mcp")
    f.set_defaults(func=cmd_facade)

    p = sub.add_parser(
        "workspace-status", help="Inspect the trusted-workspace policy for a path"
    )
    p.add_argument("workspace_root")
    p.add_argument("--access", choices=["read", "write"], default="read")
    p.set_defaults(func=cmd_workspace_status)

    p = sub.add_parser(
        "trust-workspace", help="Add one Git workspace to the persistent trust list"
    )
    p.add_argument("workspace_root")
    p.add_argument(
        "--force", action="store_true", help="Allow an intentional non-Git workspace"
    )
    p.set_defaults(func=cmd_workspace_trust)

    p = sub.add_parser(
        "untrust-workspace", help="Remove a workspace from the persistent trust list"
    )
    p.add_argument("workspace_root")
    p.set_defaults(func=cmd_workspace_untrust)

    p = sub.add_parser(
        "doctor",
        help="Check the core router, providers, extensions, Context7, telemetry, and an optional workspace path",
    )
    p.add_argument("workspace_root", nargs="?")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "env-report", help="Show runtime platform information, including WSL detection"
    )
    p.set_defaults(func=cmd_env_report)

    p = sub.add_parser(
        "probe-workspace",
        help="Build a bounded, trust-aware workspace profile from manifests and metadata",
    )
    p.add_argument("workspace_root")
    p.add_argument("--refresh", action="store_true")
    p.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="Diagnostic-only override; normal clients must profile only trusted workspaces",
    )
    p.set_defaults(func=cmd_probe_workspace)

    p = sub.add_parser("probe-status", help="Show cached workspace-profile status")
    p.add_argument("workspace_root")
    p.set_defaults(func=cmd_probe_status)

    p = sub.add_parser(
        "verify",
        help="Run deterministic install/execute/cancel/restart/update lifecycle verification",
    )
    p.add_argument("workspace_root", nargs="?")
    p.add_argument("--mode", choices=["quick", "full"], default="quick")
    p.add_argument("--include-provider-smoke", action="store_true")
    p.add_argument("--client", default="cli")
    p.add_argument("--no-evidence", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser(
        "evidence", help="List or inspect redacted verification evidence"
    )
    p.add_argument("--latest", action="store_true")
    p.add_argument("--kind", choices=["lifecycle", "lab", "workflow"])
    p.add_argument("--successful-only", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser(
        "lab",
        help="Repeat deterministic lifecycle verification and require consecutive passes",
    )
    p.add_argument("workspace_root", nargs="?")
    p.add_argument("--repeat", type=int)
    p.add_argument("--mode", choices=["quick", "full"], default="quick")
    p.add_argument("--profile")
    p.add_argument("--include-provider-smoke", action="store_true")
    p.set_defaults(func=cmd_lab)

    p = sub.add_parser(
        "qualification",
        help="Prepare, run, and inspect real-environment qualification receipts",
    )
    qualification_sub = p.add_subparsers(dest="action", required=True)

    q = qualification_sub.add_parser(
        "definitions", help="Show frozen qualification profiles and canaries"
    )
    q.set_defaults(func=cmd_qualification)

    q = qualification_sub.add_parser(
        "template", help="Write client-assertion and canary-result templates"
    )
    q.add_argument("--profile", required=True)
    q.add_argument("--output-dir", required=True)
    q.add_argument("--workspace-root")
    q.set_defaults(func=cmd_qualification)

    q = qualification_sub.add_parser(
        "client-receipt", help="Record redacted client/runtime facts"
    )
    q.add_argument("--client", required=True)
    q.add_argument("--client-version", default="")
    q.add_argument("--facts-json", default="{}")
    q.set_defaults(func=cmd_qualification)

    q = qualification_sub.add_parser(
        "run", help="Run the three-pass lab and evaluate real-client evidence"
    )
    q.add_argument("--profile", required=True)
    q.add_argument("--workspace-root")
    q.add_argument("--client-assertions")
    q.add_argument("--canary-results")
    q.add_argument("--repeat", type=int)
    q.add_argument("--client")
    q.add_argument("--no-provider-smoke", action="store_true")
    q.set_defaults(func=cmd_qualification)

    q = qualification_sub.add_parser(
        "status", help="List or inspect stored qualification receipts"
    )
    q.add_argument("--latest", action="store_true")
    q.add_argument("--profile")
    q.add_argument("--qualified-only", action="store_true")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=cmd_qualification)

    p = sub.add_parser("extensions", help="Show installed client-adapter extensions")
    p.set_defaults(func=cmd_extensions)

    p = sub.add_parser("mcp", help="Run the MCP server over stdio")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser(
        "set-provider", help="Set the default provider used by direct tasks"
    )
    p.add_argument("provider", help="Provider name, for example: codex")
    p.set_defaults(func=cmd_set_provider)

    p = sub.add_parser(
        "provider-status", help="Show implemented provider availability and auth status"
    )
    p.set_defaults(func=cmd_provider_status)

    p = sub.add_parser(
        "kiro-mcp-status",
        help="Explicitly diagnose Kiro MCP registry and local configuration",
    )
    p.set_defaults(func=cmd_kiro_mcp_status)

    p = sub.add_parser(
        "provider-models", help="List selectable models and variants for a provider"
    )
    p.add_argument("provider", nargs="?", default="codex")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_provider_models)

    p = sub.add_parser(
        "agent-catalog",
        help="List safe metadata for externally registered agents",
    )
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_agent_catalog)

    agent = sub.add_parser(
        "agent",
        help="Manage exact external agent versions in the local registry",
    )
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    p = agent_sub.add_parser("list", help="List registered external agents")
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_agent_catalog)

    p = agent_sub.add_parser(
        "discover",
        help="Discover external agent metadata without importing or executing agents",
    )
    p.add_argument(
        "--source",
        choices=["kiro", "manager", "file", "endpoint", "all"],
        default="kiro",
    )
    p.add_argument("--workspace", default=".")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--path", help="AgentSource v1 JSON file")
    p.add_argument("--endpoint", help="AgentSource v1 HTTP endpoint")
    p.add_argument("--expected-source-id", default="")
    p.add_argument("--authorization-env", default="")
    p.add_argument("--timeout-seconds", type=int, default=10)
    p.add_argument("--allow-insecure-loopback", action="store_true")
    p.set_defaults(func=cmd_agent_discover)

    p = agent_sub.add_parser(
        "sync",
        help="Preview or apply an idempotent source-to-registry catalog diff",
    )
    p.add_argument(
        "--source",
        choices=["kiro", "manager", "file", "endpoint", "all"],
        default="kiro",
    )
    p.add_argument("--workspace", default=".")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--path", help="AgentSource v1 JSON file")
    p.add_argument("--endpoint", help="AgentSource v1 HTTP endpoint")
    p.add_argument("--expected-source-id", default="")
    p.add_argument("--authorization-env", default="")
    p.add_argument("--timeout-seconds", type=int, default=10)
    p.add_argument("--allow-insecure-loopback", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument(
        "--missing-action",
        choices=["keep", "disable", "revoke"],
        default="keep",
        help="Lifecycle decision for previously managed agents missing from a complete source",
    )
    p.add_argument(
        "--confirm-revoke",
        default="",
        help="For revoke, repeat the exact source id shown by preview",
    )
    p.add_argument("--actor", default="local-operator")
    p.set_defaults(func=cmd_agent_sync)

    p = agent_sub.add_parser(
        "sync-status",
        help="Show safe source ownership and latest catalog reconciliation event",
    )
    p.set_defaults(func=cmd_agent_sync_status)

    p = agent_sub.add_parser("inspect", help="Inspect one exact local AgentRef")
    p.add_argument("reference")
    p.set_defaults(func=cmd_agent_inspect)

    p = agent_sub.add_parser(
        "publish",
        help="Publish a new immutable local agent version",
    )
    p.add_argument("reference")
    p.add_argument("--owner", required=True)
    p.add_argument("--transport", required=True)
    p.add_argument(
        "--target",
        action="append",
        help="Transport target key=value; repeat for multiple values",
    )
    p.add_argument("--capability", action="append", default=[])
    p.add_argument("--input-schema", default="baldr.Task/v1")
    p.add_argument("--output-schema", default="baldr.StructuredReport/v1")
    p.add_argument(
        "--effect-mode",
        choices=["read-only", "workspace-write", "external"],
        default="read-only",
    )
    p.add_argument("--supports-sessions", action="store_true")
    p.add_argument("--supports-cancellation", action="store_true")
    p.add_argument(
        "--digest",
        help="Optional declared sha256 digest; computed automatically when omitted",
    )
    p.set_defaults(func=cmd_agent_publish)

    for action, enabled in (("enable", True), ("disable", False)):
        p = agent_sub.add_parser(action, help=f"{action.title()} one exact AgentRef")
        p.add_argument("reference")
        p.set_defaults(func=cmd_agent_set_enabled, enabled=enabled)

    p = agent_sub.add_parser(
        "revoke",
        help="Irreversibly revoke an exact local AgentRef",
    )
    p.add_argument("reference")
    p.add_argument("--confirm-reference", required=True)
    p.set_defaults(func=cmd_agent_revoke)

    p = agent_sub.add_parser(
        "remove",
        help="Remove a disabled AgentRef when no active durable run uses it",
    )
    p.add_argument("reference")
    p.set_defaults(func=cmd_agent_remove)

    manager = sub.add_parser(
        "agent-manager",
        help="Run and administer the persistent HTTP Agent Manager",
    )
    manager_sub = manager.add_subparsers(dest="agent_manager_command", required=True)

    p = manager_sub.add_parser("configure", help="Configure the HTTP manager client")
    p.add_argument("--registry", default="manager")
    p.add_argument("--base-url", required=True)
    p.add_argument("--authorization-env", default="")
    p.add_argument("--allow-insecure-loopback", action="store_true")
    p.set_defaults(func=cmd_agent_manager_configure)

    p = manager_sub.add_parser("status", help="Check manager health and catalog")
    p.set_defaults(func=cmd_agent_manager_status)

    p = manager_sub.add_parser("audit", help="Read the authorized append-only audit log")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_agent_manager_audit)

    p = manager_sub.add_parser("metrics", help="Read bounded manager operational metrics")
    p.set_defaults(func=cmd_agent_manager_metrics)

    p = manager_sub.add_parser("serve", help="Start the persistent manager service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--database", default="")
    p.add_argument("--registry", default="manager")
    p.add_argument("--authorization-env", default="BALDR_AGENT_MANAGER_TOKEN")
    p.add_argument(
        "--policy",
        default="",
        help="RBAC policy JSON; when omitted the authorization env is a legacy admin token",
    )
    p.set_defaults(func=cmd_agent_manager_serve)

    p = manager_sub.add_parser(
        "init-policy",
        help="Create a secret-free RBAC policy containing environment references",
    )
    p.add_argument("output")
    p.add_argument("--registry", default="manager")
    p.add_argument("--principal-id", required=True)
    p.add_argument("--credential-env", required=True)
    p.add_argument(
        "--role",
        action="append",
        choices=["reader", "publisher", "operator", "auditor", "admin"],
    )
    p.add_argument("--tenant", action="append")
    p.add_argument("--owner-scope", action="append")
    p.set_defaults(func=cmd_agent_manager_init_policy)

    for command, function, help_text in (
        ("doctor", cmd_agent_manager_doctor, "Inspect local schema and durable health"),
        ("backup", cmd_agent_manager_backup, "Create a consistent SQLite backup"),
    ):
        p = manager_sub.add_parser(command, help=help_text)
        p.add_argument("--database", default="")
        p.add_argument("--registry", default="manager")
        if command == "backup":
            p.add_argument("--output", required=True)
        p.set_defaults(func=function)

    p = manager_sub.add_parser(
        "init-manifest",
        help="Create a versioned publication file for an externally hosted agent",
    )
    p.add_argument("output")
    p.add_argument("reference")
    p.add_argument("--owner", required=True)
    p.add_argument("--transport", required=True)
    p.add_argument("--target", action="append")
    p.add_argument("--capability", action="append", default=[])
    p.add_argument("--input-schema", default="baldr.Task/v1")
    p.add_argument("--output-schema", default="baldr.StructuredReport/v1")
    p.add_argument(
        "--effect-mode",
        choices=["read-only", "workspace-write", "external"],
        default="read-only",
    )
    p.add_argument("--supports-sessions", action="store_true")
    p.add_argument("--supports-cancellation", action="store_true")
    p.add_argument("--digest")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_agent_manager_init_manifest)

    p = manager_sub.add_parser(
        "validate-manifest",
        help="Validate a publication file and print only safe identity metadata",
    )
    p.add_argument("path")
    p.set_defaults(func=cmd_agent_manager_validate_manifest)

    p = manager_sub.add_parser(
        "publish-file",
        help="Publish an immutable agent version from a reviewed JSON file",
    )
    p.add_argument("path")
    p.set_defaults(func=cmd_agent_manager_publish_file)

    p = manager_sub.add_parser("publish", help="Publish one immutable manager version")
    p.add_argument("reference")
    p.add_argument("--owner", required=True)
    p.add_argument("--transport", required=True)
    p.add_argument("--target", action="append")
    p.add_argument("--capability", action="append", default=[])
    p.add_argument("--input-schema", default="baldr.Task/v1")
    p.add_argument("--output-schema", default="baldr.StructuredReport/v1")
    p.add_argument(
        "--effect-mode",
        choices=["read-only", "workspace-write", "external"],
        default="read-only",
    )
    p.add_argument("--supports-sessions", action="store_true")
    p.add_argument("--supports-cancellation", action="store_true")
    p.add_argument("--digest")
    p.set_defaults(func=cmd_agent_manager_publish)

    for action, enabled in (("enable", True), ("disable", False)):
        p = manager_sub.add_parser(
            action, help=f"{action.title()} one manager AgentRef"
        )
        p.add_argument("reference")
        p.set_defaults(func=cmd_agent_manager_set_enabled, enabled=enabled)

    p = manager_sub.add_parser("revoke", help="Irreversibly revoke one AgentRef")
    p.add_argument("reference")
    p.set_defaults(func=cmd_agent_manager_revoke)

    p = sub.add_parser(
        "workflow-status", help="Show roles, workflows, providers, and safety settings"
    )
    p.set_defaults(func=cmd_workflow_status)

    p = sub.add_parser("roles", help="List configured multi-agent roles")
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("workflows", help="List configured workflows")
    p.set_defaults(func=cmd_workflows)

    p = sub.add_parser(
        "set-role-provider",
        help="Set a role provider, e.g. architect=kiro-cli implementer=codex",
    )
    p.add_argument("role", choices=["architect", "implementer", "reviewer"])
    p.add_argument("provider", help="Provider name, for example: codex or kiro-cli")
    p.add_argument(
        "--agent", help="Optional provider-specific agent name, useful for kiro-cli"
    )
    p.add_argument(
        "--effort", help="Optional provider-specific effort, useful for kiro-cli"
    )
    p.set_defaults(func=cmd_set_role_provider)

    p = sub.add_parser(
        "enable-kiro-cli", help="Enable Kiro CLI as an optional provider"
    )
    p.add_argument("--command", default="kiro-cli")
    p.add_argument("--agent", default="baldr-worker")
    p.add_argument("--effort", default="high")
    p.add_argument("--api-key-env", default="KIRO_API_KEY")
    p.add_argument("--no-require-api-key", action="store_true")
    p.set_defaults(func=cmd_enable_kiro_cli)

    p = sub.add_parser("disable-kiro-cli", help="Disable Kiro CLI provider")
    p.set_defaults(func=cmd_disable_kiro_cli)

    p = sub.add_parser(
        "run-workflow", help="Run a multi-agent workflow through baldr-router"
    )
    p.add_argument("workspace_root")
    p.add_argument("task", nargs="?", default="")
    p.add_argument("--workflow", default="architect-implement-review")
    p.add_argument("--extra-context", default="")
    p.add_argument("--architect-provider")
    p.add_argument("--implementer-provider")
    p.add_argument("--reviewer-provider")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--context7-library", action="append")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--idempotency-key")
    p.add_argument("--resume-run-id")
    p.add_argument(
        "--reconciliation-action",
        choices=[
            "authorize_changes",
            "decline_changes",
            "resume_from_checkpoint",
            "accept_existing_changes",
            "discard_worktree",
            "inspect_shadow",
            "continue_from_shadow",
            "apply_shadow_changes",
            "discard_shadow",
            "mark_failed",
        ],
    )
    p.add_argument("--cancel", action="store_true")
    p.add_argument("--cancel-reason", default="Cancellation requested by client.")
    p.add_argument("--client", default="cli")
    p.set_defaults(func=cmd_run_workflow)

    p = sub.add_parser(
        "set-codex-runner", help="Set Codex runner: exec-json, app-server, or sdk"
    )
    p.add_argument("runner", choices=sorted(VALID_CODEX_RUNNERS))
    p.add_argument(
        "--session-scope", choices=["workspace", "task", "global"], default="workspace"
    )
    p.set_defaults(func=cmd_set_codex_runner)

    p = sub.add_parser(
        "setup-context7",
        help="Enable Context7 for the Codex provider and/or router prefetch cache",
    )
    p.add_argument(
        "--source", default="local-file", help="local-file or env:CONTEXT7_API_KEY"
    )
    p.add_argument(
        "--mode",
        default="hybrid",
        choices=sorted(VALID_CONTEXT7_MODES),
        help="codex-mcp, router-cache, hybrid, or off",
    )
    p.add_argument(
        "--install-codex-mcp",
        action="store_true",
        help="Add Context7 to ~/.codex/config.toml without storing the key there",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing [mcp_servers.context7] table",
    )
    p.add_argument("--cache-ttl-hours", type=int, default=48)
    p.add_argument("--max-libraries", type=int, default=3)
    p.add_argument("--max-chars", type=int, default=9000)
    p.add_argument("--no-inject-docs", action="store_true")
    p.add_argument(
        "--no-fast",
        action="store_true",
        help="Use Context7 LLM reranking instead of fast vector search",
    )
    p.set_defaults(func=cmd_setup_context7)

    p = sub.add_parser(
        "context7-onboarding", help="Print a non-secret Context7 setup decision tree"
    )
    p.set_defaults(func=cmd_context7_onboarding)

    p = sub.add_parser(
        "enable-context7-env",
        help="Enable Context7 using an existing env var without storing secrets",
    )
    p.add_argument("--env-name", default="CONTEXT7_API_KEY")
    p.add_argument(
        "--mode", default="hybrid", choices=sorted(VALID_CONTEXT7_MODES - {"off"})
    )
    p.add_argument(
        "--install-codex-mcp",
        action="store_true",
        help="Add Context7 to ~/.codex/config.toml without storing the key there",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing [mcp_servers.context7] table",
    )
    p.add_argument("--cache-ttl-hours", type=int, default=48)
    p.add_argument("--max-libraries", type=int, default=3)
    p.add_argument("--max-chars", type=int, default=9000)
    p.add_argument("--no-inject-docs", action="store_true")
    p.add_argument(
        "--no-fast",
        action="store_true",
        help="Use Context7 LLM reranking instead of fast vector search",
    )
    p.set_defaults(func=cmd_enable_context7_env)

    p = sub.add_parser("disable-context7", help="Disable Context7 in router config")
    p.add_argument("--remove-codex-mcp", action="store_true")
    p.set_defaults(func=cmd_disable_context7)

    p = sub.add_parser(
        "install-codex-context7-mcp",
        help="Only add Context7 MCP block to ~/.codex/config.toml",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install_codex_context7)

    p = sub.add_parser(
        "remove-codex-context7-mcp",
        help="Remove managed Context7 MCP block from ~/.codex/config.toml",
    )
    p.set_defaults(func=cmd_remove_codex_context7)

    p = sub.add_parser(
        "context7-lookup", help="Fetch Context7 docs through the router cache"
    )
    p.add_argument(
        "library",
        help="Library name or Context7 library id, e.g. react or /vercel/next.js",
    )
    p.add_argument("query", help="Specific docs query")
    p.add_argument(
        "--fast", action="store_true", help="Use Context7 fast mode for this lookup"
    )
    p.set_defaults(func=cmd_context7_lookup)

    p = sub.add_parser("context7-cache-status", help="Show Context7 cache status")
    p.set_defaults(func=cmd_context7_cache_status)

    p = sub.add_parser("context7-cache-clear", help="Clear Context7 cache")
    p.add_argument("--older-than-hours", type=int)
    p.set_defaults(func=cmd_context7_cache_clear)

    p = sub.add_parser("runs", help="Show recent provider runs from telemetry")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("stats", help="Show aggregated provider telemetry")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
