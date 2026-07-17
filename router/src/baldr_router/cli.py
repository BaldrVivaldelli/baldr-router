from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .codex import codex_model_catalog
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
from .secrets import prompt_context7_key_and_store
from .status import doctor
from .telemetry import recent_runs, telemetry_stats
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
            context7_policy=getattr(args, "context_mode", None) or getattr(args, "context7_policy", None),
            role_profiles=_parse_role_profiles(getattr(args, "role_profile", None)),
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
            cancel_reason=getattr(args, "cancel_reason", "Cancellation requested by client."),
            work_item_action=getattr(args, "work_item_action", "execute"),
            work_item_id=getattr(args, "work_item_id", None),
            title=getattr(args, "title", None),
            workspace_mode=getattr(args, "workspace_mode", None),
            execution_preset=getattr(args, "execution_preset", None),
            context7_policy=getattr(args, "context_mode", None) or getattr(args, "context7_policy", None),
            role_profiles=_parse_role_profiles(getattr(args, "role_profile", None)),
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
        print_json(latest_evidence(kind=args.kind, successful_only=args.successful_only))
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
    f.add_argument("--context7-policy", choices=["auto", "on", "off"], help=argparse.SUPPRESS)
    f.add_argument("--role-profile", action="append", help="Assign role profiles as role=profile[,profile]")
    f.add_argument("--allow-non-git", action="store_true", help="Confirm reduced guarantees for a non-Git workspace")
    f.add_argument("--profile-definition-json", help="Create/update one execution profile through the setup intent")
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
            "execute", "create", "draft", "create-item", "update", "update-item",
            "continue", "continue-item",
            "start", "start-item", "cancel", "cancel-item", "reconcile",
            "reconcile-item", "archive", "archive-item", "restore", "restore-item",
            "delete", "delete-item",
            "inspect-phase", "inspect-item-phase",
            "list-deliverables", "list-item-deliverables",
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
    f.add_argument("--phase-cursor", help="Opaque cursor returned by a prior phase page")
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
    f.add_argument("--context7-policy", choices=["auto", "on", "off"], help=argparse.SUPPRESS)
    f.add_argument("--role-profile", action="append", help="Override item role profiles as role=profile[,profile]")
    f.add_argument("--remember-workspace", action="store_true")
    f.add_argument("--allow-non-git", action="store_true")
    f.add_argument("--attachments-json", help="JSON array of attachment metadata")
    f.add_argument("--item-config-json", help="JSON object with durable item execution metadata")
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

    p = sub.add_parser("workspace-status", help="Inspect the trusted-workspace policy for a path")
    p.add_argument("workspace_root")
    p.add_argument("--access", choices=["read", "write"], default="read")
    p.set_defaults(func=cmd_workspace_status)

    p = sub.add_parser("trust-workspace", help="Add one Git workspace to the persistent trust list")
    p.add_argument("workspace_root")
    p.add_argument("--force", action="store_true", help="Allow an intentional non-Git workspace")
    p.set_defaults(func=cmd_workspace_trust)

    p = sub.add_parser("untrust-workspace", help="Remove a workspace from the persistent trust list")
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

    p = sub.add_parser("evidence", help="List or inspect redacted verification evidence")
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
        "provider-models", help="List selectable models and variants for a provider"
    )
    p.add_argument("provider", nargs="?", default="codex")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_provider_models)

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
