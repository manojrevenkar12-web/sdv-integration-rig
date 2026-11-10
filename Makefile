.PHONY: test rust web eval-dev crosscheck report serve
test:
	python -m pytest -m "not slow"
rust:
	cd tools/isotp-trace && cargo test --release && cargo build --release
web:
	cd web && npm ci && npx tsc --noEmit && npx vitest run && npx vite build
eval-dev:
	python -m sdvrig flash-eval --set dev && python -m sdvrig fleet-eval --set dev
crosscheck: rust
	python -m sdvrig crosscheck
report:
	python -m pytest -m "not slow" -q --junitxml=reports/tests.xml && python -m sdvrig report
serve:
	python -m sdvrig serve --port 13400
