.PHONY: test bundle check
REPOSITORY ?=

test: check
	python3 -m unittest discover -s tests -v

check:
	bash -n install.sh bootstrap.sh
	sh -n payload/lang-switcher payload/switch_layout.sh payload/switch_case.sh
	python3 -m compileall -q install.py payload scripts tests
	@if command -v node >/dev/null; then node --check payload/extension/extension.js; fi

bundle:
	python3 scripts/build_release.py --repo "$(REPOSITORY)"
