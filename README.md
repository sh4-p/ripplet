<p align="center">
  <img src="https://raw.githubusercontent.com/sh4-p/ripplet/main/docs/logo.png" alt="Ripplet" width="180">
</p>

<h1 align="center">Ripplet</h1>

<p align="center">
  <b>See the ripple before you make the change.</b><br>
  A precise, local code graph for coding agents.
</p>

<p align="center">
  <a href="https://pypi.org/project/ripplet/"><img src="https://img.shields.io/pypi/v/ripplet?label=PyPI&cacheSeconds=600" alt="PyPI"></a>
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
| h3 | TypeScript | 0.986 | 0 |
| axios | JavaScript | 1.000 | 0 |
| starlette | Python | 0.964 | 0 |
| requests | Python | 0.974 | 0 |
| ripgrep | Rust | 0.938 | 0 |
| axum | Rust | 0.852 | 0 |
| eShopOnWeb | C# | 0.920 | 0 |
| gin | Go | 0.959 | 0 |
| echo | Go | 0.940 | 0 |
| spring-petclinic | Java | 0.962 | 0 |
| commons-lang | Java | 0.905 | 0 |
| **all 12** | | **0.925** of 77,907 call sites | **0** |

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

The gain is in changes (renames, impact, the tests to run). Exploring and building
cost about the same with or without Ripplet. Five "add a small feature" tasks in
five repositories (Python, Go, TypeScript, Java, Kotlin), each starting from the same
commit, one run each: $1.10 without Ripplet, $1.00 with it available (the agent
decides when to use it). A difference under ~20% is noise. What decides the cost is
the number of turns, not the size of the answers, so Ripplet batches (`explain_symbol`
takes several names) and can add the task's context before the first turn
(`--prefetch` below).

Forcing the use does not help. An experimental profile (`RIPPLET_MCP_TOOLS=sandbox`)
adds `read_code`, `edit_code` and `write_code`, so code can be read and edited
without leaving Ripplet. With the agent's own Read/Grep/Edit switched off the same
five tasks cost $1.54 (+40%): it still reads the code it edits, and every call pays
the tool's overhead. The edits themselves worked (code changed in 5 of 5). It stays
opt-in.

Adding a parameter is a multi-site edit too, so `change_signature` does it in one call
(declaration and every call site, `dry_run` to preview). Same task on four repositories (Python,
Go, Java, Kotlin), one run each, checked from the working tree: $0.20 and 15 turns with the tool
against $1.42 and 159 turns with the agent's own edits, and 70 of 70 call sites changed in both.
(An earlier run missed two Kotlin sites because the parser gave up on a `$$".."` string, and in Python
a bare value after a keyword argument was a syntax error that sent the agent back to hand editing;
both are fixed.) Sites the graph cannot prove are listed, not edited. Single runs: read it as a
direction, not a precise figure.
Reordering parameters (`reorder_params`) on one Python task, swapping the first two parameters of
`merge_setting` in requests and all 9 call sites: $0.04 and 3 turns with the tool against $0.16 and 18 turns
without, both ending with the same file. One run each.
Moving a function to a new module (`guess_filename` in requests, with its imports and two importers): $0.09 and 11 turns
with `move_symbol` against $0.23 and 28 turns without; both ended with working imports and passing tests. One run each.
TypeScript and JavaScript (ES modules) are moved the same way, from tree-sitter syntax trees: imports of the moved code
travel with it, `import {x} from './old'` and `export {x} from './old'` elsewhere are rewritten (`.js`/`.ts`/no-extension
style kept), imports the old module no longer uses are dropped, and a barrel (`export *`), namespace import or
`require`/`import()` keeps a re-export. It refuses CommonJS, default exports, a `declare module` block that re-opens the
name, assignment to a moved `let`, and path-alias imports it cannot resolve are listed. Checked with `tsc`
(errors compared with the untouched repo) on sampled exports with `with_dependencies`: h3 80 samples, 75 moved with no new
error, 5 refused; hono 60 samples, 52 moved with no new error, 8 refused (the grammar misreads a few generic call signatures).
One TypeScript run (`getURLPathname` in h3, four importers, a new module): $0.17 and 17 turns with `move_symbol` against $0.21 and 25 turns without;
both ended with no new `tsc` error. One run each.
Java types move to another package: the file moves, its `package` line changes, imports and fully qualified names elsewhere are rewritten,
and files of the old package that used the type without an import gain one. It refuses a file with several top-level types and moves that
would break package-private or protected access. Compiled with `javac` on commons-lang: two samples of 80 and 60 public types, 94 moved
with no new error, 46 refused, none broke.
C# types move to another file: the namespace follows the project's folder convention (an existing target keeps its own),
files that used the type through the old namespace gain `using New;` (extension methods and Razor `@model` pages included),
qualified names are rewritten and a `using` of a namespace that is now empty is removed. It refuses partial types and a target in
another project. Built with `dotnet build` on eShopOnWeb, sampled public types: the Web project (two samples of 60) 119 moved with no new
error and 1 refused, ApplicationCore (40) 37 moved and 3 refused. The first runs, before the Razor, extension-method and empty-namespace
fixes, had 20 broken moves of 135.
Go declarations (func, type with its methods, const, var) move to another file of the package or to another package: `old.Name` becomes
`new.Name` everywhere (a file of the new package drops the qualifier), imports follow in both directions and an import nothing uses any
more is dropped. It refuses what Go would not compile: an unexported name still used in the old package, unexported identifiers,
fields or methods that the move would put out of reach, an import cycle, methods in other files, dot imports, cgo, a name declared in
several files (build tags). Checked with `go vet` (tests included) on gin, cobra, chi and testify, sampled symbols: to a new file of the
same package 310 moved with no new error and 10 refused; to a new package 54 moved with no new error and 482 refused (these packages are
flat and tightly coupled, so most unexported helpers cannot leave). One broken move found by the sampling (a const declared under two
build tags) is now refused.
One Go run (`StringToBytes` in gin, five other files use it, a new package): $0.08 and 10 turns with `move_symbol` against $0.23 and 34 turns without;
both ended with `go vet` showing nothing new. One run each.
With `with_dependencies`, the unexported helpers of the same file that only the moved code uses (functions, types, their methods) travel along
instead of blocking the move. New-package runs with it, 150 samples each (cobra 146): 62 moved with no new `go vet` error, 534 refused
(gin 16, cobra 9, chi 29, testify 8 moved).
PHP classes, interfaces, traits and enums move to another file: the namespace follows the composer.json psr-4 mapping of the new directory
(an existing target file keeps its own), `use` lines, grouped `use`, fully qualified names, docblocks and class-name strings are rewritten,
files of the old namespace that named the class bare gain a `use`, and the moved class gets `use` lines for the old-namespace classes it
named. It refuses bracketed namespaces, several classes in one file, a global class, a new file not named after the class and a name
that already exists in the new namespace. Checked without the mover's own parser (`php -l` on every changed file, a text scan for the old
name, a scan for a missing `use`): koel and bookstack, 100 sampled classes each moved to a new namespace, 200 of 200 clean. The first run found
`instanceof` references missed (4 of 200); fixed.
Dart classes, mixins, enums, typedefs, functions and consts move to another file: the moved code takes the old file's imports (relative
ones re-based) and imports the old file for the public names it uses, a library that re-exported the old file re-exports the new one
(the package API keeps the name), other libraries that saw the name through imports gain `import 'new.dart' show Name;` (same prefix
when they wrote `p.Name`), `part of` files follow their owner library. It refuses part libraries, extensions, conditional imports,
private names the moved code needs and private or unnamed extensions whose members it calls. Checked with `dart analyze` (errors
compared with the untouched package) on dio: two samples, 64 moved with no new error and 64 refused; bloc: 5 of 5. The first run had 4
broken moves of 60 (extension members, a conditional import, `part of` files); all three causes are handled now.
Rust fns, structs, enums, unions, traits, type aliases, consts and statics move to another module file of the same crate (the file gets its
`mod` line in the parent module when it is new): a struct or enum takes the `impl` blocks of its file along, the moved code gets the `use`
lines it needs (paths made absolute, `self::`/`super::` rewritten), every `use` and path that named the item through the old module is
rewritten, modules that glob-imported the old module gain a `use` (a `pub use` glob gains a `pub use`, so facade modules keep exporting it),
and a `pub` item of a public module leaves `pub use new::Name;` behind so the crate's API path still works. Private fields, methods and the
item itself that the other side still reaches are widened to `pub(crate)` (listed in the answer). It refuses private items of the old
module the moved code needs, macros defined in the old file, `#[path]` modules and bin/test crates. Checked with `cargo test --no-run` on a
self-written 11-file crate (generics, nested and inline modules, glob and `pub use` facades, aliases, body-level `use`, traits): 205 moves into
7 targets compile, 54 were refused, none broke; the first runs had 8 broken moves from `use x::{self}` leaves, trait imports not named in the
code, glob-reached modules, tuple variants and import clashes in an existing target, all fixed. Third-party crates were not compiled here.
Kotlin classes, objects, interfaces, enums, functions, properties and typealiases move to another file, in the same package or another
one (a new file gets the package its directory names): `import old.pkg.Name` (also member imports and `as` aliases) and fully qualified
uses name the new package, files of the old package that used the name without an import and `import old.pkg.*` users gain an import,
a file of the new package drops the redundant one, the moved code gets the imports it uses and an import for each name of the old package
it used implicitly, and a `private` declaration the old file still uses becomes `internal`. It refuses overloads, private declarations
of the old file that the moved code needs and a target package that already has the name. Checked with kotlinc on a self-written
6-file project (data classes, extension functions, aliased and wildcard imports, typealiases, private members): 70 moves into 4 targets
compile, 14 were refused, none broke; the first runs had 4 broken moves from imports inside an existing target and an alias import that
hides the original name, both fixed.
Ruby classes, modules, constants and top-level methods move to another file: the definition goes under the same `module` / `class`
wrappers (the constant path doesn't change), the old file keeps `require_relative 'new'` where it stood so everything that required it
still gets the constant (a wrapper left empty is dropped), the new file gets the old file's requires and requires the old file back when
the code names something defined there; in a Rails / Zeitwerk project no requires are written and the new file must be named after the
constant. It refuses code that depends on its file location (`__FILE__`, `__dir__`) across directories, a constant that `autoload` names,
and moves that would make a circular load. Checked by running ruby on a self-written project (output of the entry script identical, and
every lib file loaded alone defines at least what it defined before): 39 moves into 5 targets run identically, 21 were refused, none
broke; the first runs found three load-order cycles (a target that requires the old file), now placed or refused by reachability.
Lua module functions (`function M.f`, `function M:f`, `M.f = ...`), local functions and global functions move to another module: the
function goes before the target's `return`, a new file is `local M = {} ... return M`, every `m.f` where `m` is bound to
`require("old")` (and `require("old").f`) is rewritten to the new module with a `local new = require("new")` added, the old file's own
`M.f` uses too, and a lazy forwarding stub stays in the old module for methods (found through metatables) and for mentions that could
not be rewritten. It refuses locals of the old file the function uses, a function that needs the old module table while the old module
calls it, and any move that would make modules `require` each other at load time. Checked by running the project in lupa (output of
the entry script identical): 19 moves into 4 targets run identically, 29 were refused, none broke; the first runs found `is` comparisons
on tree-sitter nodes (fixed in the Rust and Ruby movers too) and requires loops through the target.
GDScript `static func`, `const`, `enum` and inner `class` members move to another script: the member goes to the end of the target (a new
file starts with `extends RefCounted`), `preload` constants it uses are copied, other members of the old script it names are written
`Old.member` (with a `preload` constant added when the old script has no `class_name`), and every `Old.f` (class_name, a preload
constant, or an inline `preload("res://old.gd").f`) plus the bare uses inside the old script become `New.f` with the constant added where
needed. It refuses instance functions and variables, instance members the moved code uses, a name the target has, a script that would
preload itself and a `preload` cycle (Godot rejects those). Checked by running the project headless in Godot 4.7 (output of the entry
script identical): 50 moves into 5 targets run identically, 30 were refused, none broke; the first run found a const that preloaded
its own new script (now refused).
Checked on 55 sampled symbols of requests and starlette: 22 moved and still compile and import, 33 were refused
(they use a name from their old module), none broke. With `with_dependencies` the functions, classes and constants
the symbol needs move along (several symbols can also be listed, comma separated): 200 sampled symbols, 166 moved and
still import, 34 refused (module-level code that runs the symbol, a conditional import, a method), none broke.

Answer quality, on the six "explain how X works" tasks, scored against key facts
(function names, files, settings) checked in each repository's source: without
Ripplet the answers name 100% of them. With Ripplet they first named 93%: one
answer followed `Session.send` and never reached the function that reads the
`NO_PROXY` variable. `task_context` now lists the rest of the topic by name (the
other `*proxy*` functions next to the top matches); since then 100% on two runs per
task, at $0.123 against $0.142. The facts are easy for a model that reads whole
files, so this shows Ripplet does not lose completeness, not that it adds any.

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
| C / C++ | includes, namespaces, templates (also `d.m<T>()`), `#if` branches, macro bodies, constructor initializer lists |
| Swift | modules, extensions, closures typed by their callee |
| Dart, Lua, GDScript | imports / requires, classes and modules; Dart null-aware calls and initializers |

A call must get an edge wherever it is written. For each language the regression
tests put calls in the awkward places: negated (`!f()`), chained over several lines
(the call is recorded on the line of its name), generic (`f::<T>()`, `f<T>(x)`),
null-conditional (`a?.f()`), and inside initializers, constructors, default values
and `init` blocks. Finding the ones that were silently dropped came before the
accuracy numbers above moved.

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
| `task_context` | the start of a task: the code it is about, callers (proven, and possible ones marked as such), the rest of the topic by name, routes / tables, the feature to imitate, files to edit, tests |
| `repo_map` | an unfamiliar repository: entry points, most-used files and symbols |
| `explain_symbol` | a known name, or several at once (`symbols=[...]`): source, every caller and callee with the call's code |
| `get_neighbors`, `analyze_change_impact` | who calls it / what breaks, with every call line; names work as well as ids, and a symbol nothing calls says so |
| `change_plan`, `rename_symbol` | renames and signature changes: every line to edit, same-named code to leave alone |
| `move_symbol` | moves top-level Python functions or classes, or TypeScript/JavaScript (ES module) functions, classes, types, enums and consts, a Java class, interface, enum or record (to another package), a C# type, a Go func, type, const or var, a PHP class, interface, trait or enum, a Dart class, mixin, enum, typedef, function or const, a Rust fn, struct, enum, union, trait, type, const or static, or a Kotlin class, object, interface, function, property or typealias, a Ruby class, module, constant or method, a Lua function, or a GDScript static func, const, enum or inner class (to another file, with its namespace and `using` directives fixed), to another module in one call (`with_dependencies` brings what they need from the old module): the definition with the imports it needs, every `from old import name` rewritten (layout kept), a re-export left where something still needs the old path; refuses (writing nothing) when the symbol uses a name defined in its old module, since that would make the modules import each other; `dry_run` |
| `change_signature` | adds, removes or reorders parameters on the declaration and every call site in one call (`add_param={decl, value}` / `remove_param=name` / `reorder_params=[names]`, `dry_run`; a reorder permutes positional arguments, leaves named ones alone and lists calls it cannot prove); unproven call sites are listed, not edited |
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
