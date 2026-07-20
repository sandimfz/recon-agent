# Strix-Mini — Architecture & End-to-End Flow

> Single-agent pentest framework. Fork of Strix, optimized for **local LLMs**
> (Gemma/Qwen/DeepSeek via LM Studio/Ollama, or any OpenAI-compatible endpoint).
> The agent runs in a Docker sandbox; a Caido proxy captures all in-sandbox
> traffic; tools are invoked over HTTP from the host into the container.

This document describes the full request lifecycle — from
`python3 strix/interface/main.py` to a finished scan — and how each subsystem
fits together. Written against commit `1ab050a` (branch `feat/warm-sandbox`).

---

## 1. Component map

```
                         HOST (your machine / VPS)
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  strix/interface/main.py           ← entry, arg parse, config load   │
  │      │                                                              │
  │      ├─ apply_saved_config()        ← ~/.strix/cli-config.json → env │
  │      ├─ check_docker_connection()  ← supports ssh:// for remote VPS  │
  │      ├─ pull_docker_image()         ← ghcr.io/usestrix/strix-sandbox │
  │      ├─ validate_environment()      ← STRIX_LLM must be set          │
  │      ├─ warm_up_llm()               ← FIRST LLM call (health check)  │
  │      ├─ generate_run_name()         ← slug + token_hex(4)           │
  │      └─ asyncio.run(run_cli|run_tui)                                │
  │                  │                                                  │
  │         strix/interface/cli.py:run_cli                              │
  │                  │                                                  │
  │         StrixAgent(agent_config).execute_scan(scan_config)           │
  │                  │  (strix/agents/StrixAgent/strix_agent.py)         │
  │         BaseAgent.agent_loop(task)   ← while True, max 300 iters    │
  │                  │                                                  │
  │     ┌────────────┴────────────────────────────────┐                  │
  │     │ LLM.generate()    (strix/llm/llm.py)        │                  │
  │     │  litellm.acompletion(stream=True)           │ ──► LLM API     │
  │     │  parse <function=..> XML from output        │                  │
  │     └────────────┬────────────────────────────────┘                  │
  │                  │                                                  │
  │     strix/tools/executor.py:_execute_tool_in_sandbox                │
  │                  │  httpx POST /execute  (Bearer sandbox_token)     │
  └──────────────────┼──────────────────────────────────────────────────┘
                     │
        Docker port mapping (random host port → 48081)
                     │
  ┌──────────────────▼───────────────── CONTAINER (strix-sandbox:0.1.13) ──┐
  │ docker-entrypoint.sh:                                                 │
  │   1. caido-cli --listen 0.0.0.0:48080  (proxy + GraphQL)              │
  │   2. loginAsGuest → token; createProject(temporary); selectProject   │
  │   3. write /etc/profile.d/proxy.sh (all traffic → 48080)              │
  │   4. import CA into NSSDB (browser trust)                             │
  │   5. python -m strix.runtime.tool_server --token=.. --port=48081      │
  │                                                                        │
  │   ┌─────────────────────────────┐    ┌────────────────────────────┐  │
  │   │ tool_server.py (FastAPI)    │    │ Caido  (proxy:48080)       │  │
  │   │  /execute   /reset_agent   │    │  /graphql  (guest token)   │  │
  │   │  /register_agent  /health  │    │  captures ALL egress traffic │  │
  │   └──────────┬─────────────────┘    └────────────────────────────┘  │
  │              │ asyncio.to_thread(tool_func)                           │
  │   ┌──────────▼──────────────────────────────────────────────────┐    │
  │   │ 35 registered tools (terminal/proxy/file/python/browser/…)  │    │
  │   │ terminal: libtmux, PS1=[STRIX_$?]$ , poll 0.5s, never kills │    │
  │   └─────────────────────────────────────────────────────────────┘    │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Process lifecycle: entry → first LLM call

All paths absolute. Commit `1ab050a`.

| # | Step | Location | Notes |
|---|------|----------|-------|
| 1 | Module import loads default config | `strix/interface/main.py:26` `apply_saved_config()` | Reads `~/.strix/cli-config.json`, injects tracked vars into `os.environ` (force=False, won't override existing env). Records what it set in `Config._applied_from_default`. |
| 2 | Parse args | `main.py:573 → parse_arguments()` (`main.py:266`) | 10 flags (see §3). For each `--target`, `infer_target_type` (`utils.py:1089`) classifies repo/web/local/ip → `targets_info`. `rewrite_localhost_targets` (`utils.py:1254`) maps localhost→`host.docker.internal`. |
| 3 | `--config` override | `main.py:575-576 → apply_config_override` (`main.py:553`) | Clears vars from step 1, sets `Config._config_file_override`, force-loads custom JSON into env. |
| 4 | Docker preflight | `main.py:578 check_docker_installed` + `main.py:579 pull_docker_image` | `pull_docker_image` (`main.py:509`) → `check_docker_connection` (`utils.py:1355`): supports `ssh://` via `use_ssh_client=True` (remote VPS), retries 3× with `time.sleep(2)`. Pulls `ghcr.io/usestrix/strix-sandbox:0.1.13` if absent. |
| 5 | Validate env | `main.py:581 validate_environment` (`main.py:52`) | `STRIX_LLM` must be set, else `sys.exit(1)`. |
| 6 | **First LLM call** | `main.py:582 asyncio.run(warm_up_llm())` (`main.py:206`) | `litellm.completion` with a `test_messages` probe — a health check, not the scan. `validate_llm_response` (`utils.py:1480`). |
| 7 | Persist config | `main.py:584 persist_config` (`main.py:564`) | Only writes back to default file if no `--config` override. |
| 8 | Generate run name | `main.py:586 generate_run_name` (`utils.py:459`) | `slug + secrets.token_hex(4)` → e.g. `pintu-co-id_31f149e7`. Output dir = `strix_runs/<run_name>`. |
| 9 | Clone repos / collect sources | `main.py:588-595` | `clone_repository` (`utils.py:1278`), `collect_local_sources` (`utils.py:1210`) → `args.local_sources` (list of `{source_path, workspace_subdir}`). Empty for blackbox web targets. |
| 10 | Resolve diff scope | `main.py:600-612 resolve_diff_scope_context` (`utils.py:992`) | For repo whitebox: PR-diff scoping. |
| 11 | Branch on mode | `main.py:628-631` | `--non-interactive` → `asyncio.run(run_cli(args))`; else `asyncio.run(run_tui(args))`. |

---

## 3. CLI flags

| Flag | Type | Purpose |
|------|------|---------|
| `-t/--target` | str, repeatable, required | URL / repo / local path / domain / IP |
| `--instruction` | str | Inline custom instructions (mutually exclusive with `--instruction-file`/`--check`) |
| `--instruction-file` | str | Path to instruction file |
| `--check` | str (TASK) | Single-task mode; sets `STRIX_CHECK_MODE=true`, rewrites instruction |
| `-n/--non-interactive` | flag | No TUI; run to completion in CLI |
| `-m/--scan-mode` | `quick\|standard\|deep` (default `deep`) | Scan depth |
| `--scope-mode` | `auto\|diff\|full` (default `auto`) | PR diff scoping |
| `--diff-base` | str | Base git ref for diff |
| `--config` | str | Path to JSON config override |
| `-v/--version` | — | Print version |

---

## 4. Configuration system (`strix/config/config.py`)

`Config` is a class-as-namespace: every lowercase string/None attribute is a
tracked config key. `Config.get(name)` resolves `os.getenv(NAME.upper(), default)`.

- **Defaults** (`config.py:14-58`): `strix_llm=None`, `strix_image="ghcr.io/usestrix/strix-sandbox:0.1.13"`, `strix_runtime_backend="docker"`, `strix_warm_sandbox="true"`, `strix_terminal_backend="tmux"`, `llm_timeout="300"`, `strix_max_output_tokens="2048"`, etc.
- **Tracked vars** (`config.py:76-85`): all lowercase string/None attrs, upcased for env.
- **LLM change detection** (`config.py:88-99`): the 10 canonical LLM vars are tracked separately — if `os.environ` has an LLM var that differs from the saved file, the saved-file LLM vars are dropped (live env wins). Prevents stale-file clobbering a runtime `export STRIX_LLM=...`.
- **File I/O** (`config.py:118-200`): `load`/`save` against `~/.strix/cli-config.json` (chmod 600); `--config` sets `_config_file_override` so reads/writes target the custom path, and the default file is never written back when an override is active.
- **`resolve_llm_config`** (`config.py:211-236`): returns `(model, api_key, api_base)`. `strix/`-prefixed models route to `https://models.strix.ai/api/v1`; otherwise `llm_api_base`/`openai_api_base`/`litellm_base_url`/`ollama_api_base` (first non-None).

---

## 5. Agent core loop (`strix/agents/`)

### 5.1 Entry

`StrixAgent.execute_scan(scan_config)` (`strix/agents/StrixAgent/strix_agent.py:59`) classifies targets into `repositories/local_code/urls/ip_addresses`, builds a `task_description` string (scope + authorization + special instructions), then delegates:

```python
return await self.agent_loop(task=task_description)   # strix_agent.py:151
```

### 5.2 The loop

`BaseAgent.agent_loop(task)` (`strix/agents/base_agent.py:152-260`) is a
`while True:` (not `for step in range`); the iteration counter is manual via
`state.increment_iteration()` (`base_agent.py:184`). `max_iterations=300`
(`base_agent.py:50`).

Per-iteration order:
1. `_check_agent_messages` (`:168`) — inter-agent inbox polling (sub-agent messaging).
2. Waiting-state / stop-condition checks (`:170-182`).
3. `state.increment_iteration()` (`:184`); warn at 85% and again 3 iterations from the cap (`:186-211`).
4. `should_finish = await (create_task(self._process_iteration(tracer)))` (`:214-216`).
5. Handle finish / `CancelledError` / LLM-error / generic-error (`:222-259`).

Termination: `state.should_stop()` (`state.py:96`, i.e. `stop_requested`/`completed`/max iters), or `finish_scan`/`agent_finish` returning `scan_completed=True` (`executor.py:286-293`), or unrecoverable LLM error.

### 5.3 Sandbox init

`_initialize_sandbox_and_state` (`base_agent.py:331-367`):

```python
runtime = get_runtime()
sandbox_info = await runtime.create_sandbox(           # base_agent.py:340
    self.state.agent_id, self.state.sandbox_token, self.local_sources
)
self.state.sandbox_id    = sandbox_info["workspace_id"]      # container id
self.state.sandbox_token = sandbox_info["auth_token"]
self.state.sandbox_info = sandbox_info
self.state.add_message("user", task)                         # seed conversation
```

`SandboxInfo` TypedDict (`strix/runtime/runtime.py:5`): `workspace_id, api_url, auth_token, tool_server_port, caido_port, agent_id`.

### 5.4 One full iteration

1. `conversation_history = state.get_conversation_history()` (`base_agent.py:369`) — direct list ref.
2. `async for response in self.llm.generate(conversation_history)` (`:371`).
   - `_prepare_messages` (`llm.py:272-305`): prepends system prompt, appends an `<agent_identity>` user msg, runs `MemoryCompressor.compress_history` (token budget 100k, keeps 15 recent, summarizes older chunks via an LLM call), adds `<meta>Continue…</meta>` cue if last msg was assistant and non-interactive, adds Anthropic prompt-cache control.
   - `_build_completion_args` (`llm.py:307-336`): `litellm_model`, `messages`, `timeout`, `max_tokens` (default 2048), optional `api_key`/`api_base`, `reasoning_effort` only for non-local reasoning-capable models.
   - `_stream` (`llm.py:218-270`): `await asyncio.wait_for(acompletion(..., stream=True), timeout)`; accumulates `delta.content`; early-yields when `</function>` appears outside a thinking block; on stream end runs `stream_chunk_builder(chunks)` for usage, then `normalize_tool_format` → `fix_incomplete_tool_call` → `_truncate_to_first_function` → `parse_tool_invocations`.
3. `state.add_message("assistant", content, thinking_blocks)` (`base_agent.py:396`); tracer logs chat.
4. `actions = final_response.tool_invocations` (`:405-409`). If empty → inject a corrective user message (`:381-392`) and continue.
5. `should_finish = await self._execute_actions(actions, tracer)` (`:412`).
   - `process_tool_invocations` (`executor.py:320-349`) loops each tool invocation through `_execute_single_tool`.
   - Results are wrapped as XML `<tool_result><tool_name>…</tool_name><result>…</result></tool_result>` (`executor.py:258-261`) and appended to `conversation_history` as a `user` message (`:344-347`) — this is the feedback to the LLM.
6. Loop. Next iteration the LLM sees the tool result as context.

### 5.5 State

`AgentState` (`strix/agents/state.py:13-186`): `agent_id` (`agent_<8hex>` via `uuid`), `parent_id` (None = root), `sandbox_id`/`sandbox_token`/`sandbox_info`, `task`, `iteration`/`max_iterations`, `completed`/`stop_requested`/`waiting_for_input`/`llm_failed`, `messages` (conversation history, each `{role, content, thinking_blocks?}`), `actions_taken`/`observations` (with iteration/timestamp), `errors`, plus a private `_wake_event: asyncio.Event` for interactive resume.

Findings (vulnerability reports) are **not** stored in state — they live on the `Tracer`, added by the `create_vulnerability_report` tool (`strix/tools/reporting/reporting_actions.py:202`), which dedupes via an LLM call (`strix/llm/dedupe.py:142`).

### 5.6 Memory / compaction

`MemoryCompressor` (`strix/llm/memory_compressor.py`): `MAX_TOTAL_TOKENS=100_000`, `MIN_RECENT_MESSAGES=15`. `compress_history` (`:166-226`) splits system vs regular messages, keeps the last 15 regular messages, and if total tokens exceed 90% of budget, summarizes older messages in chunks of 10 via a non-streaming `litellm.completion`. Mutates `state.messages` in place (`llm.py:295-296`).

---

## 6. LLM integration (`strix/llm/`)

- **Wrapper**: `strix/llm/llm.py:97 class LLM`. `generate()` (`:201-216`) is an `AsyncIterator[LLMResponse]` with a retry loop (`strix_llm_max_retries`, default 5; backoff `min(90, 2*2**attempt)`).
- **Streaming**: always streamed via `acompletion(..., stream=True)` (`llm.py:225`). Per-chunk timeout `max(timeout,120)` (`:234`) protects slow local thinking models.
- **Tool-call format — NOT native function-calling**: tool schemas are rendered to XML and injected into the **system prompt** by `registry.get_tools_prompt()` (`strix/tools/registry.py:280-300`), grouped by module (`<terminal_tools>…<tool name="terminal_execute">…</tool>…</terminal_tools>`). The system-prompt Jinja embeds this via `{{ get_tools_prompt() }}` (`strix/agents/StrixAgent/system_prompt.jinja`). No `tools=` array is passed to litellm.
- **Parsing**: the model is told to emit `<function=name><parameter=p>value</parameter></function>`. `parse_tool_invocations` (`strix/llm/utils.py:90-130`) regexes it out (with `normalize_tool_format` (`:14-41`) handling `<invoke name=…>`, quoted variants, and JSON fallback). `_truncate_to_first_function` keeps only the first tool call.
- **Reasoning effort**: `reasoning_effort` added only if `_supports_reasoning()` and the model is non-local (`127.0.0.1`/`localhost`/`ollama` excluded — local models skip it) (`llm.py:328-334`).
- **Pydantic serialization warnings** (the `Message`/`StreamingChoices` warnings seen in logs): litellm 1.81.x stores provider-specific fields (`reasoning_content`, `thinking_blocks`, `provider_specific_fields`) as Pydantic-extras. When `stream_chunk_builder` rebuilds the final `ModelResponse` and `completion_cost` serializes it, Pydantic emits `PydanticSerializationUnexpectedValue`. Harmless — values are still accessible via `getattr` — and signals a litellm-version vs provider-extension mismatch. `strix/llm/__init__.py:16-19` disables litellm debug logging and `asyncio` warnings but not `PydanticSerializationWarning`.

---

## 7. Docker sandbox runtime (`strix/runtime/`)

### 7.1 Factory

`strix/runtime/__init__.py:18` `get_runtime()` reads `strix_runtime_backend` (`:21`, default `docker`), and at `:23` hardcodes `if runtime_backend == "docker":` → lazy singleton `DockerRuntime()`. Any other value raises `ValueError`. `cleanup_runtime()` (`:35`) calls `runtime.cleanup()` then nulls the global.

### 7.2 `DockerRuntime.__init__` (`docker_runtime.py:29-63`)

- Reads `docker_host` from Config, propagates to `os.environ["DOCKER_HOST"]`.
- `self.client = docker.from_env(timeout=60)` (`:36`).
- Cold-mode fields (`:43-46`): `_scan_container`, `_tool_server_port`, `_tool_server_token`, `_caido_port`.
- Warm-mode fields (`:51-58`):
  - `self._warm = Config.get("strix_warm_sandbox") in {true,1,yes}` (default **on**)
  - `self._warm_scan_ids: set[str]` — intra-scan double-copy guard, **not** cross-scan dedup
  - `self._current_caido_project_id` — for deletion on next scan
- `:60-63` if warm: `atexit.register(self.shutdown)`.

### 7.3 `create_sandbox(agent_id, existing_token, local_sources)` (`docker_runtime.py:284-342`)

1. `scan_id = self._get_scan_id(agent_id)` (`:290`) — from `tracer.scan_config["scan_id"]` else `scan-<agent prefix>`.
2. `container = self._get_or_create_container(scan_id)` (`:291`).
3. **Warm reset** (`:297-299`): if `self._warm and scan_id not in self._warm_scan_ids`, run `_warm_reset(...)` then `self._warm_scan_ids.add(scan_id)`.
4. `should_copy_sources = scan_id not in self._warm_scan_ids or not self._warm` (`:304`); if `local_sources and should_copy_sources`, copy each source + the `skills/` resource dir into `/workspace` via tar (`:305-317`).
5. `token = existing_token or self._tool_server_token` (`:326`); resolve `api_url = http://{host}:{tool_server_port}` (`:330-331`).
6. `await self._register_agent(api_url, agent_id, token)` (`:333`).
7. Return `SandboxInfo` (`:335-342`).

### 7.4 Container naming & creation

- `_container_name(scan_id)` (`:133`): `"strix-scan-warm"` if warm else `"strix-scan-{scan_id}"`.
- `_get_or_create_container` (`:209-254`): reuse cached `_scan_container` → get by name (start + `time.sleep(2)` if stopped, `_recover_container_state`) → label search → else `_create_container`.
- `_create_container` (`:143-207`): remove stale same-name container (`:152-159`), allocate two random host ports (`_find_available_port`), `secrets.token_urlsafe(32)` for the tool-server token (`:163`), `containers.run(image, command="sleep infinity", detach=True, ports={48081/tcp: tool_port, 48080/tcp: caido_port}, cap_add=["NET_ADMIN","NET_RAW"], labels={"strix-scan-id": …}, environment={TOOL_SERVER_PORT, TOOL_SERVER_TOKEN, STRIX_SANDBOX_EXECUTION_TIMEOUT, HOST_GATEWAY}, extra_hosts={host.docker.internal: host-gateway}, tty=True)`, then `_wait_for_tool_server`.
- `_wait_for_tool_server` (`:109-131`): `time.sleep(5)` initial, then up to 30 health polls with exponential backoff (cap 5s). **Worst case ≈ 142.5s** before `SandboxInitializationError`. This is the 30-60s+ cold-start overhead that warm mode eliminates.

### 7.5 Teardown — `cleanup` vs `destroy_sandbox` vs `shutdown`

| Method | Cold | Warm | Caller |
|---|---|---|---|
| `cleanup()` (`:389`) | async `docker rm -f` (fire-and-forget) | **no-op** (clears cached refs, container survives) | `cleanup_runtime()` ← `cli.py:112-115` / `tui.py:857-861` atexit + signal handlers |
| `destroy_sandbox(id)` (`:375`) | sync stop+remove | (comment notes warm uses `shutdown`) | **Dead code** — kept for the `AbstractRuntime` interface |
| `shutdown()` (`:419`) | (registered only when warm) | blocking `docker rm -f` (timeout 30), clears `warm_scan_ids` + `current_caido_project_id` | `atexit.register(self.shutdown)` (`:63`), warm only |

---

## 8. Warm mode (`strix_warm_sandbox=true`) — the speed win

One long-lived container `strix-scan-warm` is reused across scans. Container
creation (image verify + entrypoint bootstrap + `_wait_for_tool_server`,
~30-60s+) happens **once**; subsequent scans skip it and only run a per-scan
reset (~1s of HTTP).

### 8.1 Per-scan reset sequence (`_warm_reset`, `docker_runtime.py:445-499`)

1. **No-op guard** (`:468-469`): `if not self._warm_scan_ids: return`. The first scan in a warm container has nothing to reset (bootstrap already produced clean state). This also avoids hitting Caido/tool-server endpoints before they are ready — it is the fix for the scan-1 "Initializing" hang.
2. **Step 1 — cancel prev agent** (`:479`): `_call_reset_agent` → `POST /reset_agent` (best-effort, catches `httpx.HTTPError` incl. 404 from a stale image and warns instead of raising, `:501-524`).
3. **Step 2 — wipe `/workspace`** (`:484-492`): `find /workspace -mindepth 1 -delete` + `chown -R pentester:pentester /workspace && chmod -R 755 /workspace` (keeps the dir, it's WORKDIR).
4. **Step 3 — fresh Caido project** (`:497-499`): `_switch_caido_project` (`:526-598`) — `loginAsGuest` (fresh guest token, sidesteps TTL) → optional `deleteProject(prev_id)` → `createProject(temporary:true, name:"scan-{scan_id}")` → `selectProject(id)`. `selectProject` is the isolation mechanism: proxy captures land in the new project.

### 8.2 Second-scan speed path

`_get_or_create_container` finds existing `strix-scan-warm` by name (running, recovers ports/token) → no image pull, no bootstrap, no 5s+142s wait. `_warm_reset` runs steps 1-3 (~1s). Same `tool_server_port`/`caido_port`/token reused. Net: scan N≥2 starts in **<5s** vs 30-60s cold.

### 8.3 Known issue — warm-mode first-scan source copy

⚠️ At `docker_runtime.py:297-304`, the order is:

```python
if self._warm and scan_id not in self._warm_scan_ids:
    await self._warm_reset(...)          # :298
    self._warm_scan_ids.add(scan_id)    # :299  ← adds BEFORE the check at :304
should_copy_sources = scan_id not in self._warm_scan_ids or not self._warm  # :304
if local_sources and should_copy_sources: # :305  ← False on warm first scan
```

Trace for warm first scan with `local_sources`: `:297` True → `:298` `_warm_reset` (no-op via `:468`) → `:299` set becomes `{scan_id}` → `:304` `should_copy_sources = (scan_id not in {scan_id}) or (not True)` = `False or False` = **False** → `:305` does not copy sources or skills.

- **Impact**: in warm mode, the first scan's `local_sources` (whitebox/local-code/cloned-repo targets) are not copied into `/workspace`. Cold mode is unaffected (`not self._warm`=True).
- **Does not affect** blackbox web scans (e.g. `http://pintu.co.id`) — `local_sources` is empty there, so the branch is moot.
- **Fix**: move `self._warm_scan_ids.add(scan_id)` to **after** the source-copy block, or guard source copy with a separate "already copied this scan" flag. Not applied here per the user's request to document first.

---

## 9. Tool execution layer

### 9.1 Host → container HTTP (`strix/tools/executor.py`)

`execute_tool` (`:30-36`): if `should_execute_in_sandbox(tool_name)` and sandbox active → `_execute_tool_in_sandbox` (`:39-98`), else `_execute_tool_locally`.

`_execute_tool_in_sandbox`:
- `server_url = await runtime.get_sandbox_url(sandbox_id, sandbox_info["tool_server_port"])` (`:57-58`)
- `request_url = f"{server_url}/execute"` (`:59`)
- `httpx.AsyncClient(trust_env=False)` POST `{agent_id, tool_name, kwargs}` with `Authorization: Bearer {sandbox_token}` (`:69-84`), timeout = sandbox exec timeout + 30s, connect 10s.
- Maps `error` field / 401 / HTTP errors to `RuntimeError` (`:85-94`).

Tools with `sandbox_execution=False` run **locally on the host**: `finish_scan`, `create_vulnerability_report`, `web_search`. Everything else runs in the container.

### 9.2 In-container server (`strix/runtime/tool_server.py`)

FastAPI app, `STRIX_SANDBOX_MODE=true` guard (`:16-18`), `HTTPBearer` auth against `EXPECTED_TOKEN` (the `--token` arg). Started by `docker-entrypoint.sh:161-166` as `pentester` via uvicorn.

| Endpoint | Purpose |
|---|---|
| `POST /execute` (`:86-127`) | Verify token; cancel any in-flight task for the same `agent_id` (`:94-97`); `asyncio.create_task(wait_for(_run_tool, REQUEST_TIMEOUT))`; `_run_tool` does `set_current_agent_id` → `get_tool_by_name` → `convert_arguments` → `asyncio.to_thread(tool_func)`. Returns `{result}` or `{error}`. |
| `POST /register_agent?agent_id=` (`:130-135`) | No-op; validates token. |
| `POST /reset_agent?agent_id=` (`:138-166`) | Warm-reset: `agent_tasks.pop(agent_id).cancel()` + `get_terminal_manager().cleanup_agent(agent_id)` (best-effort). |
| `GET /health` (`:169-178`) | `{status:"healthy", sandbox_mode, auth_configured, active_agents, agents}`. |

### 9.3 Registry & schemas (`strix/tools/registry.py`)

- `@register_tool(sandbox_execution=True, requires_browser_mode=False, requires_web_search_mode=False)` decorator (`:190-251`): filters by env (sandbox mode skips non-sandbox tools; browser/web-search gated), loads the tool's XML schema side-by-side (`*_schema.xml` via `_load_xml_schema` `:47-87`, parsed with `defusedxml`), stores in `tools`/`_tools_by_name`/`_tool_param_schemas`.
- `get_tools_prompt()` (`:280-300`) concatenates XML schemas grouped by module for the system prompt.
- `should_execute_in_sandbox(name)` (`:273-277`) reads the per-tool flag.
- `argument_parser.convert_arguments` (`strix/tools/argument_parser.py:15-89`) coerces LLM-provided string kwargs to the tool function's annotated types (`int/float/bool/list/dict` via `inspect.signature`).

### 9.4 Tool inventory — 35 tools, 11 categories

| Category | Tools | Notes |
|---|---|---|
| **terminal** | `terminal_execute` | libtmux persistent bash; see §10 |
| **proxy** | `list_requests`, `view_request`, `send_request`, `repeat_request`, `scope_rules`, `list_sitemap`, `view_sitemap_entry` | Caido GraphQL; see §11 |
| **file_edit** | `str_replace_editor`, `list_files`, `search_files` | wraps `openhands_aci`; ripgrep search; auto-prefixes `/workspace` |
| **python** | `python_action` | persistent Python REPL sessions |
| **browser** | `browser_action` | Playwright; `requires_browser_mode` |
| **web_search** | `web_search` | Perplexity `sonar-reasoning-pro`; `sandbox_execution=False`, `requires_web_search_mode` |
| **notes** | `create_note`, `list_notes`, `get_note`, `update_note`, `delete_note` | persisted to `notes.jsonl` + markdown |
| **todo** | `create_todo`, `list_todos`, `update_todo`, `mark_todo_done`, `mark_todo_pending`, `delete_todo` | priorities low/normal/high/critical |
| **reporting** | `create_vulnerability_report` | CVSS XML parse, CWE/CVE extract, LLM dedupe, `tracer.add_vulnerability_report`; `sandbox_execution=False` |
| **finish** | `finish_scan` | root-only, finalizes tracer; `sandbox_execution=False` |
| **agents_graph** | `view_agent_graph`, `create_agent`, `send_message_to_agent`, `wait_for_message`, `agent_finish` | sub-agent delegation + inter-agent messaging via `_agent_messages` dict |
| **thinking** | `think` | no-op thought logger |
| **load_skill / run_skill** | `load_skill`, `execute_skill` | skill injection + script execution from `/workspace/skills/custom/` |

---

## 10. Terminal subsystem (`strix/tools/terminal/`)

### 10.1 Manager

`TerminalManager` (`terminal_manager.py:11`) is a module-level singleton (`get_terminal_manager()` `:161-162`). Sessions are keyed by `agent_id` (via the `current_agent_id` `ContextVar`, `strix/tools/context.py`) → `dict[terminal_id, TerminalSession]`. `cleanup_agent(agent_id)` (`:122-128`) pops the whole agent bucket and closes each session — wired into `/reset_agent`.

### 10.2 Session — PS1 trick, poll, never-kill

`TerminalSession.__init__` (`terminal_session.py:32-47`) → `initialize()` (`:57-90`): `libtmux.Server()`, `new_session(start_directory="/workspace", x=120, y=30)`, `history-limit 10000`, a `bash` window, then injects the **exit-code prompt**:

```python
pane.send_keys(f'export PROMPT_COMMAND=\'export PS1="{self.PS1}"\'; export PS2=""')
# where PS1 = r"[STRIX_$?]$ "   (terminal_session.py:49-51)
```

`$?` is expanded by bash into the literal prompt, so each prompt reads `[STRIX_<exitcode>]$ `. The regex `PS1_PATTERN = r"\[STRIX_(\d+)\]"` (`:53-55`) extracts the exit code.

- **`POLL_INTERVAL = 0.5`** (`:28`) — every poll, `capture-pane` is read and diffed.
- **Never kills** — on timeout, `_execute_new_command` (`:309-379`) returns `status:"running"`, `exit_code:None`, stores `prev_output` (the raw pane snapshot) and sets `prev_status=CONTINUE`. The next call (empty command `""` → `_handle_empty_command` `:203-260`, or `is_input=True` → `_handle_input_command` `:262-307`) resumes polling until a new PS1 appears.

### 10.3 Stateful scrollback diff

`_get_command_output` (`:157-172`) diffs by literal prefix removal: `raw.removeprefix(self.prev_output)` then strips the echoed command prefix (`_remove_command_prefix` `:23-24`). `_combine_outputs_between_matches` (`:173-191`) slices the pane content **between** PS1 markers (the command's actual output). `_clear_screen` (`:104-109`: `C-l` + `clear-history`) runs after each completed command so the next diff starts clean.

### 10.4 Special keys & input-to-running-command

`_is_special_key` (`:141-152`) recognizes tmux key names: `C-c`, `^c`, `S-`/`M-`/`F1-12`, `Up/Down/Left/Right/Home/End`, `BSpace/Enter/Escape/Tab/PageDown/...`. Sent via `pane.send_keys(command, enter=not is_special_key and not no_enter)`.

When a long command is running, `status:"running"` acts as a lock: new non-input commands are rejected with "A command is already running. Use is_input=true…"; `is_input=True` or special keys bypass the lock and route to `_handle_input_command`.

### 10.5 Phase 2 hook — Go PTY backend

`config.py:57-58` defines `strix_terminal_backend` (`"tmux"` default, planned `"go"`). **No branching exists yet** — `grep` only hits config. Phase 2 will branch in `TerminalSession.initialize()` (`:57-90`) and swap `_get_pane_content` (`:97`)/`pane.send_keys` for Go HTTP calls, keeping the scrollback/diff/PS1 logic in Python (raw-split approach).

---

## 11. Caido proxy & LLM integration

### 11.1 Caido — three endpoints in-container

| Endpoint | Port | Purpose |
|---|---|---|
| Proxy listener | `48080` | MITM HTTP/HTTPS proxy; CA from `/app/certs/ca.p12` (`docker-entrypoint.sh:12-17`) |
| GraphQL API | `48080/graphql` | `loginAsGuest`, `createProject`, `selectProject`, `requestsByOffset`, `sitemap*`, `scopes` |
| Tool server | `48081` | **Independent** of Caido — FastAPI tool executor. Lets warm reset run without restarting Caido. |

All container egress flows through 48080 because `docker-entrypoint.sh:113-146` writes `http_proxy`/`https_proxy`/`ALL_PROXY`=`http://127.0.0.1:48080` into `/etc/profile.d/proxy.sh`, `/etc/environment`, `/etc/wgetrc`, and sources it into `~/.bashrc`/`~/.zshrc`. Browser trust via `certutil` into NSSDB (`:149-151`).

### 11.2 `ProxyManager` (`strix/tools/proxy/proxy_manager.py`)

- Singleton `get_proxy_manager()` (`:817-824`).
- Token: `auth_token or os.getenv("CAIDO_API_TOKEN")` (`:30`); bootstrap token set by entrypoint (`docker-entrypoint.sh:76`).
- **`_refresh_token()`** (`:32-55`): re-posts `loginAsGuest` for TTL expiry in long-lived warm containers. `_get_client()` (`:57-63`) calls it when `not self.auth_token`, then builds `RequestsHTTPTransport` + `gql.Client(fetch_schema_from_transport=False)`.
- Per-scan project switch also re-fetches a guest token in `_switch_caido_project` (`docker_runtime.py:540-548`).

### 11.3 Proxy tools (7)

`list_requests` (HTTPQL filter, pagination, sort via `requestsByOffset`), `view_request` (base64 raw + regex search), `send_request` (via `self.proxies`), `repeat_request` (replay with modifications), `scope_rules` (CRUD allowlist/denylist), `list_sitemap`/`view_sitemap_entry` (attack-surface tree). See §9.4 table.

### 11.4 LLM call path (recap)

`resolve_llm_config` (`config.py:211`) → `LLMConfig` (`strix/llm/config.py:8-43`) resolves `litellm_model`/`canonical_model` (via `resolve_strix_model`, `strix/llm/utils.py:57-71`) + `api_key`/`api_base`/`timeout`. `LLM.generate` → `_stream` → `acompletion(stream=True)` → accumulate → `parse_tool_invocations`. Tool schemas are textual XML in the system prompt, not litellm's native `tools` array.

---

## 12. End-to-end: one tool call (`terminal_execute("echo hi")`)

```
LLM emits: <function=terminal_execute><parameter=command>echo hi</parameter></function>
  │
  │ base_agent.py:405 parse_tool_invocations (llm/utils.py:90)
  ▼
base_agent._execute_actions → executor.process_tool_invocations (:320)
  │ executor._execute_single_tool (:266) → execute_tool_with_validation (:165)
  │   validate name (registry.get_tool_by_name) + params (get_tool_param_schema)
  ▼
executor.execute_tool (:30) → should_execute_in_sandbox("terminal_execute")=True
  │ → _execute_tool_in_sandbox (:39)
  │   server_url = runtime.get_sandbox_url(sandbox_id, tool_server_port)  # http://host:<port>
  │   POST {agent_id, tool_name:"terminal_execute", kwargs:{command:"echo hi"}}
  │     Authorization: Bearer <sandbox_token>   (httpx, trust_env=False)
  ▼                              [ Docker port 48081 ]
tool_server.py /execute (:86)
  │ verify_token → cancel prev task for agent_id (:94) → asyncio.create_task(wait_for(_run_tool,120))
  ▼
_run_tool (:71): set_current_agent_id → get_tool_by_name → convert_arguments → asyncio.to_thread(tool_func)
  ▼
terminal_manager.execute_command (:27) → TerminalSession.execute (:381)
  │ _get_pane_content (capture-pane) ; snapshot PS1 count
  │ pane.send_keys("echo hi", enter=True)              # tmux injection
  │ loop every 0.5s: capture → count [STRIX_\d+] markers
  │   new marker ⇒ COMPLETED: exit_code=regex group(1)
  │   diff: raw.removeprefix(prev_output), strip "echo hi" prefix ⇒ "hi"
  │   _clear_screen (C-l + clear-history)
  ▼
return {content:"hi", status:"completed", exit_code:0, working_dir:"/workspace"}
  │ wrapped in ToolExecutionResponse{result} → back over HTTP
  ▼
host executor._format_tool_result (:227)  # truncate to STRIX_MAX_TOOL_OUTPUT_CHARS
  │ → "<tool_result><tool_name>terminal_execute</tool_name><result>hi</result></tool_result>"
  │ appended to conversation_history as user message (executor.py:344-347)
  ▼
next iteration: LLM sees the tool result as context
```

---

## 13. File map (key modules)

| Module | File | Role |
|---|---|---|
| Entry | `strix/interface/main.py` | arg parse, config load, docker preflight, warm_up_llm, branch CLI/TUI |
| CLI run | `strix/interface/cli.py` | non-interactive Rich Live panel + agent execute |
| TUI run | `strix/interface/tui.py` | textual app, thread-based agent execution |
| Utilities | `strix/interface/utils.py` | target inference, run-name gen, docker connection, diff scope |
| Config | `strix/config/config.py` | env-backed config store + `resolve_llm_config` |
| Agent | `strix/agents/base_agent.py`, `strix/agents/StrixAgent/strix_agent.py`, `strix/agents/state.py` | core loop, scan entry, state |
| LLM | `strix/llm/llm.py`, `strix/llm/config.py`, `strix/llm/utils.py`, `strix/llm/memory_compressor.py`, `strix/llm/dedupe.py` | streaming wrapper, tool-call parsing, compaction, dedupe |
| Runtime | `strix/runtime/docker_runtime.py`, `strix/runtime/__init__.py`, `strix/runtime/runtime.py` | container lifecycle, factory, contract |
| Tool server | `strix/runtime/tool_server.py` | in-container FastAPI executor |
| Tool exec | `strix/tools/executor.py`, `strix/tools/registry.py`, `strix/tools/argument_parser.py`, `strix/tools/context.py` | host-side dispatch, registration, arg coercion, agent ctx |
| Terminal | `strix/tools/terminal/terminal_session.py`, `terminal_manager.py`, `terminal_actions.py` | libtmux PTY, PS1 trick, scrollback |
| Proxy | `strix/tools/proxy/proxy_manager.py`, `proxy_actions.py` | Caido GraphQL client + tools |
| Container | `containers/Dockerfile`, `containers/docker-entrypoint.sh` | image + bootstrap (Caido, proxy env, tool server) |
| Build | `scripts/docker.sh`, `scripts/build.sh`, `scripts/install.sh`, `Makefile` | image build, install, dev cycle |

---

## 14. Open / deferred

- **Phase 2 — Go PTY sidecar (`sandboxd`)**: not started. Raw-split approach — Go owns raw PTY I/O (`creack/pty`), Python keeps the scrollback/diff/PS1 state machine; `terminal_session.py` swaps libtmux calls for Go HTTP when `STRIX_TERMINAL_BACKEND=go`. Multi-stage Dockerfile adds a Go build stage; `docker-entrypoint.sh` launches `sandboxd` before the tool server. Acceptance: `terminal_execute` returns within ~100ms of completion (not poll-bounded), special keys identical, `STRIX_TERMINAL_BACKEND=tmux` reverts.
- **Warm-mode first-scan source-copy bug** (§8.3): sources not copied on warm first scan when `local_sources` non-empty. Fix: move `warm_scan_ids.add(scan_id)` after the copy block.
- **VPS branch sync**: fix `1ab050a` lives on `feat/warm-sandbox` only; `main` has just the initial commit and the VPS pulled the pre-fix warm-sandbox code. Needs `feat/warm-sandbox` merged to `main` (or pulled on the VPS) + image rebuild + `docker rm -f strix-scan-warm`.
