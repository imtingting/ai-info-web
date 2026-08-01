PYTHON ?= python3
PYTHONPATH := src
DB_PATH ?= ../ai-info-web-data/ai-info-web.sqlite3

.PHONY: init test

init:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ai_info_web.cli init --db $(DB_PATH)

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v
