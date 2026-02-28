# Production Roadmap

## Current State (2026-02-28)

| Project | CI | Tests | Docker | GHCR | Deploy-ready |
|---------|-----|-------|--------|------|-------------|
| dotmac-platform | ✅ PASS | ✅ 68 | ✅ | ✅ auto-push | ✅ YES |
| dotmac_crm | ❌ mypy | ✅ 114 | ✅ | ✅ ghcr.yml | ❌ |
| dotmac_ecm | ❌ | ✅ 53 | ✅ | ❌ | ❌ |
| dotmac_erp | ❌ tests+precommit | ✅ 299 | ✅ | ❌ | ❌ |
| dotmac_starter | ❌ 6 failures | ✅ 44 | ❌ build fails | ❌ | ❌ |
| dotmac_sub | ❌ 6 failures | ✅ 120 | ❌ health fails | ❌ | ❌ |
| schoolnet | ❌ tests+lint | ✅ 51 | ✅ | ❌ | ❌ |

## Gold Standard: dotmac-platform
- CI: lint → typecheck → test → security → Docker build+push to GHCR
- On main push: auto-builds and pushes to ghcr.io
- Tags: semver + sha + latest
- Stack: FastAPI + uvicorn + PostgreSQL + Redis + Celery

## Phase 1: Green CI (all repos)
Priority tasks per repo to get CI passing.

## Phase 2: GHCR Pipeline (all repos)
Copy platform's Docker build+push workflow to all repos.

## Phase 3: Platform Integration
Deploy all services via dotmac-platform's compose/orchestration.
