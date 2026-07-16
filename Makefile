.PHONY: env install lock lint typecheck test check up down migrate ingest live collector collector-drill process-live prune tracks detect train-anomaly prepare-pilot-model benchmark-anomaly ensemble eval api ui

# One-time: create the conda env, then `conda activate pharos`
env:
	conda create -y -n pharos python=3.12

# Run inside the activated pharos env. Torch powers the flagship GRU on every platform.
install:
	pip install -r requirements-dev.txt && pip install -e .

# Refreeze the pinned lock (CI and Docker install from it).
lock:
	printf -- '--extra-index-url https://download.pytorch.org/whl/cpu\n\n' > requirements.lock
	pip freeze --exclude-editable >> requirements.lock

lint:
	ruff check . && ruff format --check .

typecheck:
	mypy

test:
	pytest

check: lint typecheck test

up:
	docker compose up -d db && docker compose run --rm migrate

down:
	docker compose down

migrate:
	alembic upgrade head

# Collection: load a cached NOAA Marine Cadastre AIS slice + the reference zones.
#   make ingest FILE=data/ais/AIS_2023_01_01_zone10.csv REGION=us-west
ingest:
	python -m pharos.ingest.flows "$(FILE)" "$(REGION)"

# Live global AIS lane via AISStream.io (needs the `live` extra + PHAROS_AISSTREAM_KEY).
# Streams for a bounded window into the corpus. Includes Malacca / Singapore Strait.
live:
	python -m pharos.ingest.aisstream

# Continuous, reconnecting Singapore collector. For the pilot, point it at a local SQLite file:
#   PHAROS_DATABASE_URL=sqlite:///data/sg-live.db make collector
collector:
	python -m pharos.collector.worker

# Deterministic, keyless failure drill: disconnect, health timeout (sleep), reconnect, and dedup.
collector-drill:
	pytest -q tests/test_collector_worker.py

# One manual incremental pass over the configured live-pilot window.
process-live:
	python -m pharos.tracks.incremental

# Checkpoint WAL and prune only expired live positions (default retention: 21 days).
prune:
	python -m scripts.prune

# Build per-vessel tracks from raw positions (segment -> resample -> kinematics).
tracks:
	python -m pharos.tracks.build

# Run the detector ensemble (gap / rendezvous / loiter / spoof / anomaly) -> Incident rows.
detect:
	python -m pharos.detect.run

# Train the flagship torch GRU trajectory-anomaly model.
train-anomaly:
	python -m pharos.detect.anomaly

# Reproducible real-data artifact used for the Singapore pilot freeze.
prepare-pilot-model:
	OMP_NUM_THREADS=2 VECLIB_MAXIMUM_THREADS=2 \
		PHAROS_DATABASE_URL=sqlite:///data/sg-pilot-training.db \
		python -m scripts.prepare_pilot_model \
		--source data/ais/la_2020_01_01.csv=us-la \
		--source data/ais/gulf_gfw_cohort_2023_07_25.csv=us-gulf

# Reproduce the GRU hidden-capacity selection over data + initialization seeds.
benchmark-anomaly:
	python -m scripts.benchmark_anomaly

# Fuse detector outputs into per-vessel maritime-threat rollups (the composite).
ensemble:
	python -m pharos.detect.ensemble

# Score the detector ensemble on the synthetic gold set (per-type P/R, cross-region) ->
# docs/EVAL.md. The independent GFW corroboration runs as part of eval-real.
eval:
	python -m pharos.eval.run

# Real-data validation on a downloaded NOAA Marine Cadastre AIS slice (see scripts/eval_real.py
# for the download + filter one-liners). The honest test the synthetic gold set can't be.
#   make eval-real FILE=data/ais/la_2020_01_01.csv REGION=us-la
eval-real:
	python -m scripts.eval_real "$(FILE)" "$(REGION)"

# Serve the read-only FastAPI + GeoJSON endpoints on :8000
api:
	uvicorn pharos.api.app:app --reload

# React dashboard dev server on :5173 (needs make api in another shell)
ui:
	npm --prefix frontend install && npm --prefix frontend run dev
