# Safe Callisto Local Runtime Plan

## Objective

Make the documented Callisto local launcher start a usable API and frontend on macOS without unsafe OpenMP workarounds, without automatically initializing any venue executor, and without requiring trading credentials.

## Safety boundary

- Default execution mode is `disabled`.
- Ordinary API startup never initializes the inherited Polymarket execution service.
- No live Kalshi route or worker is connected by this increment.
- `KMP_DUPLICATE_LIB_OK` is forbidden.
- Lefty-v5 remains untouched.
- No finite trading policy limits are added.

## Reproduced failures

1. Normal `uvicorn main:app` aborted on macOS with OpenMP error 15.
2. Line tracing localized the abort to `semantic_matcher.initialize()` during API lifespan startup.
3. `services.news.semantic_matcher` eagerly imports FAISS, which loads its bundled `libomp.dylib`; sentence-transformers initialization then loads a second OpenMP runtime.
4. API startup called the legacy Polymarket `live_execution_service.initialize()` even when execution credentials were absent.
5. Strategy and data-source warmup passed an unsupported `session=` keyword.
6. `/health/ready` treated the separately deployed scanner worker as required for API readiness.
7. The default frontend port `3000` conflicts with an existing local Gitea service.

## Implementation sequence

1. Add RED tests for platform-specific FAISS defaults, execution-disabled API startup, loader warmup signatures, and API readiness semantics.
2. Disable FAISS by default on macOS while preserving sentence-transformer embeddings with NumPy similarity fallback; keep explicit `NEWS_ENABLE_FAISS=1` override support.
3. Remove legacy live-execution initialization from the API lifespan and ensure worker startup cannot initialize live execution in Callisto's default disabled mode.
4. Repair strategy and data-source loader warmup calls.
5. Make API readiness depend on the API/database boundary; report scanner, WebSocket feeds, Redis, and execution as observable optional/degraded capabilities rather than mandatory API gates.
6. Move the local Vite/GUI default to port `5173`, retaining environment override support.
7. Rebrand the browser title, application header, launcher banner/window, and local-run documentation to Callisto while preserving upstream attribution and AGPL notices.
8. Run normal startup against an isolated local PostgreSQL database, then verify `/health/live`, `/health/ready`, `/docs`, frontend navigation, shutdown, and clean git state.

## Verification results

- Final selected safe-runtime, worker-host, Kalshi, main-lifespan, and news-accuracy suite: `51 passed`, with the DB-only lifespan test separately covered by real local server validation.
- The macOS native regression subprocess successfully imported `main` followed by PyTorch after setup removed FAISS; no OpenMP error occurred.
- The complete setup script and documented `run.sh --services-smoke-test` launcher path passed and cleaned up their services.
- Frontend production build: passed.
- Backend compilation and shell syntax checks: passed.
- Normal `uvicorn main:app` served `/health/live`, `/health/ready`, and `/docs` without a wrapper.
- Readiness reported `status=ready`, `database=true`, and `execution=disabled` with Redis and worker planes intentionally off.
- Startup contained no OpenMP error and no legacy venue-execution initialization attempt.
- Ten distinct frontend screens rendered from the normal local runtime with Callisto branding.
- Backend, frontend, and temporary PostgreSQL processes were stopped; local ports were verified closed.

## Verification gates

- Focused tests observed RED before implementation and GREEN afterward.
- Relevant backend regression tests pass.
- `ruff check` and `ruff format --check` pass on changed Python files.
- Backend `compileall` passes.
- Frontend production build passes.
- Normal `uvicorn main:app` starts without a wrapper and without OpenMP error 15.
- Startup logs contain no attempt to initialize Polymarket or Kalshi execution.
- `/health/live` returns `alive`.
- `/health/ready` returns `ready` with the database healthy while optional worker planes are disabled.
- Ten distinct frontend screens render through the normal local runtime.
- Secret scan and `git diff --check` pass.
- Independent financial/runtime review has no blockers before merge.
