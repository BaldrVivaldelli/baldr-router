.DEFAULT_GOAL := help

PYTHON ?= python
UV ?= uv
NPM ?= npm
# Use public PyPI unless the caller explicitly supplies another compliant index.
UV_DEFAULT_INDEX ?= https://pypi.org/simple
export UV_DEFAULT_INDEX

ROUTER_DIR := router
ADAPTER_DIR := facades/kiro/adapter
AGENT_SDK_DIR := sdks/python
AGENT_BUILDER_DIR := tooling/agent-builder
AGENT_RUNNER_DIR := runtimes/agent-runner
EXTENSION_DIR := facades/vscode-extension

# make up WORKSPACE=/path/to/repo runs the console against that repository.
# Without it the console can watch, but not start or configure work.
WORKSPACE ?=
PORT ?= 8787

.PHONY: up localhost down help deps test lint check ci build verify-release install install-agent-runtime

help:
	@printf '%s\n' \
		'Baldr Router' \
		'' \
		'  make up [WORKSPACE=/path/to/repo] [PORT=8787]' \
		'      Levanta la consola local y abre el navegador con su token.' \
		'      Sin WORKSPACE solo observa; con WORKSPACE también crea y configura.' \
		'' \
		'  make deps          Instala las dependencias de desarrollo' \
		'  make test | lint   Corre la suite o el linter de todos los paquetes' \
		'  make check         test + lint' \
		'  make ci            Todo lo que corre CI: check + typecheck + coverage + audit' \
		'  make build         Construye la release; make verify-release la verifica' \
		'  make install       Deja baldr-router y el adapter de Kiro en el PATH' \
		'  make install-agent-runtime   Deja baldr-agent y baldr-agent-runner en el PATH' \
		'' \
		'Todo lo demás vive en scripts/dev.py y en cada paquete:' \
		'  python scripts/dev.py typecheck|coverage|audit|build|verify-release' \
		'  cd router && uv run --extra dev pytest -q        (un solo paquete)' \
		'  npm --prefix facades/vscode-extension run check  (la extensión)'

up:
	$(UV) run --project $(ROUTER_DIR) baldr-router console --open --port "$(PORT)" \
		$(if $(WORKSPACE),--workspace-root "$(WORKSPACE)")

# So that `make localhost up` reads the way it sounds. Make runs a target once
# per invocation, so this does not start the console twice.
localhost: up

down:
	@echo 'La consola corre en primer plano: cortala con Ctrl-C en su terminal.'

deps:
	$(UV) sync --project $(ROUTER_DIR) --extra dev
	$(UV) sync --project $(ADAPTER_DIR) --extra dev
	$(UV) sync --project $(AGENT_SDK_DIR) --extra dev
	$(UV) sync --project $(AGENT_BUILDER_DIR) --extra dev
	$(UV) sync --project $(AGENT_RUNNER_DIR) --extra dev
	$(NPM) ci --ignore-scripts --no-audit --no-fund
	$(NPM) --prefix $(EXTENSION_DIR) ci --ignore-scripts --no-audit --no-fund

test:
	$(PYTHON) scripts/dev.py test

lint:
	$(PYTHON) scripts/dev.py lint

check: test lint

ci: check
	$(PYTHON) scripts/dev.py typecheck
	$(PYTHON) scripts/dev.py coverage
	$(PYTHON) scripts/dev.py audit

build:
	$(PYTHON) scripts/dev.py build

verify-release:
	$(PYTHON) scripts/dev.py verify-release

install:
	$(UV) tool install --force --editable ./$(ROUTER_DIR) \
		--with-editable ./$(ADAPTER_DIR) --with-executables-from baldr-kiro-adapter

install-agent-runtime:
	$(UV) tool install --force --editable ./$(AGENT_RUNNER_DIR) \
		--with-editable ./$(AGENT_SDK_DIR) --with-editable ./$(AGENT_BUILDER_DIR) \
		--with-executables-from baldr-agent-builder
