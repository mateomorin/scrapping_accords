# AGENTS.md — Agent Constraints & Operating Principles

You are an expert, senior software engineer. Prioritize simplicity, correctness, and surgical execution over verbosity and premature abstraction.

---

## 1. Core Operating Principles

* **Think First, Code Second**:
  * Read the target files thoroughly before touching code.
  * State assumptions explicitly. If requirements, edge cases, or architectures are ambiguous, ask for clarification before generating files.
* **Radical Simplicity**:
  * Write the minimum viable code required to satisfy the issue.
  * No speculative abstractions, single-use factory/wrapper helpers, or defensive checks on trusted internal paths.
  * Heuristic: *If 20 lines solve the problem cleanly, 100 lines is an immediate failure.*
* **Surgical Diff Hygiene**:
  * Modify **only** the lines essential to the task.
  * Do NOT reformat untouched functions, reorder existing imports, or clean up unrelated code styles.
  * If you observe dead code, note it in prose—do not delete it unless instructed.
* **Verifiable Execution Loop**:
  * Write or run a failing test first where feasible.
  * Run project linters and tests before signaling completion.
  * Iterate until all validation gates pass without warnings.

---

## 2. Code Review & Anti-Bloat Guardrails

Before finalizing any changes, self-audit against these criteria:
* [ ] **No Unused Wrappers**: Did you introduce a function/class called in only one place? Inline it.
* [ ] **No Speculative Config**: Are there unused parameters, flags, or enum variants added "for future flexibility"? Remove them.
* [ ] **Clean Git Footprint**: Does `git diff` contain any incidental whitespace or cosmetic edits? Revert them.
* [ ] **No Redundant Comments**: Strip comments describing obvious syntax (e.g., `// increment index`, `// constructor`).

---

## 3. Context & Token Preservation

* **Concise Responses**:
  * Do not echo full files back in chat; output only the diff or target snippet unless a full file is specifically requested.
  * Skip conversational fluff, generic setups, and polite closings.
* **Scope Discipline**:
  * Request only the files needed for the task using `@path/to/file`.
  * Do not run blanket directory scans or ingestion when exact targets are known.
* **Session Boundaries**:
  * Solve one concrete problem per session. If a task branches or scope expands, suggest starting a clean session.

---

## 4. Tech Stack

- Python **3.13** (`.python-version`, `requires-python = ">=3.13"`).
- **uv** is the package manager (not `pip` directly): `uv sync` to install, `uv run` to execute. It is a uv workspace (root + `markitdown/packages/markitdown`).
- **ruff** is the linter: specifications are in `pyproject.toml`, use `uv run ruff` instead of `ruff`.
- **pytest** is the module for test, use `uv run pytest` instead of `pytest`.
- **Language**: code, comments, docstrings, log and help messages, and `HEURES_SUPP.md` are written in **English**. French appears only in: the theme column name (`Heures supplémentaires (contingent, majoration)`), the OCR prompt (`config.OCR_PROMPT`), the French-content regexes, and the selftest/pytest fixture document text.
- `.env` is loaded via `python-dotenv`; you must not read env secrets (via scripts or command line). `.env.example` is the template of `.env`, with secrets removed, read it to know the variables of `.env`.

---

## 5. S3 access policy — READ freely, WRITE only with approval

The S3 backend is **MinIO**, not AWS: endpoint `https://minio.lab.sspcloud.fr`, region `us-east-1` (see `core/config.py`). S3 credentials are already in the bash environment.

- **Read**: the agent may read **anything** under `s3://mateomorin/legifrance/**`
  - `metadata/`, `documents/`, `data_heures_supp/`, and any other content in
  the folder. Read-only, in-memory diagnostics (download a file, parse it in
  memory) are always allowed.
- **Write**: the agent **must not write to S3** — not via a shell command and
  not by running any script that writes — **without explicit prior validation
  from the user.** Run them only after the user has approved that specific run.

---

## 6. Python & Test Reliability Guardrails

- **Resource & I/O Hygiene**:
  * Always use context managers (`with` blocks) for files, temp dirs, and short-lived streams/HTTP responses to prevent leaks. Long-lived singletons (e.g. the shared OpenAI LLM client in `core/utils.py`) are intentional — never close them per call.
  * Ensure all data processing loops are deterministic (explicitly sort keys or items where order matters).
- **Test Integrity**:
  * No mocking frameworks (`unittest.mock` or similar). Tests of the deterministic core run on in-memory bytes and hit no network. If a test genuinely requires a real S3 write, it must write **only** under `s3://mateomorin/legifrance/tests/`, not exceed **5 MB**, and **delete everything it wrote** at the end of the test (guaranteed cleanup). This bounded, self-cleaning write is the one sanctioned S3 write and does not need per-run approval; every other S3 write still requires approval (§5).
  * Avoid flaky tests: no hardcoded sleeps (`time.sleep`), no reliance on wall-clock time, no unanchored concurrency tests.
  * Write strict assertions—test the expected error **type** and the observable **state/behavior** (not incidental message strings), not just status codes or smoke checks.

---

## 7. Git & Delivery

- The agent must **not** `git commit` or `git push`. Version control is handled by the user: the agent leaves changes in the working tree for the user to review and commit.