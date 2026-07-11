import fs from 'node:fs';
for (const target of ['dist']) {
  fs.rmSync(new URL(`../${target}`, import.meta.url), { recursive: true, force: true });
}
