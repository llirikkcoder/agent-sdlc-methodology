PY ?= python3
EXAMPLES := examples/regulated-document-qa examples/ai-office-sales examples/agent-delivery examples/equipment-rental
# примеры, описанные классами (ADR-021): model.py — источник, JSON сверяется с ним
DESCRIBED := examples/equipment-rental

.PHONY: check validate validate-lite drift drift-strict drift-anchor purity test demo described twin

check: validate validate-lite described drift purity test
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

# описание на классах и закоммиченные пять слоёв не должны расходиться
described:
	@for e in $(DESCRIBED); do $(PY) tools/digitize.py build $$e/model.py --out $$e --check || exit 1; done

# цифровой двойник примера проката: сквозные сценарии на заглушках
twin:
	$(PY) tools/digitize.py twin examples/equipment-rental --scenario examples/equipment-rental/twin-scenarios.json \
		--chain booking_request,equipment_issue,rental_return@b-1 --chain booking_request,equipment_issue,damaged_return@b-2

purity:
	$(PY) tools/check_core_purity.py

test:
	$(PY) -m unittest discover -s tests

demo:
	$(PY) examples/ai-office-sales/demo_run.py
