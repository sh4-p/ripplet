<p align="center">
  <img src="https://raw.githubusercontent.com/sh4-p/ripplet/main/docs/logo.png" alt="Ripplet" width="180">
</p>

<h1 align="center">Ripplet</h1>

<p align="center">
  <b>See the ripple before you make the change.</b><br>
  A precise, local code graph for coding agents.
</p>

<p align="center">
  <a href="https://pypi.org/project/ripplet/"><img src="https://img.shields.io/pypi/v/ripplet" alt="PyPI"></a>
  <img src="https://img.shields.io/pypi/pyversions/ripplet" alt="Python">
  <img src="https://img.shields.io/badge/license-FSL--1.1--ALv2-blue" alt="License">
  <img src="https://img.shields.io/badge/MCP-server-8A2BE2" alt="MCP">
  <a href="https://buymeacoffee.com/neush4"><img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?logo=buymeacoffee&logoColor=black" alt="Buy me a coffee"></a>
</p>

<p align="center">
  <a href="#how-it-compares">Benchmarks</a> ·
  <a href="#languages">Languages</a> ·
  <a href="#using-it-with-agents">Agents</a> ·
  <a href="#change-safety-from-the-command-line">Change safety</a> ·
  <a href="#the-ui">UI</a> ·
  <a href="#license">License</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/sh4-p/ripplet/main/docs/demo/ripplet-demo.gif" alt="ripplet setup, an agent answering an impact question, and the ripplet ui graph" width="860">
</p>

---

**A precise, local code graph for coding agents.** Ripplet indexes a repository
into a call / type / data graph and answers the questions agents otherwise
answer with grep: *who calls this, what breaks if it changes, which lines a
rename touches, which tests to run.* Every answer comes with `file:line`, and
a link the graph cannot prove is labelled as such instead of being guessed.

- **Precise.** Checked call by call against compiler-grade indexes (SCIP):
  zero wrong edges across 12 repositories, 0.92 call-site recall.
- **Built for agents.** MCP server, always-on rules for Claude Code, Codex,
  Gemini CLI, Cursor, Copilot, OpenCode and more, a Claude Code skill, and
  hooks that steer an agent to the graph before it greps.
- **Change safety.** Impact of an edit, a dry-run rename, broken callers after
  an edit, the exact tests that reach a change, PR review, architecture rules.
- **Local.** One SQLite file, no LLM, no cloud, no vector database. Python 3.12+.

```bash
pip install "ripplet[treesitter-multilang]"
ripplet setup .        # index + MCP config + agent rules/skill/hooks, in one step
ripplet ui .           # browse the graph
```

### At a glance

| | Ripplet | Graphify |
|---|---:|---:|
| Callers found (site recall, 258 functions) | **~93%** | ~23% |
| Right file first for a task (hit@1, 389 tasks) | **63.0%** | 44.5% |
| Broken change caught by selected tests | **68-92%** | 10-20% |
| Wrong call edges vs a compiler (12 repos) | **0** | not measured |

Details and per-repository numbers below.

---

## How it compares

Measured on real open-source repositories with harnesses that score every
tool the same way (ground truth: textual call sites, the files real commits
changed, or mutants a test suite kills). Graphify 0.9.92 was run with its
defaults (`graphify extract --code-only`, `graphify query`).

### Finding every caller of a function

Site recall = share of the real call sites (`file:line`) the tool reports.

| Set | Functions | Ripplet | Graphify |
|---|---:|---:|---:|
| fresh (13 repos: TS, Java, C#, Go, PHP, Python, Rust, Lua) | 150 | **93%** | 23% |
| dev (7 repos: Java, Lua, Python, TS, C#) | 67 | **96%** | 29% |
| extra (9 repos: Lua, Ruby, Rust) | 41 | **89%** | 16% |

Per language, all three sets: Java 99% vs 66%, TypeScript 91% vs 14%, Python 96% vs 29%,
Go 98% vs 48%, C# 93% vs 11%, Lua 94% vs 6%, Rust 90% vs 56%, Ruby 86% vs 25%, PHP 100% vs 79%.

### Finding where a task should be done

The commit subject of a real later commit is the task; the files that commit
changed are the answer. `task_context` vs `graphify query`.

hit@1 = the first file named is one the commit changed; hit@5 = one of the first five.
389 tasks on 13 repositories (Python, Java, Go, TypeScript, PHP); the last 9 were added later as
hold-out sets, to catch tuning that only fits the first four.

| Repository | Tasks | hit@1 Ripplet | hit@1 Graphify | hit@5 Ripplet | hit@5 Graphify |
|---|---:|---:|---:|---:|---:|
| httpx (Python) | 38 | **53%** | 34% | **87%** | 76% |
| marshmallow (Python) | 29 | **76%** | 41% | **90%** | 83% |
| jsoup (Java) | 40 | **60%** | 30% | **78%** | 55% |
| chi (Go) | 40 | **62%** | 50% | 80% | 80% |
| attrs (Python) | 15 | **80%** | 33% | **87%** | 53% |
| zustand (TypeScript) | 11 | 91% | 91% | 91% | **100%** |
| gson (Java) | 39 | **46%** | 36% | **79%** | 49% |
| cobra (Go) | 40 | **82%** | 52% | **100%** | 88% |
| monolog (PHP) | 40 | **68%** | 55% | **78%** | 70% |
| javapoet (Java) | 37 | 70% | 70% | **95%** | 84% |
| commons-text (Java) | 18 | **78%** | 72% | **94%** | 83% |
| commons-csv (Java) | 7 | **71%** | 14% | 86% | 86% |
| JsonPath (Java) | 35 | **26%** | 11% | **60%** | 34% |
| **all** | **389** | **63.0%** | 44.5% | **83.8%** | 69.9% |

Average answer size: about 1,500 tokens.

### Picking the tests to run after a change

Mutation test selection: a mutant is caught when one of the selected tests
fails on it.

| Repository | Mutants | Ripplet | Graphify | grep |
|---|---:|---:|---:|---:|
| click (Python, 578 tests) | 40 | **68%** caught | 10% | 30% |
| conduit (C#, 38 tests) | 25 | **92%** caught | 20% | 52% |

Ripplet selects the tests that reach the changed symbol through calls,
instantiation and inheritance, so a broken change is far more likely to fail
a selected test; it also selects more tests (43% of click's suite, 16% of conduit's).

### Precision against a compiler

Ripplet's call edges compared with SCIP indexes (scip-typescript, scip-python,
rust-analyzer, scip-go, scip-dotnet, scip-java) — every call site the
compiler resolves, and every edge Ripplet claims:

| Repository | Language | Recall | Wrong edges |
|---|---|---:|---:|
| hono | TypeScript | 0.955 | 0 |
| h3 | TypeScript | 0.978 | 0 |
| axios | JavaScript | 1.000 | 0 |
| starlette | Python | 0.954 | 0 |
| requests | Python | 0.974 | 0 |
| ripgrep | Rust | 0.938 | 0 |
| axum | Rust | 0.839 | 0 |
| eShopOnWeb | C# | 0.920 | 0 |
| gin | Go | 0.959 | 0 |
| echo | Go | 0.940 | 0 |
| spring-petclinic | Java | 0.962 | 0 |
| commons-lang | Java | 0.905 | 0 |
| **all 12** | | **0.923** of 77,408 call sites | **0** |

A call Ripplet cannot prove (an untyped receiver whose method name exists in
the project) is not turned into an edge. Agents see it in an `UNVERIFIED`
section: *possible caller, check the line*.

### With a coding agent

Claude Haiku 4.5, headless, 13 repositories in 13 languages; the same tasks
with and without Ripplet (without = no MCP server, no rules, no hooks).

| Task | With Ripplet | Without |
|---|---|---|
| Rename a function everywhere (complete, compiles) | **13 / 13** | 12 / 13 |
| … cost / turns for all 13 | **$0.81 / 85** | $1.82 / 420 |
| List every caller (recall) | 0.985 | 0.995 |
| Files read or grepped (both tasks, all repos) | **14** | 229 |

Renames are as reliable at less than half the cost and a fifth of the turns.
Caller lists are about as complete; where the graph has a gap, the agent is
told so (UNVERIFIED) rather than given a confident wrong answer.

The gain is in changes (renames, impact, the tests to run). Exploring tasks cost
about the same with or without Ripplet, and not less: on 8 tasks in 4 small
repositories (explain how X works, add a small helper), $1.31 with and $1.25
without; on 4 tasks in two large ones (Exposed, Kotlin, 5.9k files; BookStack,
PHP), $0.99 with and $0.85 without. One run each, so a difference under ~20% is
noise, and the answers were not scored for quality. What decides the cost is the
number of turns, not the size of the answers, so Ripplet batches (`explain_symbol`
takes several names) and can add the task's context before the first turn
(`--prefetch` below).

---

## Languages

Deep analysis (symbols, imports, calls with receiver types, inheritance,
overrides) for:

| Language | Notes |
|---|---|
| Python | AST; imports, `__all__`, properties, `__call__`, typed attributes |
| TypeScript / JavaScript | tree-sitter; namespaces, re-export barrels, workspaces, callbacks |
| Java, Kotlin | packages (also across modules and source sets), nested/anonymous classes, overloads, extension functions, generic supertypes, negated calls |
| C# | usings, generics, extension methods, local functions, `await Task<T>` |
| Go | packages, interfaces, embedding, build tags |
| Rust | modules, re-exports, workspaces, traits, macros |
| PHP | namespaces, Laravel container/facades, Blade views, docblock types |
| Ruby | modules, mixins, Rails conventions |
| C / C++ | includes, namespaces, templates, `#if` branches, macro bodies |
| Swift | modules, extensions, closures typed by their callee |
| Dart, Lua, GDScript | imports / requires, classes and modules |

Also indexed: SQL schemas and code migrations (Laravel, Rails, Django, Alembic,
EF Core, Prisma) as tables and columns, HTTP routes and the client calls that
hit them across languages, Docker Compose, Kubernetes, CI workflows, Terraform,
Markdown docs, config files.

---

## Using it with agents

```bash
ripplet setup .                 # init, index, .mcp.json / opencode.json, rules, skill, hooks, guard
ripplet rules on . --target all # rules for every agent below
ripplet rules on . --strict     # block the first raw read/grep until a Ripplet tool is used
ripplet rules on . --prefetch   # Claude Code: add the task's Ripplet context to each request before the agent starts
```

| Agent | Always-on guidance | Enforcement |
|---|---|---|
| Claude Code | `CLAUDE.md` + skill `.claude/skills/ripplet/SKILL.md` | PreToolUse / PostToolUse hooks |
| Codebuddy | Claude-style | same hooks |
| OpenCode | `AGENTS.md` | plugin `.opencode/plugins/ripplet.js` |
| Gemini CLI | `GEMINI.md` | BeforeTool hook |
| Codex, Hermes, Amp, Devin, Jules | `AGENTS.md` | |
| Cursor | `.cursor/rules/ripplet.mdc` | |
| GitHub Copilot | `.github/copilot-instructions.md` | |
| Kiro, Windsurf, Antigravity, Cline | their rules folders | |

**Prefetch (Claude Code, optional).** `ripplet rules on . --prefetch` adds a
`UserPromptSubmit` hook: the request goes through `task_context`, and when it names
code of the repository or the match is strong, the answer is added to the request
before the agent starts (no tool call, no turn; about 1.6 s). On 389 real tasks it
adds context for 40% of them, and the right file is among the first five in 95.5%
of those; weaker matches add nothing. It is off by default because, measured with
Haiku, it did not change cost. `RIPPLET_PREFETCH=0` switches it off for a session.

This repository ships the same guidance as files you can copy:
[`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md), [`GEMINI.md`](GEMINI.md) and the
Claude Code skill [`skills/ripplet/SKILL.md`](skills/ripplet/SKILL.md).

### MCP tools

`ripplet mcp` serves these over stdio (`ripplet tool <name> key=value` runs any
of them from the shell):

| Tool | Use it for |
|---|---|
| `task_context` | the start of a task: the code it is about, callers (proven, and possible ones marked as such), routes / tables, the feature to imitate, files to edit, tests |
| `repo_map` | an unfamiliar repository: entry points, most-used files and symbols |
| `explain_symbol` | a known name, or several at once (`symbols=[...]`): source, every caller and callee with the call's code |
| `get_neighbors`, `analyze_change_impact` | who calls it / what breaks, with every call line; names work as well as ids, and a symbol nothing calls says so |
| `change_plan`, `rename_symbol` | renames and signature changes: every line to edit, same-named code to leave alone |
| `investigate_error` | a stack trace or log line mapped to functions, code and callers |
| `check_change` | after editing: broken callers, leftovers, the exact tests to run (and a note when your test folder is not in the index) |
| `list_routes` | routes, their handlers and the permission checks they make |
| `feature_slice` | building something like an existing feature: all its touch points |
| `find_related_tests`, `find_path`, `search_symbols`, `query_graph` | the rest |

Answers fit a token budget and say how to get more; read tools re-index
changed files first, so line numbers follow the agent's edits. A name that is
not in the index returns "not found" with the closest names, never an empty
list that reads as "no callers".

---

## Change safety from the command line

```bash
ripplet impact --symbol Store.save                  # what depends on it
ripplet tool change_plan symbol=Store.save change=rename new_name=persist
ripplet check . --run-tests                         # broken callers + the tests that reach the change
ripplet review . --base origin/main                 # PR review (Markdown or --format sarif)
ripplet arch                                        # architecture rules in ripplet.arch.yaml
ripplet test-plan --working-tree                    # tests to run, with the evidence for each
```

Architecture rules are checked on the graph, not on text:

```yaml
rules:
  - id: writes-need-permission
    must_call: [requireRole, requireNotViewer]
    for: {path: "app/Controllers/*.php", type: [method], public: true, name: "^(store|update|delete)"}
    depth: 3
  - id: ui-not-db
    forbid: {from: "src/ui/**", to: "src/db/**"}
```

---

## The UI

`ripplet ui .` opens a local, read-only explorer:

- **Map** of module dependencies, **Explore** a symbol's neighbourhood (graph or
  list, large neighbourhoods grouped by file), **Impact** ring by ring with the
  tests that reach it, **Changes** of your working tree, **Path** between two symbols.
- **Routes** with their handlers, **Data**: tables, columns and the queries on
  them (SELECT / INSERT / UPDATE / DELETE), **Index health**: coverage per
  language, stale files, agent setup.
- Every caller with the call's code, proven vs inferred links, possible
  (unverified) callers, and a dry-run **rename preview**.

---

## Install

```bash
pip install ripplet                              # Python, Markdown, config, SQL
pip install "ripplet[treesitter-multilang]"      # + every language above
pip install "ripplet[watch]"                     # ripplet watch: re-index on save
```

```bash
ripplet index .                 # build / update the graph (incremental)
ripplet search Store            # find symbols
ripplet explain Store.save      # source, callers, callees
ripplet status                  # what is indexed
```

Optional local semantic search with Ollama (`ripplet embed --model
qwen3-embedding:0.6b`, then `ripplet query ... --mode hybrid_full`); everything
else works without a model.

## Limitations

- Static analysis: calls made through reflection, string-built names or
  dependency injection containers configured at runtime are not followed
  (Laravel's `app(X::class)` is).
- Dynamic languages (Ruby, Lua, PHP without types) have more UNVERIFIED sites
  than typed ones; Ripplet reports them instead of guessing.
- Generated code that only exists after a build is not indexed.
- Folders your `.gitignore` excludes (often `tests/`) are not indexed;
  `check_change` says so instead of reporting "0 tests".
- A name that only appears as a string (a dispatcher branching on
  `"explain_symbol"`) is not a call edge; `task_context` points to the function
  that holds the string.
- Single SQLite file per workspace.

## License

Ripplet is **source-available** under the
[Functional Source License 1.1, Apache 2.0 future license](LICENSE) (FSL-1.1-ALv2).

- **Free to use** for anything except a competing product: personal projects,
  work, commercial companies, internal tools, CI, education, research.
- **Free to change**, and to share your changes, as long as the license and
  copyright notices stay.
- **Not allowed:** offering Ripplet, or a copy or fork of it, as a commercial
  product or service that competes with it.
- **Becomes Apache 2.0** two years after each release.
- The name "Ripplet" and its logo are trademarks and are not licensed.

For a commercial or team license, contact the author.

## Support

Ripplet is built by one person. If it saved you a few greps (or a broken
release), you can [buy me a coffee](https://buymeacoffee.com/neush4).

## Author

Mehmet Emin Madanoglu
