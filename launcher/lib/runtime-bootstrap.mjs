import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

export const VERSION = '0.20.0';
const MANIFEST_NAME = 'runtime.json';
const DEFAULT_KEEP_VERSIONS = 2;

export function truthy(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').toLowerCase());
}

export function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

export function decodeOutput(buffer) {
  if (!buffer) return '';
  const bytes = Buffer.isBuffer(buffer) ? buffer : Buffer.from(String(buffer));
  let zeroCount = 0;
  for (const b of bytes) if (b === 0) zeroCount += 1;
  const decoded = zeroCount > Math.max(2, bytes.length / 8)
    ? bytes.toString('utf16le')
    : bytes.toString('utf8');
  return decoded.replace(/^\uFEFF/, '').replace(/\u0000/g, '').trim();
}

export function runCapture(command, args, options = {}) {
  const result = spawnSync(command, args, {
    windowsHide: true,
    encoding: 'buffer',
    env: process.env,
    ...options,
  });
  return {
    status: result.status,
    signal: result.signal,
    error: result.error ? String(result.error.message || result.error) : undefined,
    stdout: decodeOutput(result.stdout),
    stderr: decodeOutput(result.stderr),
  };
}

function firstLine(text) {
  return String(text || '').split(/\r?\n/).map((line) => line.trim()).find(Boolean) || '';
}

export function sha256File(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return '';
  const hash = createHash('sha256');
  hash.update(fs.readFileSync(filePath));
  return hash.digest('hex');
}

function manifestPath(root) {
  return path.join(root, MANIFEST_NAME);
}

function readManifest(root) {
  try {
    return JSON.parse(fs.readFileSync(manifestPath(root), 'utf8'));
  } catch {
    return null;
  }
}

function writeManifest(root, manifest) {
  fs.writeFileSync(manifestPath(root), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
}

export function managedRuntimeCurrent(root, executable, wheelPath, version = VERSION) {
  if (!fs.existsSync(executable)) return false;
  const manifest = readManifest(root);
  if (!manifest || manifest.version !== version) return false;
  const expectedHash = sha256File(wheelPath);
  return !expectedHash || manifest.wheelSha256 === expectedHash;
}

function semverDirectory(name) {
  return /^\d+\.\d+\.\d+(?:[-+].*)?$/.test(name);
}

export function pruneOldHostRuntimes(runtimeDir, { keepVersions = DEFAULT_KEEP_VERSIONS, currentVersion = VERSION } = {}) {
  if (!fs.existsSync(runtimeDir)) return [];
  const candidates = fs.readdirSync(runtimeDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && semverDirectory(entry.name))
    .map((entry) => {
      const fullPath = path.join(runtimeDir, entry.name);
      return { name: entry.name, fullPath, mtime: fs.statSync(fullPath).mtimeMs };
    })
    .sort((a, b) => b.mtime - a.mtime);
  const keep = new Set([currentVersion]);
  for (const item of candidates) {
    if (keep.size >= Math.max(1, keepVersions)) break;
    keep.add(item.name);
  }
  const removed = [];
  for (const item of candidates) {
    if (keep.has(item.name)) continue;
    fs.rmSync(item.fullPath, { recursive: true, force: true });
    removed.push(item.name);
  }
  return removed;
}

export function commandPath(command, options = {}) {
  if (!command) return '';
  const platform = options.platform || process.platform;
  const capture = options.capture || runCapture;
  if (path.isAbsolute(command) && fs.existsSync(command)) return command;
  if (platform === 'win32') {
    const result = capture('where.exe', [command]);
    return result.status === 0 ? firstLine(result.stdout) : '';
  }
  const result = capture('bash', ['-lc', `command -v ${shellQuote(command)}`]);
  return result.status === 0 ? firstLine(result.stdout) : '';
}

function runtimeMode(env = process.env) {
  const forced = truthy(env.BALDR_ROUTER_FORCE_WSL) ? 'wsl' : '';
  const raw = String(env.BALDR_ROUTER_LAUNCHER_MODE || forced || 'auto').toLowerCase();
  return ['auto', 'host', 'wsl'].includes(raw) ? raw : 'auto';
}

function hostRoot(runtimeDir, version = VERSION) {
  return path.join(runtimeDir, version, 'host');
}

function hostVenvPaths(runtimeDir, version = VERSION, rootOverride = '') {
  const root = rootOverride || hostRoot(runtimeDir, version);
  const venv = path.join(root, 'venv');
  if (process.platform === 'win32') {
    return {
      root,
      venv,
      python: path.join(venv, 'Scripts', 'python.exe'),
      router: path.join(venv, 'Scripts', 'baldr-router.exe'),
    };
  }
  return {
    root,
    venv,
    python: path.join(venv, 'bin', 'python'),
    router: path.join(venv, 'bin', 'baldr-router'),
  };
}

function pauseSynchronously(milliseconds) {
  const signal = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(signal, 0, 0, Math.max(1, milliseconds));
}

function activeLockOwner(lockRoot) {
  try {
    const owner = JSON.parse(fs.readFileSync(path.join(lockRoot, 'owner.json'), 'utf8'));
    if (owner.hostname !== os.hostname()) return false;
    const pid = Number(owner.pid);
    if (!Number.isSafeInteger(pid) || pid <= 0) return false;
    try {
      process.kill(pid, 0);
      return true;
    } catch (error) {
      return error?.code === 'EPERM';
    }
  } catch {
    return false;
  }
}

export function acquireHostInstallLock(runtimeDir, options = {}) {
  const version = options.version || VERSION;
  const timeoutMs = Math.max(1, Number(options.timeoutMs ?? 180_000));
  const staleMs = Math.max(timeoutMs, Number(options.staleMs ?? 600_000));
  const pollMs = Math.max(10, Number(options.pollMs ?? 100));
  const debug = options.debug || (() => {});
  const versionDir = path.join(runtimeDir, version);
  const lockRoot = path.join(versionDir, 'host.install.lock');
  const token = `${process.pid}-${Date.now()}`;
  const deadline = Date.now() + timeoutMs;
  fs.mkdirSync(versionDir, { recursive: true });

  while (true) {
    try {
      fs.mkdirSync(lockRoot);
      fs.writeFileSync(path.join(lockRoot, 'owner.json'), `${JSON.stringify({
        token,
        pid: process.pid,
        hostname: os.hostname(),
        createdAt: new Date().toISOString(),
      })}\n`, 'utf8');
      return {
        lockRoot,
        release() {
          try {
            const owner = JSON.parse(fs.readFileSync(path.join(lockRoot, 'owner.json'), 'utf8'));
            if (owner.token === token) fs.rmSync(lockRoot, { recursive: true, force: true });
          } catch {
            // A failed or replaced lock must never remove another installer's lock.
          }
        },
      };
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      let ageMs = 0;
      try {
        ageMs = Date.now() - fs.statSync(lockRoot).mtimeMs;
      } catch (statError) {
        if (statError?.code === 'ENOENT') continue;
        throw statError;
      }
      if (ageMs > staleMs && !activeLockOwner(lockRoot)) {
        debug(`removing stale runtime install lock at ${lockRoot}`);
        fs.rmSync(lockRoot, { recursive: true, force: true });
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error(
          `Timed out waiting for another Baldr process to finish installing the private runtime at ${lockRoot}`,
        );
      }
      pauseSynchronously(pollMs);
    }
  }
}

function findHostPython(options = {}) {
  const platform = options.platform || process.platform;
  const capture = options.capture || runCapture;
  const candidates = platform === 'win32'
    ? [
        { command: 'py.exe', prefix: ['-3'] },
        { command: 'python.exe', prefix: [] },
        { command: 'python3.exe', prefix: [] },
      ]
    : [
        { command: 'python3', prefix: [] },
        { command: 'python', prefix: [] },
      ];
  for (const candidate of candidates) {
    const resolved = commandPath(candidate.command, { platform, capture });
    if (!resolved) continue;
    const probe = capture(resolved, [...candidate.prefix, '-c', 'import sys; print(sys.version_info[0])']);
    if (probe.status === 0 && probe.stdout.trim() === '3') {
      return { command: resolved, prefix: candidate.prefix };
    }
  }
  return null;
}

function installHostRuntime(options) {
  const { runtimeDir, wheelPath, debug } = options;
  const finalPaths = hostVenvPaths(runtimeDir);
  if (managedRuntimeCurrent(finalPaths.root, finalPaths.router, wheelPath)) return finalPaths.router;
  const installLock = acquireHostInstallLock(runtimeDir, { debug });
  try {
    if (managedRuntimeCurrent(finalPaths.root, finalPaths.router, wheelPath)) return finalPaths.router;
    return installHostRuntimeUnlocked(options);
  } finally {
    installLock.release();
  }
}

function installHostRuntimeUnlocked({ runtimeDir, wheelPath, debug, keepVersions, platform, capture }) {
  const finalPaths = hostVenvPaths(runtimeDir);
  if (managedRuntimeCurrent(finalPaths.root, finalPaths.router, wheelPath)) return finalPaths.router;
  if (!wheelPath || !fs.existsSync(wheelPath)) {
    throw new Error(`Bundled baldr-router wheel is missing: ${wheelPath || '<unset>'}`);
  }
  const python = findHostPython({ platform, capture });
  if (!python) {
    throw new Error('Python 3 was not found on the host. Install Python 3 or use WSL on Windows.');
  }

  const versionDir = path.join(runtimeDir, VERSION);
  fs.mkdirSync(versionDir, { recursive: true });
  const previousManifest = readManifest(finalPaths.root);
  // Python virtual environments are not relocatable: console-script shebangs
  // contain the absolute interpreter path. Build at the final path and use a
  // rollback directory instead of renaming a staged venv into place.
  const rollbackRoot = path.join(versionDir, `host.rollback-${process.pid}-${Date.now()}`);
  fs.rmSync(rollbackRoot, { recursive: true, force: true });
  let previousMoved = false;
  if (fs.existsSync(finalPaths.root)) {
    fs.renameSync(finalPaths.root, rollbackRoot);
    previousMoved = true;
  }

  debug(`creating transactional private venv at ${finalPaths.venv}`);
  try {
    fs.mkdirSync(finalPaths.root, { recursive: true });
    let result = capture(python.command, [...python.prefix, '-m', 'venv', finalPaths.venv]);
    if (result.status !== 0) {
      throw new Error(`Could not create the private Python environment: ${result.stderr || result.stdout}`);
    }
    result = capture(finalPaths.python, [
      '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--upgrade', wheelPath,
    ]);
    if (result.status !== 0) {
      throw new Error(`Could not install baldr-router into the private environment: ${result.stderr || result.stdout}`);
    }
    if (!fs.existsSync(finalPaths.router)) {
      throw new Error(`Installation completed but the executable was not found at ${finalPaths.router}`);
    }
    writeManifest(finalPaths.root, {
      version: VERSION,
      wheelSha256: sha256File(wheelPath),
      installedAt: new Date().toISOString(),
      platform,
      executable: finalPaths.router,
      source: 'private-host-runtime',
      wheelPath,
      previousVersion: previousManifest?.version || null,
      rollbackPerformed: false,
      receiptSchemaVersion: 1,
    });
    if (!managedRuntimeCurrent(finalPaths.root, finalPaths.router, wheelPath)) {
      throw new Error('The private runtime was installed but failed its version/hash verification.');
    }
    fs.rmSync(rollbackRoot, { recursive: true, force: true });
    pruneOldHostRuntimes(runtimeDir, { keepVersions, currentVersion: VERSION });
    return finalPaths.router;
  } catch (error) {
    fs.rmSync(finalPaths.root, { recursive: true, force: true });
    if (previousMoved && fs.existsSync(rollbackRoot)) {
      fs.renameSync(rollbackRoot, finalPaths.root);
    } else {
      fs.rmSync(rollbackRoot, { recursive: true, force: true });
    }
    throw error;
  }
}

function wslAvailable(platform = process.platform, capture = runCapture) {
  if (platform !== 'win32') return false;
  const result = capture('wsl.exe', ['--status']);
  return result.status === 0 || `${result.stderr}\n${result.stdout}`.toLowerCase().includes('default distribution');
}

export function listWslDistros(options = {}) {
  const platform = options.platform || process.platform;
  const capture = options.capture || runCapture;
  if (platform !== 'win32') return [];
  const result = capture('wsl.exe', ['--list', '--quiet']);
  if (result.status !== 0) return [];
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/windows subsystem/i.test(line));
}

function wslArgs(distro, script) {
  const args = [];
  if (distro) args.push('-d', distro);
  args.push('--', 'bash', '-lc', script);
  return args;
}

function wslCapture(distro, script, capture = runCapture) {
  return capture('wsl.exe', wslArgs(distro, script));
}

function wslPrivateRoot(version = VERSION) {
  return `$HOME/.local/share/baldr-router-vscode/${version}`;
}

function wslPrivateRouter(version = VERSION) {
  return `${wslPrivateRoot(version)}/venv/bin/baldr-router`;
}

function probeWslTarget(
  distro,
  routerCommand,
  preferManaged = false,
  capture = runCapture,
  wheelPath = '',
) {
  const privateRouter = wslPrivateRouter();
  const manifest = `${wslPrivateRoot()}/${MANIFEST_NAME}`;
  const expectedHash = sha256File(wheelPath);
  const managedReady = expectedHash
    ? `[ -x ${privateRouter} ] && [ -f ${manifest} ] && grep -Fq ${shellQuote(expectedHash)} ${manifest}`
    : `[ -x ${privateRouter} ] && [ -f ${manifest} ]`;
  const checks = preferManaged
    ? [
        `if ${managedReady}; then printf '%s\n' ${privateRouter};`,
        `elif command -v ${shellQuote(routerCommand)} >/dev/null 2>&1; then command -v ${shellQuote(routerCommand)};`,
      ]
    : [
        `if command -v ${shellQuote(routerCommand)} >/dev/null 2>&1; then command -v ${shellQuote(routerCommand)};`,
        `elif ${managedReady}; then printf '%s\n' ${privateRouter};`,
      ];
  const script = [...checks, 'else exit 127; fi'].join(' ');
  const result = wslCapture(distro, script, capture);
  if (result.status !== 0) return null;
  return firstLine(result.stdout);
}

function windowsPathToWsl(distro, windowsPath, capture = runCapture) {
  const result = wslCapture(distro, `wslpath -u ${shellQuote(windowsPath)}`, capture);
  if (result.status !== 0 || !result.stdout) {
    throw new Error(`Could not translate the bundled wheel path into WSL: ${result.stderr || result.stdout}`);
  }
  return firstLine(result.stdout);
}

function installWslRuntime({ distro, wheelPath, debug, keepVersions, capture }) {
  if (!wheelPath || !fs.existsSync(wheelPath)) {
    throw new Error(`Bundled baldr-router wheel is missing: ${wheelPath || '<unset>'}`);
  }
  const wheelWsl = windowsPathToWsl(distro, wheelPath, capture);
  const root = wslPrivateRoot();
  const router = wslPrivateRouter();
  const rollbackRoot = `${root}.rollback-${process.pid}-${Date.now()}`;
  const wheelHash = sha256File(wheelPath);
  const manifest = JSON.stringify({
    version: VERSION,
    wheelSha256: wheelHash,
    installedAt: new Date().toISOString(),
    platform: 'wsl',
    executable: router,
    source: 'private-wsl-runtime',
    wheelPath: wheelWsl,
    previousVersion: null,
    rollbackPerformed: false,
    receiptSchemaVersion: 1,
  });
  const keep = Math.max(1, Number(keepVersions) || DEFAULT_KEEP_VERSIONS);
  // As on the host, create the venv at its final absolute path. Moving a venv
  // after installation breaks console-script shebangs. A rollback directory
  // preserves the prior same-version runtime until the new one verifies.
  const script = `set -e; ` +
    `root="$HOME/.local/share/baldr-router-vscode/${VERSION}"; ` +
    `rollback="$HOME/.local/share/baldr-router-vscode/${VERSION}.rollback-${process.pid}-${Date.now()}"; ` +
    `router="$root/venv/bin/baldr-router"; ` +
    `if [ -x "$router" ] && [ -f "$root/${MANIFEST_NAME}" ] && grep -Fq ${shellQuote(wheelHash)} "$root/${MANIFEST_NAME}"; then printf '%s\\n' "$router"; exit 0; fi; ` +
    `command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required inside WSL.' >&2; exit 127; }; ` +
    `rm -rf "$rollback"; moved=0; if [ -e "$root" ]; then mv "$root" "$rollback"; moved=1; fi; ` +
    `restore_runtime() { code=$?; rm -rf "$root"; if [ "$moved" -eq 1 ] && [ -e "$rollback" ]; then mv "$rollback" "$root"; fi; exit $code; }; ` +
    `trap restore_runtime ERR INT TERM; ` +
    `mkdir -p "$root"; python3 -m venv "$root/venv"; ` +
    `"$root/venv/bin/python" -m pip install --disable-pip-version-check --no-input --upgrade ${shellQuote(wheelWsl)} >&2; ` +
    `printf '%s\\n' ${shellQuote(manifest)} > "$root/${MANIFEST_NAME}"; ` +
    `[ -x "$router" ] && grep -Fq ${shellQuote(wheelHash)} "$root/${MANIFEST_NAME}"; ` +
    `trap - ERR INT TERM; rm -rf "$rollback"; ` +
    `base=$HOME/.local/share/baldr-router-vscode; count=0; for dir in $(ls -1dt "$base"/[0-9]* 2>/dev/null || true); do count=$((count+1)); if [ $count -gt ${keep} ]; then rm -rf "$dir"; fi; done; ` +
    `printf '%s\\n' "$router"`;
  debug(`installing transactional private WSL runtime${distro ? ` in ${distro}` : ''}`);
  const result = wslCapture(distro, script, capture);
  if (result.status !== 0) {
    throw new Error(`Could not install baldr-router inside WSL: ${result.stderr || result.stdout}`);
  }
  return firstLine(result.stdout);
}

function wslCandidates(preferred, options = {}) {
  const values = [];
  if (preferred) values.push(preferred);
  values.push('');
  for (const distro of listWslDistros(options)) if (!values.includes(distro)) values.push(distro);
  return values;
}

export function makeOptions(overrides = {}) {
  const env = overrides.env || process.env;
  const debugEnabled = truthy(env.BALDR_ROUTER_DEBUG);
  const debug = overrides.debug || ((message) => {
    if (debugEnabled) process.stderr.write(`[baldr-router-bootstrap] ${message}\n`);
  });
  return {
    mode: overrides.mode || runtimeMode(env),
    routerCommand: overrides.routerCommand || env.BALDR_ROUTER_COMMAND || 'baldr-router',
    wslDistro: overrides.wslDistro ?? env.BALDR_ROUTER_WSL_DISTRO ?? '',
    runtimeDir: overrides.runtimeDir || env.BALDR_VSCODE_RUNTIME_DIR || path.join(os.homedir(), '.baldr-router-vscode'),
    wheelPath: overrides.wheelPath || env.BALDR_BUNDLED_WHEEL || '',
    autoInstall: overrides.autoInstall ?? truthy(env.BALDR_ROUTER_AUTO_INSTALL || '1'),
    preferManaged: overrides.preferManaged ?? truthy(env.BALDR_ROUTER_PREFER_MANAGED),
    keepVersions: Number(overrides.keepVersions ?? env.BALDR_ROUTER_KEEP_VERSIONS ?? DEFAULT_KEEP_VERSIONS),
    platform: overrides.platform || process.platform,
    capture: overrides.capture || runCapture,
    debug,
    env,
  };
}

export function resolveRuntime(overrides = {}) {
  const options = makeOptions(overrides);
  const {
    mode, routerCommand, runtimeDir, wheelPath, autoInstall, preferManaged,
    keepVersions, platform, capture, debug,
  } = options;

  if (mode !== 'wsl') {
    const privatePaths = hostVenvPaths(runtimeDir);
    if (preferManaged && managedRuntimeCurrent(privatePaths.root, privatePaths.router, wheelPath)) {
      return { ok: true, kind: 'host', executable: privatePaths.router, source: 'private-host-runtime', mode };
    }
    if (!preferManaged) {
      const direct = commandPath(routerCommand, { platform, capture });
      if (direct) return { ok: true, kind: 'host', executable: direct, source: 'host-path', mode };
    }
    if (managedRuntimeCurrent(privatePaths.root, privatePaths.router, wheelPath)) {
      return { ok: true, kind: 'host', executable: privatePaths.router, source: 'private-host-runtime', mode };
    }
    if (preferManaged && !autoInstall) {
      const direct = commandPath(routerCommand, { platform, capture });
      if (direct) return { ok: true, kind: 'host', executable: direct, source: 'host-path', mode };
    }
    if (autoInstall) {
      try {
        const executable = installHostRuntime({
          runtimeDir, wheelPath, debug, keepVersions, platform, capture,
        });
        return { ok: true, kind: 'host', executable, source: 'installed-private-host-runtime', mode };
      } catch (error) {
        debug(String(error?.message || error));
        if (platform !== 'win32' || mode === 'host') {
          return { ok: false, kind: 'missing', mode, reason: String(error?.message || error) };
        }
      }
    }
  }

  if (platform === 'win32' && mode !== 'host' && wslAvailable(platform, capture)) {
    for (const distro of wslCandidates(options.wslDistro, { platform, capture })) {
      const existing = probeWslTarget(distro, routerCommand, preferManaged, capture, wheelPath);
      if (existing) {
        return { ok: true, kind: 'wsl', distro, executable: existing, source: 'wsl-existing', mode };
      }
    }
    if (autoInstall) {
      const preferred = options.wslDistro || '';
      try {
        const executable = installWslRuntime({
          distro: preferred, wheelPath, debug, keepVersions, capture,
        });
        return { ok: true, kind: 'wsl', distro: preferred, executable, source: 'installed-private-wsl-runtime', mode };
      } catch (error) {
        return { ok: false, kind: 'missing', mode, reason: String(error?.message || error) };
      }
    }
  }

  return {
    ok: false,
    kind: 'missing',
    mode,
    reason: 'baldr-router was not found and no compatible private runtime could be prepared.',
  };
}

export function sanitizedTarget(target) {
  if (!target) return target;
  return {
    ok: target.ok,
    kind: target.kind,
    distro: target.distro || null,
    executable: target.executable || null,
    source: target.source || null,
    mode: target.mode,
    reason: target.reason || null,
    version: VERSION,
  };
}

export function childSpec(target, routerArgs) {
  if (target.kind === 'host') {
    return { command: target.executable, args: routerArgs };
  }
  if (target.kind === 'wsl') {
    const command = `exec ${shellQuote(target.executable)} ${routerArgs.map(shellQuote).join(' ')}`;
    return { command: 'wsl.exe', args: wslArgs(target.distro || '', command) };
  }
  throw new Error(target.reason || 'No runtime target is available.');
}

function withWslForwardedEnv(target, env, extraEnv = {}) {
  if (target.kind !== 'wsl') return env;
  const forward = new Set([
    'CONTEXT7_API_KEY', 'KIRO_API_KEY', 'BALDR_TRUSTED_WORKSPACE_ROOTS_JSON',
    'BALDR_CLIENT_ID', 'BALDR_CLIENT_VERSION',
  ]);
  for (const name of Object.keys(extraEnv)) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) && extraEnv[name] != null) {
      forward.add(name);
    }
  }
  const existing = String(env.WSLENV || '').split(':').filter(Boolean);
  const names = new Set(existing.map((item) => item.split('/')[0]));
  for (const name of forward) {
    if (env[name] && !names.has(name)) existing.push(name);
  }
  if (existing.length) env.WSLENV = existing.join(':');
  return env;
}

export function spawnRuntime(target, routerArgs, options = {}) {
  const spec = childSpec(target, routerArgs);
  const extraEnv = options.env || {};
  const hostOs = process.platform === 'win32' ? 'windows' : process.platform === 'darwin' ? 'macos' : 'linux';
  const qualificationEnv = {
    BALDR_CLIENT_HOST_OS: process.env.BALDR_CLIENT_HOST_OS || hostOs,
    BALDR_RUNTIME_TRANSPORT: target.kind === 'wsl' ? 'wsl' : 'host',
    BALDR_RUNTIME_SOURCE: String(target.source || ''),
    BALDR_RUNTIME_WSL_DISTRO: String(target.distro || ''),
  };
  const env = withWslForwardedEnv(
    target,
    { ...process.env, ...qualificationEnv, ...extraEnv },
    { ...qualificationEnv, ...extraEnv },
  );
  return spawn(spec.command, spec.args, {
    stdio: options.stdio || 'inherit',
    windowsHide: true,
    detached: process.platform !== 'win32',
    env,
    cwd: options.cwd,
  });
}

export async function terminateChildTree(child, { graceMs = 400 } = {}) {
  if (!child?.pid) return;
  const waitForClose = async (milliseconds) => {
    if (child.exitCode !== null) return;
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, milliseconds);
      child.once('close', () => { clearTimeout(timer); resolve(); });
    });
  };
  if (process.platform === 'win32') {
    const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    await new Promise((resolve) => {
      killer.once('error', resolve);
      killer.once('close', resolve);
    });
    await waitForClose(1000);
    return;
  }
  try { process.kill(-child.pid, 'SIGTERM'); } catch { try { child.kill('SIGTERM'); } catch { /* best effort */ } }
  await new Promise((resolve) => setTimeout(resolve, graceMs));
  let groupAlive = false;
  try { process.kill(-child.pid, 0); groupAlive = true; } catch { /* already gone */ }
  if (groupAlive) {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { try { child.kill('SIGKILL'); } catch { /* best effort */ } }
  }
  await waitForClose(1000);
}

export function proxyRuntime(target, routerArgs, options = {}) {
  const child = spawnRuntime(target, routerArgs, { ...options, stdio: 'inherit' });
  let terminating = false;
  const forward = async () => {
    if (terminating) return;
    terminating = true;
    await terminateChildTree(child);
  };
  process.once('SIGINT', () => { void forward(); });
  process.once('SIGTERM', () => { void forward(); });
  child.on('error', (error) => {
    process.stderr.write(`[baldr-router-bootstrap] failed to start runtime: ${error.message}\n`);
    process.exitCode = 127;
  });
  child.on('exit', (code) => {
    process.exit(code ?? 1);
  });
  return child;
}

export function detectReport(overrides = {}) {
  const target = resolveRuntime({ ...overrides, autoInstall: false });
  return {
    ok: target.ok,
    launcher: {
      version: VERSION,
      platform: overrides.platform || process.platform,
      arch: process.arch,
      node: process.version,
      hostname: os.hostname(),
    },
    target: sanitizedTarget(target),
  };
}
