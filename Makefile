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
LAUNCHER_DIR := launcher
EXTENSION_DIR := facades/vscode-extension

CLI_ARGS ?=
QUALIFICATION_PROFILE ?= vscode-windows-wsl
QUALIFICATION_TEMPLATE_DIR ?= qualification/templates/$(QUALIFICATION_PROFILE)
QUALIFICATION_WORKSPACE_ROOT ?= .
QUALIFICATION_EVIDENCE_DIRECTORY ?= qualification-input
QUALIFICATION_OUTPUT_DIRECTORY ?= qualification-output
QUALIFICATION_REPEAT ?= 3
QUALIFICATION_CLIENT ?=

.PHONY: \
	all help deps test lint check build build-no-tests release verify-release \
	facades facades-check router-test router-lint adapter-test adapter-lint agent-sdk-test agent-sdk-typescript-test agent-builder-test agent-builder-typescript-test agent-runner-test \
	launcher-test extension-install extension-check extension-test extension-package extension-clean \
	install-kiro install-agent-runtime install-launcher cli mcp qualification-definitions qualification-template qualification-ci

all: check

help:
	@printf '%s\n' \
		'Baldr Router — targets disponibles:' \
		'  make deps                         Instala dependencias de desarrollo locales' \
		'    (usa PyPI público; UV_DEFAULT_INDEX=<url> permite reemplazarlo)' \
		'  make test | lint | check           Ejecuta validación completa' \
		'  make facades | facades-check       Genera o valida fachadas desde el contrato' \
		'  make build | build-no-tests        Construye la release completa' \
		'  make verify-release | release      Verifica artefactos o construye la release' \
		'  make router-test | adapter-test | agent-sdk-test | agent-sdk-typescript-test' \
		'  make agent-builder-test | agent-builder-typescript-test | agent-runner-test' \
		'  make launcher-test | extension-test' \
		'  make extension-install | extension-check | extension-package' \
		'  make install-kiro | install-agent-runtime | install-launcher' \
		'  make cli CLI_ARGS="<comando>"      Ejecuta la CLI desde el checkout' \
		'  make qualification-template QUALIFICATION_PROFILE=... QUALIFICATION_TEMPLATE_DIR=...' \
		'  make qualification-ci QUALIFICATION_PROFILE=... QUALIFICATION_WORKSPACE_ROOT=... QUALIFICATION_EVIDENCE_DIRECTORY=...'

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

facades:
	$(PYTHON) scripts/generate_facades.py

facades-check:
	$(PYTHON) scripts/generate_facades.py --check

router-test:
	cd $(ROUTER_DIR) && $(UV) run --extra dev pytest -q

router-lint:
	cd $(ROUTER_DIR) && $(UV) run --extra dev ruff check src tests
	$(PYTHON) -m compileall -q $(ROUTER_DIR)/src

adapter-test:
	cd $(ADAPTER_DIR) && $(UV) run --extra dev pytest -q

adapter-lint:
	cd $(ADAPTER_DIR) && $(UV) run --extra dev ruff check src tests

agent-sdk-test:
	cd $(AGENT_SDK_DIR) && $(UV) run --extra dev pytest -q

agent-sdk-typescript-test:
	$(NPM) test --workspace @baldr/agent-sdk

agent-builder-test:
	cd $(AGENT_BUILDER_DIR) && $(UV) run --extra dev pytest -q

agent-builder-typescript-test:
	$(NPM) run build --workspace @baldr/agent-sdk
	$(NPM) test --workspace @baldr/agent-builder-typescript

agent-runner-test:
	cd $(AGENT_RUNNER_DIR) && $(UV) run --extra dev pytest -q

launcher-test:
	$(NPM) --prefix $(LAUNCHER_DIR) test

extension-install:
	$(NPM) --prefix $(EXTENSION_DIR) ci --ignore-scripts --no-audit --no-fund

extension-check:
	$(NPM) --prefix $(EXTENSION_DIR) run check

extension-test:
	$(NPM) --prefix $(EXTENSION_DIR) test

extension-package:
	$(NPM) --prefix $(EXTENSION_DIR) run package

extension-clean:
	$(NPM) --prefix $(EXTENSION_DIR) run clean

build:
	$(PYTHON) scripts/dev.py build

build-no-tests:
	$(PYTHON) scripts/dev.py build --skip-tests

verify-release:
	$(PYTHON) scripts/dev.py verify-release

release: build

install-kiro:
	$(UV) tool install --force --editable ./$(ROUTER_DIR) --with-editable ./$(ADAPTER_DIR) --with-executables-from baldr-kiro-adapter

install-agent-runtime:
	$(UV) tool install --force --editable ./$(AGENT_RUNNER_DIR) --with-editable ./$(AGENT_SDK_DIR) --with-editable ./$(AGENT_BUILDER_DIR) --with-executables-from baldr-agent-builder

install-launcher:
	cd $(LAUNCHER_DIR) && $(NPM) install -g .

cli:
	cd $(ROUTER_DIR) && $(UV) run baldr-router $(CLI_ARGS)

mcp:
	$(MAKE) cli CLI_ARGS="mcp"

qualification-definitions:
	$(MAKE) cli CLI_ARGS="qualification definitions"

qualification-template:
	$(PYTHON) scripts/dev.py qualification-template --profile "$(QUALIFICATION_PROFILE)" --output-dir "$(QUALIFICATION_TEMPLATE_DIR)"

qualification-ci:
	$(UV) run --project $(ROUTER_DIR) python scripts/run_qualification_ci.py \
		--profile "$(QUALIFICATION_PROFILE)" \
		--workspace-root "$(QUALIFICATION_WORKSPACE_ROOT)" \
		--evidence-directory "$(QUALIFICATION_EVIDENCE_DIRECTORY)" \
		--output-directory "$(QUALIFICATION_OUTPUT_DIRECTORY)" \
		--repeat "$(QUALIFICATION_REPEAT)" \
		$(if $(QUALIFICATION_CLIENT),--client "$(QUALIFICATION_CLIENT)")
