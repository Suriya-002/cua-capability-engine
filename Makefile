.PHONY: install lint type test test-live discover replay serve docker evidence-verify

install:
	pip install -e ".[dev]" && playwright install chromium && pre-commit install

lint:
	ruff check . && ruff format --check .

type:
	mypy

test:
	pytest -m "not live" -q

test-live:
	pytest -m live -q

serve:
	uvicorn cua.api.app:app --host 0.0.0.0 --port 7860 --reload

discover:
	cua discover --goal "Look up member 10023 and read their savings balance" \
	  --entry $${CUA_MOCKBANK_URL:-http://localhost:7860/bank}/login \
	  --param member_id=10023 --name lookup_member_balance

replay:
	cua replay evidence/capabilities/lookup_member_balance@1.0.0.json --param member_id=10023

evidence-verify:
	cua evidence verify evidence/

docker:
	docker build -t cua-demo:local . && docker run --rm -p 7860:7860 --env-file .env cua-demo:local
