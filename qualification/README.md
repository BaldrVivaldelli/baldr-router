# Qualification input

Generate fresh templates for the target client instead of editing these files
from memory:

```bash
python scripts/dev.py qualification-template \
  --profile vscode-windows-wsl \
  --output-dir qualification/templates/vscode-windows-wsl
```

Available profiles are documented in
[`docs/real-environment-qualification.md`](../docs/real-environment-qualification.md).
The templates contain no secrets and should store only portable evidence
references, not screenshots, source code, API keys, or absolute user paths.
