PYTHON ?= python3

.PHONY: test compile check

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -q

compile:
	$(PYTHON) -m py_compile guest_gateway.py rich_renderer.py

check: compile test
	bash -n init-env.sh run-docker.sh install-launchagent.sh
