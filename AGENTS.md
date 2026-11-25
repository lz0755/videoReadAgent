# Video Understanding Agent — Repository Rules

## Scope
- P0 is single-user, local deployment, one Worker (current repo environment uses Python 3.14.6 per user decision; plan baseline was 3.11, tracked in ADR-001).
- Implement only the current approved PR; no drive-by refactor or dependency upgrade.
- P1 includes leases/fencing, complex playback proxies, custom VAD remapping and websites.
- All real media must have recorded authorization and a content hash.

## Read before coding
- Read this file, the exact PR spec, relevant code contracts and prerequisite handoffs.
- Do not load all design documents or historical chats into every task.
- Models/ports/config/schema/prompts become truth after their owning PR is merged.
- A code/document conflict requires an ADR and a reviewed contract change.
- Use analysis_id everywhere; qa_job_id maps to jobs.job_id.

## Architecture
- domain contains pure models, errors, policies and ports; no infrastructure imports.
- application and agent use injected ports, not concrete adapters or provider SDKs.
- Concrete implementations are wired in bootstrap.py.
- No import-time network, required API key lookup or model-weight loading.
- Keep SDK/provider details out of domain.
- Run import-linter and AST architecture tests; do not bypass with dynamic imports.

## Two-round TDD
- First-time contracts may use a reviewed Step 0 signature-only scaffold.
- Step A changes tests and explicitly approved fixtures only.
- Prove fixture properties first; red must be a behavior failure, not an import failure.
- Step B changes only allowed production paths; frozen assertions cannot change.
- Never delete, skip or weaken a failing test to make a PR green.
- Report exact commands, failures and checks that were not run.

## Integration tests
- Use real tmp_path files, disk SQLite, FFmpeg and PyAV in integration tests.
- Do not mock av, subprocess, sqlite3 or the real filesystem in integration tests.
- Provider Fakes at the declared port are allowed; unit tests may use mocks.
- Fixture Self Check failures are failures, not skips or normal samples.

## Media and evidence
- Times are strict integer milliseconds; ranges are [start_ms, end_ms).
- Preserve actual PTS, time_base, origin, requested time and actual selected time.
- Never use frame_index/fps, requested time or zero as fabricated evidence time.
- P0 ASR uses vad_filter=false; no unverified timestamp remapping.
- Claims cite existing evidence from the bound asset_id and analysis_id.
- Distinguish observed, heard and inferred; missing coverage is not absence.

## Safety and budgets
- Default tests are offline; provider_live and model_local are explicit jobs.
- Cloud egress requires consent and credentials; no silent Fake/provider fallback.
- Models cannot choose paths, URLs, asset ownership, shell commands or credentials.
- Count each model attempt, tool execution and sent image part, including retries.
- Files API HTTP calls, retries and transferred bytes are counted separately and share deadline.
- SDK retries are zero; app retries and repair share task caps and deadlines.
- Reserve before send; unknown external cost/result is never treated as zero.
- Never mark documented_supported or untested capabilities as runtime_verified.

## Quality gates
- `uv run --no-sync ruff check src experiments tests`
- `uv run --no-sync ruff format --check src experiments tests`
- `uv run --no-sync mypy src/video_agent experiments tests`
- `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket`

## Delivery
- No secrets, private frames/transcripts or absolute private paths in logs or commits.
- Update docs/handoff/PR-<id>.md with tests, contracts, limits and next dependency.
- Do not claim model quality, offline isolation or recovery that was not verified.
