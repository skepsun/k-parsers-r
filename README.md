# Kotatsu-Redo Parsers CI

An automated ghost repository. Every six hours (and on demand) it polls
`Kotatsu-Redo/kotatsu-parsers-redo`, rebuilds the parsers as a plugin JAR and publishes it to the
`repo` branch, where the Kototoro Android client downloads it:

```
repo/
├── index.min.json        plugin manifest the client reads
├── upstream_commit.txt   upstream commit the JAR was built from
├── compat_ref.txt        client parser API the JAR was compiled against
└── apk/plugin.jar        classes.dex of all parsers
```

## Why the build carries a compatibility layer

Upstream commit `3432a85a` (*"Parsers that update without an app release"*) moved the parsers onto
**parsers-api v1**: the application owns the contract (`src/contract/`), parsers no longer expose an
enum of sources, and they are shipped as a *signed dex bundle* (`parsers-v1.json` +
`parsers-v1-<version>.zip`) that the app drives through `ParsersBundle`.

The client we serve is older than that split. Its `JarExtensionLoader` only accepts the classic
plugin shape:

* an **enum** `org.koitharu.kotatsu.parsers.model.MangaParserSource`,
* a factory `MangaParserFactoryKt.newParser(MangaParserSource, MangaLoaderContext)`,
* `MangaParser.source` typed as `MangaSource`,
* `@Broken` / `@MangaSourceParser` visible at runtime.

Building upstream as-is therefore yields classes the client loads as **zero sources** (the source
type is a class, not an enum) or crashes on with `AbstractMethodError`. Upstream's own dex bundle is
not a drop-in replacement either: it is signatures/keys-checked, contains no contract classes and
calls through `ParsersBundle`, which the client does not have.

So `ci/make_compatible.py` rewrites a checkout into the pre-`3432a85a` shape:

1. every contract file the client also has is replaced by the client's own copy, so the compiler
   checks against the API the plugin will really run on;
2. the contract source set is merged into `src/main` again (one compilation, so the generated
   source enum is visible to the base parsers);
3. `ci/compat/ParserProcessor.kt` regenerates `MangaParserSource` as an enum, and
   `ci/compat/LinkResolver.kt` restores the class the parsers call into;
4. the two parsers-api-v1-only files (`ParsersBundle`, `ParserMangaSource`) and their single
   implementation are dropped.

Each step asserts on the text it rewrites, so an upstream reshuffle fails the run loudly instead of
shipping a quietly broken plugin. `ci/verify_plugin.py` then checks the built JAR's actual
bytecode (enum-ness, factory signature, `getSource()` return type, runtime `@Broken`, source count)
before anything is published.

## The pinned client API

`ci/api-ref.txt` holds the client commit whose `parser-api` the parsers are compiled against:

```
Kototoro-app/Kototoro <commit>
```

Bump it whenever the client's `parser-api` changes — a stale pin either fails `verify_plugin.py` or
shows up at runtime as `NoSuchMethodError`. A change to the pin alone is enough to trigger a
rebuild (the delta check compares both the upstream commit and this pin).
