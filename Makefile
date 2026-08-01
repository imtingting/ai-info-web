PYTHON ?= python3
PYTHONPATH := src
DB_PATH ?= ../ai-info-web-data/ai-info-web.sqlite3

.PHONY: init test fetch-github fetch-product-hunt curate

init:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ai_info_web.cli init --db $(DB_PATH)

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

fetch-github:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ai_info_web.cli fetch-github --db $(DB_PATH)

fetch-product-hunt:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ai_info_web.cli fetch-product-hunt --db $(DB_PATH)

curate:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ai_info_web.cli curate --db $(DB_PATH)
