export const REDACTED = '<redacted>';

const secretPatterns: RegExp[] = [
  /\bctx7sk-[A-Za-z0-9_-]{8,}/gi,
  /\bsk-[A-Za-z0-9_-]{16,}/gi,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /(api[_-]?key|token|password|secret)(\s*[=:]\s*)([^\s,;\]}]{6,})/gi,
];

export function redactSensitive(value: string, secrets: readonly string[] = []): string {
  let output = String(value);
  for (const secret of [...secrets].filter((item) => item.length >= 6).sort((a, b) => b.length - a.length)) {
    output = output.split(secret).join(REDACTED);
  }
  for (const pattern of secretPatterns) {
    output = output.replace(pattern, (...args: unknown[]) => {
      const match = String(args[0]);
      if (/^(api[_-]?key|token|password|secret)/i.test(match)) {
        const groups = args.slice(1, 4).map(String);
        return `${groups[0]}${groups[1]}${REDACTED}`;
      }
      return REDACTED;
    });
  }
  return output;
}
