#!/usr/bin/env python3
"""Make a kotatsu-parsers-redo checkout buildable as a plugin JAR for the Kototoro client.

What changed upstream
---------------------
Upstream commit 3432a85a ("Parsers that update without an app release") moved the parsers
onto "parsers-api v1": the *application* owns the contract (src/contract/), the parsers are
built into a signed dex bundle (parsers-v1.json + parsers-v1-<version>.zip) and the client
calls them through ParsersBundle. In that model the generated source type is a plain class
(`ParserMangaSource`) and MangaParser/MangaLoaderContext grew bundle-only members.

The client we serve is older than that: it loads a plugin JAR whose entry point is
`MangaParserFactoryKt.newParser(MangaParserSource, MangaLoaderContext)`, where
`MangaParserSource` must be an *enum* (see JarExtensionLoader.resolveFactorySources) and
`MangaParser.source` is typed `MangaSource`. Building upstream as-is produces classes that
load to zero sources or fail with AbstractMethodError.

What this script does
---------------------
1. Puts the client's own parser-api files back in place of the contract, so the compiler is
   checking against the API the plugin will actually run on.
2. Collapses the contract source set into the main one (single compilation; the old layout),
   so the KSP-generated `MangaParserSource` is visible to the base parsers.
3. Restores an enum-generating KSP processor and a class-based LinkResolver.
4. Drops the two parsers-api-v1 files the client has no counterpart for.

Everything is asserted: if upstream reshapes a block this script rewrites, it fails loudly
instead of producing a quietly broken plugin.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CI_DIR = Path(__file__).resolve().parent
API_REF = CI_DIR / "api-ref.txt"
COMPAT = CI_DIR / "compat"

API_PATH = "parser-api/src/androidMain/kotlin/"
CONTRACT = Path("src/contract/kotlin")
MAIN = Path("src/main/kotlin")
PKG = "org/koitharu/kotatsu/parsers"
KSP_PROCESSOR = Path("kotatsu-parsers-ksp/src/main/kotlin/org/koitharu/kotatsu/parsers/ksp/ParserProcessor.kt")

# Files that only exist for the bundle architecture; the client provides neither.
UPSTREAM_ONLY = (
    f"{PKG}/ParsersBundle.kt",
    f"{PKG}/model/ParserMangaSource.kt",
)
# Parsers that only make sense with the bundle runtime, or whose class the client owns.
BUNDLE_ONLY_IMPLS = (
    f"{PKG}/core/ParsersBundleImpl.kt",
    f"{PKG}/util/LinkResolver.kt",
)


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def fail(step: str, message: str) -> "None":
    raise SystemExit(f"[{step}] {message}")


def read_api_ref() -> tuple[str, str]:
    for line in API_REF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        repo, _, sha = line.partition(" ")
        if not sha:
            break
        return repo, sha.strip()
    fail("api-ref", f"{API_REF} needs a '<owner>/<repo> <commit>' line")


def http_get(url: str, token: str | None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "k-parsers-ci"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    last: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404):
                break
        except urllib.error.URLError as e:
            last = e
    fail("fetch", f"cannot read {url}: {last}")


def client_contract_files(repo: str, sha: str, token: str | None) -> set[str]:
    """Every file the client has under its parser api directory, relative to it."""
    url = f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
    tree = json.loads(http_get(url, token))
    if tree.get("truncated"):
        fail("contract", f"{repo}@{sha[:8]} tree listing is truncated; use a shallow-friendly ref")
    files = {
        item["path"][len(API_PATH):]
        for item in tree.get("tree", ())
        if item.get("type") == "blob" and item["path"].startswith(API_PATH)
    }
    if not files:
        fail("contract", f"{repo}@{sha[:8]} has nothing under {API_PATH}")
    return files


def overlay_contract(root: Path, repo: str, sha: str, token: str | None) -> None:
    """Put the client's version of every shared contract file in place."""
    contract = root / CONTRACT
    if not contract.is_dir():
        fail("contract", f"{contract} is missing; upstream moved the contract source set")

    upstream_files = {
        p.relative_to(contract).as_posix() for p in contract.rglob("*.kt")
    }
    client_files = client_contract_files(repo, sha, token)
    shared = sorted(upstream_files & client_files)

    def download(rel: str) -> None:
        url = f"https://raw.githubusercontent.com/{repo}/{sha}/{API_PATH}{rel}"
        (contract / rel).write_bytes(http_get(url, token))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(download, shared):
            pass
    log("contract", f"{len(shared)} files taken from {repo}@{sha[:8]}")

    for rel in sorted(upstream_files - client_files):
        (contract / rel).unlink()
        log("contract", f"dropped {rel} (no counterpart in the client api)")

    missing = sorted(client_files - upstream_files)
    if missing:
        log("contract", f"{len(missing)} files are client-only (nothing to overlay for them)")


def collapse_source_sets(root: Path) -> None:
    """One compilation for everything: the old pre-3432a85a layout."""
    contract, main = root / CONTRACT, root / MAIN
    if not main.is_dir():
        fail("layout", f"{main} is missing")
    moved = 0
    for path in sorted(contract.rglob("*.kt")):
        target = main / path.relative_to(contract)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            fail("layout", f"{target} would be overwritten by the contract copy")
        path.rename(target)
        moved += 1
    shutil.rmtree(root / "src/contract")
    log("layout", f"{moved} contract files moved into src/main, contract source set removed")


def sub_once(path: Path, pattern: str, replacement: str, what: str) -> None:
    text = path.read_text(encoding="utf-8")
    text, count = re.subn(pattern, replacement, text, flags=re.S)
    if count != 1:
        fail("patch", f"{what}: {count} matches in {path} (upstream reshaped it)")
    path.write_text(text, encoding="utf-8")


def patch_build_script(root: Path) -> None:
    build = root / "build.gradle.kts"
    sub_once(
        build,
        r'val parsersApiVersion: Int = Regex\(.*?error\("ParsersApi\.VERSION was not found"\)',
        "val parsersApiVersion: Int = 1 // the api this plugin is compiled against",
        "parsersApiVersion no longer reads ParsersBundle.kt",
    )
    sub_once(
        build,
        r"\n\s*from\(generateBundleKeepRules\)",
        "",
        "the jar task no longer carries the bundle shrinker rules",
    )
    log("patch", "build.gradle.kts")


def install_compat_sources(root: Path) -> None:
    shutil.copyfile(COMPAT / "ParserProcessor.kt", root / KSP_PROCESSOR)
    log("patch", "KSP processor writes an enum MangaParserSource")

    for rel in BUNDLE_ONLY_IMPLS:
        target = root / MAIN / rel
        if target.exists():
            target.unlink()
            log("patch", f"removed {rel}")

    (root / MAIN / PKG / "util/LinkResolver.kt").write_text(
        (COMPAT / "LinkResolver.kt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    log("patch", "util/LinkResolver.kt is the class the parsers call into")

    # The client's MangaParser declares resolveLink, so the base parsers override it.
    sub_once(
        root / MAIN / PKG / "core/AbstractMangaParser.kt",
        r"public open suspend fun resolveLink\(",
        "public override suspend fun resolveLink(",
        "AbstractMangaParser.resolveLink",
    )
    sub_once(
        root / MAIN / PKG / "core/FlexibleMangaParser.kt",
        r"open suspend fun resolveLink\(",
        "override suspend fun resolveLink(",
        "FlexibleMangaParser.resolveLink",
    )
    log("patch", "base parsers override MangaParser.resolveLink")


def drop_contract_only_references(root: Path) -> None:
    """Rewrite the few places the parsers reach for the removed v1 types."""
    main = root / MAIN
    for path in sorted(main.rglob("*.kt")):
        text = path.read_text(encoding="utf-8")
        new, count = re.subn(r" && \w+\.source is ParserMangaSource", "", text)
        if count:
            path.write_text(new, encoding="utf-8")
            log("patch", f"{path.relative_to(root)}: dropped a ParserMangaSource check")

    left = sorted(
        p.relative_to(root).as_posix()
        for p in main.rglob("*.kt")
        if re.search(r"\bParserMangaSource\b|\bParsersBundle\b", p.read_text(encoding="utf-8"))
    )
    if left:
        fail(
            "patch",
            "the parsers now use the bundle api directly, which needs a compat rule here: "
            + ", ".join(left),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="path to the kotatsu-parsers-redo checkout")
    parser.add_argument("--token", default=None, help="github token for the api call")
    args = parser.parse_args()

    root = args.checkout.resolve()
    if not (root / "gradlew").exists():
        fail("layout", f"{root} does not look like the parsers checkout")

    repo, sha = read_api_ref()
    log("api-ref", f"client parser api: {repo}@{sha[:8]}")

    overlay_contract(root, repo, sha, args.token)
    collapse_source_sets(root)
    patch_build_script(root)
    install_compat_sources(root)
    drop_contract_only_references(root)
    log("done", "checkout is ready for ./gradlew jar")


if __name__ == "__main__":
    main()
