# move_symbol: how each language is moved and checked

One call moves a top-level symbol to another file, module or package and fixes imports, namespaces and visibility. It writes nothing when it cannot prove the move is safe (`dry_run` previews). Every mover was checked with the real compiler or interpreter; "none broke" means no new error against the untouched project.

Single agent runs (one each, read them as a direction): moving `guess_filename` in requests: $0.09 / 11 turns with `move_symbol` vs $0.23 / 28 without. `getURLPathname` in h3 (TypeScript): $0.17 / 17 vs $0.21 / 25. `StringToBytes` in gin (Go): $0.08 / 10 vs $0.23 / 34. All ended with passing checks.

TypeScript and JavaScript (ES modules) are moved the same way, from tree-sitter syntax trees: imports of the moved code
travel with it, `import {x} from './old'` and `export {x} from './old'` elsewhere are rewritten (`.js`/`.ts`/no-extension
style kept), imports the old module no longer uses are dropped, and a barrel (`export *`), namespace import or
`require`/`import()` keeps a re-export. It refuses CommonJS, default exports, a `declare module` block that re-opens the
name, assignment to a moved `let`, and path-alias imports it cannot resolve are listed. Checked with `tsc`
(errors compared with the untouched repo) on sampled exports with `with_dependencies`: h3 80 samples, 75 moved with no new
error, 5 refused; hono 60 samples, 52 moved with no new error, 8 refused (the grammar misreads a few generic call signatures).
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
Swift classes, structs, enums, actors, protocols, functions, properties and typealiases move to another file of the same module (a SwiftPM
target): nothing that names them changes, the moved code gets the old file's `import` lines, a `private` / `fileprivate` declaration the
old file still uses becomes internal, and a file left with only imports is deleted. It refuses a move to another target (that needs
`public` and a new import), the moved code using a private declaration of the old file, a type with private members that an `extension`
left behind reaches, and top-level variables of `main.swift`. Checked with `swiftc -typecheck` (Swift 6.4, Windows SDK) on a self-written
module: 48 moves into 6 targets type-check, 18 were refused, none broke.
C and C++ functions, types (struct / union / enum / class / typedef / template / `using`), macros and global variables move to another
file, where the kind of thing decides the target: types and macros only to a header (the old file then `#include`s it where it stood, so
everything that included the old file still sees it), a function with external linkage only to a source file (a prototype is left where it
stood when the old file still calls it, and prototypes the moved code relied on are copied), `static` / `inline` things only where nothing
else needs them. The moved code gets the old file's `#include` lines (quoted paths re-based); a header target can't depend on the old
header (include cycle), a source target includes an old header, and a source-to-source move re-declares the old file's external
functions it calls. It refuses code inside a namespace, `extern "C"` block or `#if`, member functions defined outside their class and
anything that would leave a file-local thing behind; the new file must be added to the build by hand. Checked by compiling, linking and
running with clang (-Werror=implicit-function-declaration, output identical) on a self-written C project (12 moves into 4 targets, 20
refused) and a C++ project (20 moves, 20 refused): none broke; the first run missed prototypes the moved `main` relied on.
Checked on 55 sampled symbols of requests and starlette: 22 moved and still compile and import, 33 were refused
(they use a name from their old module), none broke. With `with_dependencies` the functions, classes and constants
the symbol needs move along (several symbols can also be listed, comma separated): 200 sampled symbols, 166 moved and
still import, 34 refused (module-level code that runs the symbol, a conditional import, a method), none broke.

