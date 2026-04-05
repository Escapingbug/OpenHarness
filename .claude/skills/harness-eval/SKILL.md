---
name: harness-eval
description: This skill should be used when the user asks to "test the harness", "run integration tests", "validate features with real API", "test with real model calls", "run agent loop tests", "verify end-to-end", or needs to verify OpenHarness features on a real codebase with actual LLM calls.
version: 0.1.0
---

# Harness Eval — End-to-End Feature Validation

Validate OpenHarness features by running real agent loops against an unfamiliar codebase with actual LLM API calls. Every test exercises the full stack: API client → model → tool calls → execution → result.

## Core Principles

1. **Test on an unfamiliar project** — never test on OpenHarness itself (the agent modifies its own code). Clone a real project as the workspace.
2. **Use real API calls** — no mocks. Configure a real LLM endpoint.
3. **Multi-turn conversations** — always test 2+ turns where the model needs prior context.
4. **Combine features** — test hooks+skills+agent loop together, not in isolation.
5. **Verify tool execution** — inspect tool call lists and output files, not just model text.

## Workflow

### 1. Prepare Workspace

Clone an unfamiliar project (do not use OpenHarness):

```bash
git clone https://github.com/HKUDS/AutoAgent /tmp/eval-workspace
```

### 2. Configure Environment

```bash
export ANTHROPIC_API_KEY=sk-xxx
export ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic  # or any provider
export ANTHROPIC_MODEL=kimi-k2.5
```

### 3. Design Tests

Each test follows this pattern:

```python
engine = make_engine(system_prompt="...", cwd=UNFAMILIAR_PROJECT)
evs1 = [ev async for ev in engine.submit_message("Read X, analyze Y")]
r1 = collect(evs1)  # text, tools, turns, tokens
evs2 = [ev async for ev in engine.submit_message("Based on what you found...")]
r2 = collect(evs2)
assert "grep" in r1["tools"]  # verify tools ran
```

For detailed code templates and the `make_engine`/`collect` helpers, consult `references/test-patterns.md`.

### 4. Run Tests

```bash
python tests/test_merged_prs_on_autoagent.py   # PR feature tests
python tests/test_real_large_tasks.py           # large multi-step tasks
python tests/test_hooks_skills_plugins_real.py  # hooks/skills/plugins
python -m pytest tests/ -q -k "not autoagent"  # unit tests (no API)
```

### 5. Interpret Results

| Result | Meaning | Action |
|--------|---------|--------|
| PASS with tool calls | Feature works end-to-end | Done |
| PASS without tool calls | Model answered from knowledge | Rewrite prompt to force tool use |
| FAIL with exception | Code bug | Read traceback |
| FAIL with wrong output | Model behavior issue | Check system prompt and tool schemas |
| Timeout | Task too complex | Increase `max_turns` or simplify prompt |

## Feature Coverage Checklist

- [ ] Engine: multi-turn memory, tool chaining, parallel tools, error recovery, auto-compaction
- [ ] Swarm: InProcessBackend lifecycle, concurrent teammates, coordinator+notifications
- [ ] Hooks: pre_tool_use blocking → model adapts, post_tool_use firing
- [ ] Skills: skill tool invocation → model follows instructions
- [ ] Plugins: plugin-provided skill loaded and used in agent loop
- [ ] Memory: YAML frontmatter parsing, body content search, context injection
- [ ] Session: save → load → resume with context preserved
- [ ] Providers: Anthropic client, OpenAI client (with reasoning_content), multi-turn
- [ ] Cost: token accumulation across turns

## Common Pitfalls

- Testing on OpenHarness itself — agent modifies its own running code
- Using mocks — misses serialization and API compatibility bugs
- Single-turn only — misses context accumulation and compaction bugs
- Not checking tool call list — model may claim tool use without calling it
- Hardcoding paths — use `WORKSPACE` variable, skip in CI with `pytest.mark.skipif`

## Additional Resources

### Reference Files

- **`references/test-patterns.md`** — Complete code templates for `make_engine`, `collect`, and each feature category
- **`references/feature-matrix.md`** — Detailed test cases for every OpenHarness module

### Existing Test Files

Working test suites in the repo:
- `tests/test_merged_prs_on_autoagent.py` — PR feature validation
- `tests/test_real_large_tasks.py` — Large multi-step tasks
- `tests/test_hooks_skills_plugins_real.py` — Hooks/skills/plugins in agent loops
- `tests/test_untested_features.py` — Module-level integration tests
