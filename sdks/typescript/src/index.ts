import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { createInterface } from "node:readline";

export const CONTRACT = "baldr-agent-execution";
export const VERSION = 1;
export const SDK_VERSION = "0.19.0";

const REF = /^[a-z0-9][a-z0-9._-]{0,95}:\/\/[a-z0-9][a-z0-9._-]{0,95}\/[a-z0-9][a-z0-9._-]{0,95}@[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const MOVING_VERSIONS = new Set(["latest", "current", "stable"]);

export type EffectMode = "read-only" | "workspace-write" | "external";
export type JsonObject = Record<string, unknown>;
export type AgentHandler = (
  request: AgentRequest,
  context: AgentContext,
) => JsonObject | Promise<JsonObject>;

export interface AgentOptions {
  ref: string;
  owner: string;
  capabilities: readonly string[];
  effectMode?: EffectMode;
  inputSchema?: string;
  outputSchema?: string;
}

export interface AgentManifest {
  ref: string;
  owner: string;
  transport: string;
  target: Record<string, string>;
  capabilities: string[];
  input_schema: string;
  output_schema: string;
  execution: {
    effect_mode: EffectMode;
    supports_sessions: boolean;
    supports_cancellation: boolean;
  };
  digest: string;
}

export class ContractError extends Error {
  override name = "ContractError";
}

function object(value: unknown, field: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError(`${field} must be an object.`);
  }
  return value as JsonObject;
}

function text(value: unknown, field: string, limit: number, required = true): string {
  if (typeof value !== "string") {
    throw new ContractError(`${field} must be a string.`);
  }
  const result = value.trim();
  if (required && result.length === 0) {
    throw new ContractError(`${field} must not be empty.`);
  }
  if (result.length > limit) {
    throw new ContractError(`${field} exceeds ${limit} characters.`);
  }
  return result;
}

function sorted(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sorted);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as JsonObject)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sorted(item)]),
    );
  }
  return value;
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sorted(value));
}

export function canonicalDigest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}

export function validateRef(value: unknown): string {
  const result = text(value, "agent.ref", 320);
  const version = result.slice(result.lastIndexOf("@") + 1).toLowerCase();
  if (!REF.test(result) || MOVING_VERSIONS.has(version)) {
    throw new ContractError("agent.ref must be an exact immutable AgentRef.");
  }
  return result;
}

export function validateDigest(value: unknown, field = "agent.digest"): string {
  const result = text(value, field, 71);
  if (!DIGEST.test(result)) {
    throw new ContractError(`${field} must use sha256:<64 lowercase hex>.`);
  }
  return result;
}

export function validateIdentifier(value: unknown, field: string): string {
  const result = text(value, field, 160);
  if (!IDENTIFIER.test(result)) {
    throw new ContractError(`${field} contains unsupported characters.`);
  }
  return result;
}

export function parseMessage(value: unknown): JsonObject {
  const message = object(value, "Execution message");
  if (message.contract !== CONTRACT || message.version !== VERSION) {
    throw new ContractError(`Expected ${CONTRACT} v${VERSION}.`);
  }
  text(message.kind, "kind", 64);
  validateIdentifier(message.request_id, "request_id");
  if (message.kind !== "health-request" && message.kind !== "health-response") {
    validateIdentifier(message.job_id, "job_id");
  }
  return message;
}

async function readStdinLine(): Promise<string> {
  const source = createInterface({ input: process.stdin, crlfDelay: Infinity });
  try {
    for await (const line of source) {
      if (line.length > 2_100_000) {
        throw new ContractError("Execution request exceeds the stdin limit.");
      }
      return line;
    }
  } finally {
    source.close();
  }
  throw new ContractError("Expected one execution request on stdin.");
}

export class AgentRequest {
  readonly requestId: string;
  readonly jobId: string;
  readonly idempotencyKey: string;
  readonly agentRef: string;
  readonly agentDigest: string;
  readonly task: string;
  readonly workflow: string;
  readonly stepName: string;
  readonly reportKind: string;
  readonly profileName: string;
  readonly workspaceRoot: string | null;
  readonly effectMode: "read-only" | "workspace-write";
  readonly requestedCapabilities: readonly string[];
  readonly timeoutSeconds: number;
  readonly raw: JsonObject;

  constructor(messageValue: JsonObject) {
    const message = parseMessage(messageValue);
    if (message.kind !== "invoke") {
      throw new ContractError("AgentRequest requires an invoke message.");
    }
    const identity = object(message.agent, "invoke.agent");
    const invocation = object(message.invocation, "invoke.invocation");
    const workspace = object(invocation.workspace, "invocation.workspace");
    const capabilities = invocation.requested_capabilities;
    if (!Array.isArray(capabilities) || capabilities.some((item) => typeof item !== "string")) {
      throw new ContractError("requested_capabilities must be a string array.");
    }
    const effectMode = workspace.effect_mode;
    if (effectMode !== "read-only" && effectMode !== "workspace-write") {
      throw new ContractError("workspace.effect_mode is invalid.");
    }
    const timeout = invocation.timeout_seconds;
    if (!Number.isInteger(timeout) || (timeout as number) < 1 || (timeout as number) > 86400) {
      throw new ContractError("timeout_seconds is invalid.");
    }
    this.requestId = validateIdentifier(message.request_id, "request_id");
    this.jobId = validateIdentifier(message.job_id, "job_id");
    this.idempotencyKey = text(message.idempotency_key, "idempotency_key", 320);
    this.agentRef = validateRef(identity.ref);
    this.agentDigest = validateDigest(identity.digest);
    this.task = text(invocation.task ?? "", "task", 2_000_000, false);
    this.workflow = text(invocation.workflow ?? "", "workflow", 160, false);
    this.stepName = text(invocation.step_name ?? "", "step_name", 160, false);
    this.reportKind = text(invocation.report_kind ?? "", "report_kind", 160, false);
    this.profileName = text(invocation.profile_name ?? "", "profile_name", 160, false);
    this.workspaceRoot = workspace.root === null ? null : text(workspace.root, "workspace.root", 4096);
    this.effectMode = effectMode;
    this.requestedCapabilities = capabilities;
    this.timeoutSeconds = timeout as number;
    this.raw = message;
  }
}

export class AgentContext {
  private sequence = 0;
  private stopped = false;

  constructor(readonly request: AgentRequest) {}

  get cancelled(): boolean {
    return this.stopped;
  }

  cancel(): void {
    this.stopped = true;
  }

  raiseIfCancelled(): void {
    if (this.stopped) {
      throw new ContractError("Agent invocation was cancelled.");
    }
  }

  emit(categoryValue: string, messageValue = ""): void {
    const category = text(categoryValue, "event.category", 160);
    const message = text(messageValue, "event.message", 4096, false);
    this.sequence += 1;
    process.stdout.write(`${canonicalJson({
      contract: CONTRACT,
      version: VERSION,
      kind: "event",
      request_id: this.request.requestId,
      job_id: this.request.jobId,
      sequence: this.sequence,
      category,
      message,
    })}\n`);
  }
}

export class Agent {
  readonly ref: string;
  readonly owner: string;
  readonly capabilities: readonly string[];
  readonly effectMode: EffectMode;
  readonly inputSchema: string;
  readonly outputSchema: string;
  private handler: AgentHandler | null = null;

  constructor(options: AgentOptions) {
    this.ref = validateRef(options.ref);
    this.owner = text(options.owner, "agent.owner", 160);
    const capabilities = [...options.capabilities].map((item) => text(item, "capability", 160));
    if (capabilities.length === 0 || capabilities.length > 64 || new Set(capabilities).size !== capabilities.length) {
      throw new ContractError("Agent capabilities must be unique bounded strings.");
    }
    this.effectMode = options.effectMode ?? "read-only";
    if (!(["read-only", "workspace-write", "external"] as const).includes(this.effectMode)) {
      throw new ContractError("Agent effectMode is invalid.");
    }
    if (this.effectMode === "workspace-write" && !capabilities.includes("workspace.write")) {
      throw new ContractError("Writable agents must declare workspace.write.");
    }
    this.capabilities = capabilities;
    this.inputSchema = options.inputSchema ?? "baldr.AgentExecution/v1";
    this.outputSchema = options.outputSchema ?? "baldr.AgentResult/v1";
  }

  invoke(handler: AgentHandler): AgentHandler {
    if (this.handler !== null) {
      throw new ContractError("An Agent can register only one invoke handler.");
    }
    this.handler = handler;
    return handler;
  }

  manifest(
    transport: string,
    target: Record<string, string>,
    supportsSessions = false,
    supportsCancellation = true,
  ): AgentManifest {
    const payload = {
      ref: this.ref,
      owner: this.owner,
      transport: text(transport, "transport", 160).toLowerCase(),
      target: Object.fromEntries(Object.entries(target).sort(([left], [right]) => left.localeCompare(right))),
      capabilities: [...this.capabilities],
      input_schema: this.inputSchema,
      output_schema: this.outputSchema,
      execution: {
        effect_mode: this.effectMode,
        supports_sessions: supportsSessions,
        supports_cancellation: supportsCancellation,
      },
    };
    return { ...payload, digest: canonicalDigest(payload) };
  }

  localProcessManifest(command: string, args: readonly string[], artifactPath: string, timeoutSeconds = 1800): AgentManifest {
    const artifact = resolve(artifactPath);
    const artifactDigest = `sha256:${createHash("sha256").update(readFileSync(artifact)).digest("hex")}`;
    return this.manifest("local-process", {
      command,
      arguments_json: JSON.stringify(args),
      artifact_path: artifact,
      artifact_digest: artifactDigest,
      protocol: "stdio-jsonl-v1",
      timeout_seconds: String(Math.max(1, Math.min(timeoutSeconds, 86400))),
    });
  }

  writeManifest(path: string, manifest: AgentManifest): void {
    writeFileSync(resolve(path), `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  }

  async serveStdio(): Promise<number> {
    const line = await readStdinLine();
    const message = parseMessage(JSON.parse(line) as unknown);
    if (message.kind === "health-request") {
      process.stdout.write(`${canonicalJson({
        contract: CONTRACT,
        version: VERSION,
        kind: "health-response",
        request_id: message.request_id,
        status: "ok",
        runner_version: `agent-sdk-${SDK_VERSION}`,
        protocols: [1],
      })}\n`);
      return 0;
    }
    const request = new AgentRequest(message);
    if (request.agentRef !== this.ref) {
      throw new ContractError("The invocation AgentRef does not match this agent.");
    }
    if (request.effectMode === "workspace-write" && this.effectMode !== "workspace-write") {
      throw new ContractError("This agent is not declared for workspace writes.");
    }
    if (!request.requestedCapabilities.every((item) => this.capabilities.includes(item))) {
      throw new ContractError("The invocation requests undeclared capabilities.");
    }
    if (this.handler === null) {
      throw new ContractError("No invoke handler is registered.");
    }
    const context = new AgentContext(request);
    const cancel = (): void => context.cancel();
    process.once("SIGTERM", cancel);
    process.once("SIGINT", cancel);
    let state: "succeeded" | "failed" | "cancelled" = "succeeded";
    let result: JsonObject;
    let error: JsonObject | null = null;
    try {
      result = await this.handler(request, context);
      if (context.cancelled) {
        state = "cancelled";
        result = { ok: false, status: "cancelled" };
        error = { code: "agent_cancelled", message: "Agent invocation was cancelled.", retryable: true };
      } else if (result.ok === false) {
        state = "failed";
      }
    } catch (cause) {
      state = context.cancelled ? "cancelled" : "failed";
      result = { ok: false, status: state };
      error = {
        code: context.cancelled ? "agent_cancelled" : "agent_handler_failed",
        message: cause instanceof Error ? cause.message.slice(0, 4096) : String(cause).slice(0, 4096),
        retryable: context.cancelled,
      };
    } finally {
      process.off("SIGTERM", cancel);
      process.off("SIGINT", cancel);
    }
    process.stdout.write(`${canonicalJson({
      contract: CONTRACT,
      version: VERSION,
      kind: "result",
      request_id: request.requestId,
      job_id: request.jobId,
      state,
      agent: { ref: this.ref, digest: request.agentDigest },
      result,
      error,
    })}\n`);
    return state === "succeeded" ? 0 : 1;
  }
}
