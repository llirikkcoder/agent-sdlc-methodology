PY ?= python3
EXAMPLES := examples/regulated-document-qa examples/ai-office-sales examples/agent-delivery

.PHONY: check validate validate-lite drift drift-strict drift-anchor purity test demo

check: validate validate-lite drift purity test
	@echo "== все проверки пройдены"

validate:
	@for e in $(EXAMPLES); do $(PY) tools/validate.py $$e || exit 1; done

validate-lite:
	@for e in $(EXAMPLES); do SOFTOSCOPE_NO_JSONSCHEMA=1 $(PY) tools/validate.py $$e || exit 1; done

drift:
	@for e in $(EXAMPLES); do $(PY) tools/drift.py $$e || exit 1; done

drift-strict:
	@for e in $(EXAMPLES); do $(PY) tools/drift.py $$e --strict || true; done

# сверка примера agent-delivery с живым кодом anchor: make drift-anchor ANCHOR_REPO=/путь/к/anchor
drift-anchor:
	@test -n "$(ANCHOR_REPO)" || (echo "укажите ANCHOR_REPO=/путь/к/anchor" && exit 2)
	$(PY) tools/drift.py examples/agent-delivery --repo $(ANCHOR_REPO)

purity:
	$(PY) tools/check_core_purity.py

test:
	$(PY) -m unittest discover -s tests

demo:
	$(PY) examples/ai-office-sales/demo_run.py
