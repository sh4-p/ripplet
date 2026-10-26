<!-- ripplet:rules:begin -->
## Ripplet (code graph of this repository)

This repository is indexed by Ripplet. Its tools answer code-structure
questions from a prebuilt graph, with file:line for every fact. They know
calls through interfaces, DI, overrides and imports that text search misses.

- Starting a task: call `task_context` with the task text first. It returns the
  code the task is about (with excerpts and real callers), related routes and
  tables, an existing feature to imitate for "like X", the files to edit and
  the tests that reach them.
- New to this repository: call `repo_map` first. It lists entry points and the
  files and symbols the rest of the code uses most, with signatures.
- A known name (class, function, method): call `explain_symbol` before Grep or
  Read. One call returns its source, every caller and every callee with file:line.
  Need several names? Pass them all in `symbols` in ONE call, not one call each.
- "Who calls X?" / "What breaks if X changes?": `get_neighbors` (direction=in) or
  `analyze_change_impact`. Do not grep for callers: grep misses indirect calls
  and returns unrelated code with the same name. The list is complete only
  when the answer has no `... more:` line, no `[inferred]` caller and no
  UNVERIFIED section. Otherwise fetch the rest (offset), then open each
  inferred / unverified line (a few lines of Read each) before reporting.
- A test failed or something crashed: pass the printed trace or error line to
  `investigate_error`. It maps each frame to its function and code, shows the
  code where it happened and its callers, and finds where the message is thrown.
- Renaming a symbol: call `rename_symbol` (symbol, new_name). It edits the
  declaration, overrides, every call site and import itself, only on lines the
  graph proves, and returns the remaining text mentions to review. Do not open
  and edit each file by hand.
- Changing the parameters of, or removing a symbol: call `change_plan` BEFORE
  editing. It lists every line to change (overrides and each call site), and
  same-named code that must NOT change.
- Security / permission audits, "which endpoint lets a user do X": call
  `list_routes`. It lists every route with its handler and the access checks
  it calls (write routes without a check first) and proposes a
  ripplet.arch.yaml rule that keeps the check enforced on new routes.
- Database: tables and columns from .sql files and framework migrations
  (Laravel, Rails, Django, Alembic, EF Core, Prisma) are symbols too.
  `explain_symbol` / `change_plan` on `tasks` or `tasks.status_id` list the
  queries that use them; write a migration, then `check_change` lists code
  that still uses what it drops or renames.
- Adding a feature similar to an existing one: call `feature_slice` with the
  existing feature's name first. It returns that feature's files and every
  place the new feature must also touch.
- After editing code, before running the whole test suite: call `check_change`.
  It lists callers your edit broke, places that still use what you replaced,
  and the exact tests to run. Fix what it marks DEFINITE; POSSIBLE items are
  hints to look at, not orders. Pass `task` (the task in words) to also see
  files you changed that the task does not lead to. Architecture rules in
  ripplet.arch.yaml (`ripplet arch --example`) are checked there too.
- Read a file only for lines a tool result pointed to, or code it did not show.
- Use Grep for text: strings, config keys, log messages, comments. The graph
  indexes code symbols, not text.
- A result that was shortened ends with a line starting `[ripplet:` that says
  how to get the rest. Follow it instead of reading whole files.
- Answer code-structure questions (flows, callers, where X happens) with these
  tools yourself; an exploration subagent that greps and reads files costs
  more turns for the same answer. When you do hand exploration to a subagent,
  put these rules in its prompt.

Without the MCP tools, the same answers come from the shell, in the repository
root: `ripplet tool repo_map`, `ripplet tool explain_symbol symbol=<Name>`,
`ripplet tool rename_symbol symbol=<Name> new_name=<New>`,
`ripplet tool change_plan symbol=<Name> change=signature`,
`ripplet tool get_neighbors id=<node id> direction=in`,
`ripplet tool feature_slice feature=<name>`, `ripplet check`.
<!-- ripplet:rules:end -->
