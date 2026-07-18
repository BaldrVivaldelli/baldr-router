.PHONY: test build publish doctor

test:
	baldr-agent test

build:
	baldr-agent build

publish:
	baldr-agent publish

doctor:
	baldr-agent doctor
