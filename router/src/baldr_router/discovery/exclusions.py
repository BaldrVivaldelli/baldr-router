from __future__ import annotations

import fnmatch
from pathlib import Path

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode-test",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "vendor",
        "target",
        "dist",
        "build",
        "coverage",
        ".coverage",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        "__pycache__",
    }
)

SENSITIVE_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials*",
    "secrets.*",
    "*.secret",
    "*.token",
)

MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pnpm-workspace.yaml",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "bun.lock",
        "bun.lockb",
        "deno.json",
        "deno.jsonc",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "Pipfile",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "Makefile",
        "Taskfile.yml",
        "Taskfile.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "Gemfile",
        "composer.json",
        "mix.exs",
        "pubspec.yaml",
    }
)

MANIFEST_SUFFIXES = (
    ".sln",
    ".csproj",
    ".fsproj",
    ".vbproj",
)

LANGUAGE_EXTENSIONS = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".cs": "C#",
    ".fs": "F#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++",
    ".hpp": "C++",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".scala": "Scala",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".dart": "Dart",
    ".lua": "Lua",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".vue": "Vue",
    ".svelte": "Svelte",
}


def is_sensitive_file(path: Path) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


def is_manifest(path: Path) -> bool:
    return path.name in MANIFEST_NAMES or path.name.endswith(MANIFEST_SUFFIXES)


def excluded_directory(name: str) -> bool:
    return name in EXCLUDED_DIRECTORIES
