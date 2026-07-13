const SAFE_COMMAND = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
const SAFE_FLAG = /^--[a-z0-9][a-z0-9-]{0,63}$/i;

/** Describe a runtime invocation without copying any argument value to logs. */
export function describeRuntimeInvocation(args: string[]): string {
  const bootstrapCommand = SAFE_COMMAND.test(args[0] ?? '') ? args[0] : 'command';
  if (bootstrapCommand !== 'exec') return `[runtime] ${bootstrapCommand}`;

  const separator = args.indexOf('--');
  const routerArgs = separator >= 0 ? args.slice(separator + 1) : [];
  const commandWords = routerArgs
    .slice(0, 2)
    .filter((value) => SAFE_COMMAND.test(value));
  const flags = routerArgs.filter((value) => SAFE_FLAG.test(value));
  return ['[runtime] exec', commandWords.length ? `-- ${commandWords.join(' ')}` : '', ...flags]
    .filter(Boolean)
    .join(' ');
}
