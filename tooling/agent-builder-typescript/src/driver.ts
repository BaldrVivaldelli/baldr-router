import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import {
  dirname,
  isAbsolute,
  join,
  posix,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const DRIVER_CONTRACT = "baldr-builder-driver";
const PROTOCOL_VERSION = 1;
const DRIVER_ID = "baldr.typescript";
const DRIVER_VERSION = "0.20.0";
const TARGET_PROTOCOL = "agent-execution-v1";
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const IGNORED = new Set([
  ".git",
  ".hg",
  ".svn",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  "__pycache__",
  "node_modules",
]);

type JsonObject = Record<string, unknown>;

interface DriverRequest extends JsonObject {
  contract: string;
  version: number;
  kind: "describe-request" | "test-request" | "build-request";
  request_id: string;
  source_root?: string;
  source_digest?: string;
  source_paths?: string[];
  project_name?: string;
  project_version?: string;
  entrypoint?: string;
  test_command?: string[];
  timeout_seconds?: number;
  target?: string;
  network?: string;
  reproducible?: boolean;
  output_root?: string | null;
}

interface InventoryEntry {
  name: string;
  content: Buffer;
}

function sha256(value: Buffer | string): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function boundedText(value: unknown, field: string, limit: number): string {
  if (
    typeof value !== "string" ||
    value.trim().length === 0 ||
    value.length > limit
  ) {
    throw new Error(`${field} must be a bounded non-empty string.`);
  }
  return value.trim();
}

function safeRelative(value: string, field: string): string {
  const normalized = value.replaceAll("\\", "/");
  if (
    isAbsolute(value) ||
    normalized.split("/").includes("..") ||
    normalized.startsWith("/")
  ) {
    throw new Error(`${field} must remain inside the project.`);
  }
  return normalized;
}

function inside(root: string, candidate: string): boolean {
  const value = relative(root, candidate);
  return value === "" || (!value.startsWith(`..${sep}`) && value !== "..");
}

function collectPath(
  root: string,
  requested: string,
  entries: Map<string, Buffer>,
): void {
  const relativePath = safeRelative(requested, "source_paths");
  const selected = resolve(root, relativePath);
  if (!inside(root, selected)) {
    throw new Error("Configured source escapes the project.");
  }
  const info = lstatSync(selected, { throwIfNoEntry: false });
  if (info === undefined || info.isSymbolicLink()) {
    throw new Error(`Configured source is unavailable: ${relativePath}.`);
  }
  if (info.isFile()) {
    if (!relativePath.endsWith(".pyc")) {
      entries.set(relativePath, readFileSync(selected));
    }
    return;
  }
  if (!info.isDirectory()) {
    return;
  }
  const children = readdirSync(selected, { withFileTypes: true }).sort((left, right) =>
    left.name.localeCompare(right.name),
  );
  for (const child of children) {
    if (IGNORED.has(child.name) || child.isSymbolicLink()) {
      continue;
    }
    collectPath(root, posix.join(relativePath, child.name), entries);
  }
}

function inventory(rootValue: string, sourcePaths: readonly string[]): InventoryEntry[] {
  const root = realpathSync(resolve(rootValue));
  const entries = new Map<string, Buffer>();
  for (const sourcePath of [...new Set(sourcePaths)].sort()) {
    collectPath(root, sourcePath, entries);
  }
  let total = 0;
  return [...entries.entries()]
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
    .map(([name, content]) => {
      total += content.length;
      if (entries.size > 100_000 || total > 2 * 1024 ** 3) {
        throw new Error("Project source inventory exceeds Builder limits.");
      }
      return { name, content };
    });
}

function inventoryDigest(entries: readonly InventoryEntry[]): string {
  const value = entries.map(({ name, content }) => [
    name,
    sha256(content),
    content.length,
  ]);
  return sha256(JSON.stringify(value));
}

function sdkSourcePath(): string {
  const compiled = fileURLToPath(import.meta.resolve("@baldr/agent-sdk"));
  return resolve(dirname(compiled), "../src/index.ts");
}

function driverDigest(): string {
  const digest = createHash("sha256");
  const inputs = [
    ["driver.js", fileURLToPath(import.meta.url)],
    ["sdk.ts", sdkSourcePath()],
  ] as const;
  for (const [name, path] of inputs) {
    digest.update(name);
    digest.update(createHash("sha256").update(readFileSync(path)).digest());
  }
  digest.update(ts.version);
  return `sha256:${digest.digest("hex")}`;
}

export function descriptor(): JsonObject {
  return {
    id: DRIVER_ID,
    version: DRIVER_VERSION,
    digest: driverDigest(),
    language: "typescript",
    operations: ["test", "build"],
    targets: [TARGET_PROTOCOL],
  };
}

function transpile(source: string, fileName: string): string {
  const result = ts.transpileModule(source, {
    fileName,
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      strict: true,
      esModuleInterop: true,
      sourceMap: false,
      inlineSourceMap: false,
    },
  });
  const errors = (result.diagnostics ?? []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  if (errors.length > 0) {
    throw new Error(
      ts.formatDiagnosticsWithColorAndContext(errors, {
        getCanonicalFileName: (name) => name,
        getCurrentDirectory: () => process.cwd(),
        getNewLine: () => "\n",
      }),
    );
  }
  return result.outputText.trimEnd();
}

function validateImports(
  code: string,
  moduleId: string,
  moduleIds: Set<string>,
): void {
  const pattern = /require\(["']([^"']+)["']\)/gu;
  for (const match of code.matchAll(pattern)) {
    const specifier = match[1] as string;
    if (specifier === "@baldr/agent-sdk" || specifier.startsWith("node:")) {
      continue;
    }
    if (!specifier.startsWith(".")) {
      throw new Error(`Unsupported external dependency ${specifier} in ${moduleId}.`);
    }
    const base = posix.normalize(posix.join(posix.dirname(moduleId), specifier));
    const candidates = [base, `${base}.js`, posix.join(base, "index.js")];
    if (!candidates.some((candidate) => moduleIds.has(candidate))) {
      throw new Error(`Unresolved relative dependency ${specifier} in ${moduleId}.`);
    }
  }
}

function bundle(entries: readonly InventoryEntry[], entrypointValue: string): string {
  const entrypoint = safeRelative(entrypointValue, "entrypoint");
  const modules = new Map<string, string>();
  modules.set(
    "@baldr/agent-sdk",
    transpile(
      readFileSync(sdkSourcePath(), "utf8"),
      "@baldr/agent-sdk/index.ts",
    ),
  );
  for (const entry of entries) {
    if (!entry.name.endsWith(".ts") || entry.name.endsWith(".d.ts")) {
      continue;
    }
    const moduleId = entry.name.replace(/\.ts$/u, ".js");
    modules.set(
      moduleId,
      transpile(entry.content.toString("utf8"), entry.name),
    );
  }
  const entryId = entrypoint.replace(/\.ts$/u, ".js");
  if (!modules.has(entryId)) {
    throw new Error("TypeScript entrypoint is not present in configured sources.");
  }
  const moduleIds = new Set(modules.keys());
  for (const [moduleId, code] of modules) {
    validateImports(code, moduleId, moduleIds);
  }
  const factories = [...modules.entries()]
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
    .map(
      ([moduleId, code]) =>
        `__factories[${JSON.stringify(moduleId)}] = function(module, exports, require, __filename, __dirname) {\n${code}\n};`,
    )
    .join("\n");
  return `#!/usr/bin/env node
"use strict";
const __nativeRequire = require;
const __factories = Object.create(null);
${factories}
const __cache = Object.create(null);
function __normalize(value) {
  const output = [];
  for (const part of value.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") output.pop(); else output.push(part);
  }
  return output.join("/");
}
function __resolve(from, specifier) {
  if (specifier === "@baldr/agent-sdk") return specifier;
  if (specifier.startsWith("node:")) return null;
  if (!specifier.startsWith(".")) throw new Error("Unsupported external dependency: " + specifier);
  const base = __normalize(from.slice(0, from.lastIndexOf("/") + 1) + specifier);
  for (const candidate of [base, base + ".js", base + "/index.js"]) {
    if (__factories[candidate]) return candidate;
  }
  throw new Error("Bundled module was not found: " + specifier);
}
function __load(id) {
  if (__cache[id]) return __cache[id].exports;
  const factory = __factories[id];
  if (!factory) throw new Error("Bundled module was not found: " + id);
  const module = { exports: {} };
  __cache[id] = module;
  const localRequire = (specifier) => {
    const resolved = __resolve(id, specifier);
    return resolved === null ? __nativeRequire(specifier) : __load(resolved);
  };
  factory(module, module.exports, localRequire, id, id.slice(0, id.lastIndexOf("/")));
  return module.exports;
}
Promise.resolve(__load(${JSON.stringify(entryId)}).main())
  .then((code) => { if (Number.isInteger(code)) process.exitCode = code; })
  .catch((error) => { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; });
`;
}

function validateRequest(value: unknown): DriverRequest {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Driver request must be an object.");
  }
  const request = value as DriverRequest;
  if (
    request.contract !== DRIVER_CONTRACT ||
    request.version !== PROTOCOL_VERSION
  ) {
    throw new Error("Unsupported Builder Driver contract.");
  }
  boundedText(request.request_id, "request_id", 160);
  if (
    !["describe-request", "test-request", "build-request"].includes(request.kind)
  ) {
    throw new Error("Unsupported driver request kind.");
  }
  if (request.kind !== "describe-request") {
    boundedText(request.source_root, "source_root", 4096);
    boundedText(request.project_name, "project_name", 96);
    boundedText(request.project_version, "project_version", 64);
    boundedText(request.entrypoint, "entrypoint", 320);
    if (!DIGEST.test(request.source_digest ?? "")) {
      throw new Error("source_digest is invalid.");
    }
    if (!Array.isArray(request.source_paths) || request.source_paths.length === 0) {
      throw new Error("source_paths must be a non-empty array.");
    }
    if (!Array.isArray(request.test_command) || request.test_command.length === 0) {
      throw new Error("test_command must be a non-empty array.");
    }
    if (
      request.source_paths.length > 256 ||
      new Set(request.source_paths).size !== request.source_paths.length ||
      request.source_paths.some(
        (item) => typeof item !== "string" || item.length === 0 || item.length > 512,
      )
    ) {
      throw new Error("source_paths must be a bounded unique string array.");
    }
    if (
      request.test_command.length > 128 ||
      request.test_command.some(
        (item) => typeof item !== "string" || item.length === 0 || item.length > 4096,
      )
    ) {
      throw new Error("test_command must be a bounded string array.");
    }
    if (
      request.target !== TARGET_PROTOCOL ||
      request.network !== "inherit" ||
      request.reproducible !== true
    ) {
      throw new Error("The requested execution policy is unsupported.");
    }
    if (
      !Number.isInteger(request.timeout_seconds) ||
      (request.timeout_seconds as number) < 1 ||
      (request.timeout_seconds as number) > 86_400
    ) {
      throw new Error("timeout_seconds must be between 1 and 86400.");
    }
  }
  return request;
}

function operationResult(
  requestId: string,
  operation: "test" | "build",
  values: JsonObject,
): JsonObject {
  return {
    contract: DRIVER_CONTRACT,
    version: PROTOCOL_VERSION,
    kind: "operation-result",
    request_id: requestId,
    operation,
    status: values.status,
    driver: descriptor(),
    tests: values.tests ?? null,
    artifact: values.artifact ?? null,
    metadata: values.metadata ?? {},
    error: values.error ?? null,
  };
}

function buildArtifact(request: DriverRequest, outputRoot: string): JsonObject {
  const sourceRoot = realpathSync(resolve(request.source_root as string));
  const entries = inventory(sourceRoot, request.source_paths as string[]);
  const actualDigest = inventoryDigest(entries);
  if (actualDigest !== request.source_digest) {
    throw new Error("Workspace content does not match source_digest.");
  }
  const content = bundle(entries, request.entrypoint as string);
  mkdirSync(outputRoot, { recursive: true, mode: 0o700 });
  const artifactName = `${request.project_name}-${request.project_version}.cjs`;
  const artifactPath = resolve(outputRoot, artifactName);
  writeFileSync(artifactPath, content, { encoding: "utf8", mode: 0o644 });
  const digest = sha256(readFileSync(artifactPath));
  const metadata = {
    contract: "baldr-agent-build",
    version: 1,
    project: request.project_name,
    agent_version: request.project_version,
    language: "typescript",
    entrypoint: request.entrypoint,
    builder_driver: DRIVER_ID,
    driver_version: DRIVER_VERSION,
    typescript_version: ts.version,
    source_digest: actualDigest,
  };
  writeFileSync(
    resolve(outputRoot, "build.json"),
    `${JSON.stringify(
      {
        contract: "baldr-agent-build-result",
        version: 1,
        artifact: artifactName,
        artifact_digest: digest,
        metadata,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  return {
    artifact: {
      digest,
      media_type: "application/vnd.baldr.agent.node-cjs",
      size: statSync(artifactPath).size,
      launcher: "node-commonjs",
      path: artifactPath,
      uri: null,
    },
    metadata,
  };
}

function execute(
  command: readonly string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; timeout: number },
): Promise<{ status: number; stdout: string; stderr: string }> {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command[0] as string, command.slice(1), {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const append = (current: string, chunk: Buffer): string =>
      `${current}${chunk.toString("utf8")}`.slice(-2_000_000);
    child.stdout.on("data", (chunk: Buffer) => {
      stdout = append(stdout, chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr = append(stderr, chunk);
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, options.timeout);
    child.once("error", (error) => {
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.once("close", (code, signal) => {
      clearTimeout(timer);
      if (timedOut) {
        rejectPromise(new Error(`Agent tests exceeded ${options.timeout}ms.`));
        return;
      }
      if (code === null) {
        rejectPromise(new Error(`Agent tests ended by signal ${signal ?? "unknown"}.`));
        return;
      }
      resolvePromise({ status: code, stdout, stderr });
    });
  });
}

async function runTests(request: DriverRequest): Promise<JsonObject> {
  const temporary = mkdtempSync(join(tmpdir(), "baldr-typescript-test-"));
  try {
    const built = buildArtifact(request, temporary);
    const artifact = built.artifact as JsonObject;
    const command = (request.test_command as string[]).map((item) => {
      if (item === "{node}") return process.execPath;
      if (item === "{artifact}") return String(artifact.path);
      return item;
    });
    const processResult = await execute(command, {
      cwd: resolve(request.source_root as string),
      env: {
        ...process.env,
        BALDR_AGENT_ARTIFACT: String(artifact.path),
      },
      timeout: Math.max(1_000, (request.timeout_seconds as number) * 1_000 - 1_000),
    });
    if (processResult.status !== 0) {
      const detail = `${processResult.stdout ?? ""}${processResult.stderr ?? ""}`
        .trim()
        .slice(-2000);
      throw new Error(
        `Agent tests failed with exit code ${processResult.status}. ${detail}`.trim(),
      );
    }
    return {
      status: "passed",
      exit_code: processResult.status,
      command,
    };
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

export async function handle(value: unknown): Promise<JsonObject> {
  const request = validateRequest(value);
  if (request.kind === "describe-request") {
    return {
      contract: DRIVER_CONTRACT,
      version: PROTOCOL_VERSION,
      kind: "describe-response",
      request_id: request.request_id,
      driver: descriptor(),
    };
  }
  const operation = request.kind === "test-request" ? "test" : "build";
  try {
    if (operation === "test") {
      return operationResult(request.request_id, operation, {
        status: "succeeded",
        tests: await runTests(request),
      });
    }
    const outputRoot = request.output_root
      ? resolve(request.output_root)
      : resolve(request.source_root as string, "dist");
    return operationResult(request.request_id, operation, {
      status: "succeeded",
      ...buildArtifact(request, outputRoot),
    });
  } catch (error) {
    return operationResult(request.request_id, operation, {
      status: "failed",
      error: {
        code: "driver_operation_failed",
        message:
          error instanceof Error
            ? error.message.slice(0, 4096)
            : String(error).slice(0, 4096),
        retryable: false,
      },
    });
  }
}

export async function main(): Promise<void> {
  try {
    const lines = readFileSync(0, "utf8")
      .split(/\r?\n/u)
      .filter((line) => line.trim().length > 0);
    if (lines.length !== 1) {
      throw new Error("The driver expects exactly one JSONL request.");
    }
    process.stdout.write(
      `${JSON.stringify(await handle(JSON.parse(lines[0] as string)))}\n`,
    );
  } catch (error) {
    process.stderr.write(
      `${error instanceof Error ? error.message : String(error)}\n`,
    );
    process.exitCode = 2;
  }
}

if (
  process.argv[1] !== undefined &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  void main();
}
