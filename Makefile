.PHONY: env install lock lint typecheck test check up down migrate ingest live tracks detect train-anomaly ensemble eval api ui

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

# Build per-vessel tracks from raw positions (segment -> resample -> kinematics).
tracks:
	python -m pharos.tracks.build

# Run the detector ensemble (gap / rendezvous / loiter / spoof / anomaly) -> Incident rows.
detect:
	python -m pharos.detect.run

# Train the flagship torch GRU trajectory-anomaly model.
train-anomaly:
	python -m pharos.detect.anomaly

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
