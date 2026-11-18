#!/usr/bin/env python3
"""Refuse to publish a plugin JAR whose surface the Kototoro client cannot load.

The client (JarExtensionLoader) resolves a plugin like this:

  * it looks for an enum named ...parsers.model.MangaParserSource,
  * it requires ...parsers.MangaParserFactoryKt.newParser(MangaParserSource, MangaLoaderContext),
  * it calls MangaParser.getSource(), which must return ...parsers.model.MangaSource,
  * it reads @Broken at runtime to filter dead sources.

Compile errors catch most drift, but a wrong shape can still compile (for example an enum
that lost its interface, or a factory that kept the bundle signature). This gate runs on
the built jar, so a broken plugin never reaches the repo branch.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

MODEL_SOURCE = "org.koitharu.kotatsu.parsers.model.MangaSource"
PARSER_SOURCE = "org.koitharu.kotatsu.parsers.model.MangaParserSource"
PARSER = "org.koitharu.kotatsu.parsers.MangaParser"
FACTORY = "org.koitharu.kotatsu.parsers.MangaParserFactoryKt"
MIN_SOURCES = 100


def fail(message: str) -> "None":
    raise SystemExit(f"[verify] {message}")


def javap(jar: Path, class_name: str) -> str:
    javap = Path(os.environ.get("JAVA_HOME", "")) / "bin/javap"
    command = [str(javap) if javap.exists() else "javap", "-p", "-cp", str(jar), class_name]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"javap cannot read {class_name}: {result.stderr.strip()}")
    return result.stdout


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: verify_plugin.py <built jar> [checkout]")
    jar = Path(sys.argv[1])
    if not jar.is_file():
        fail(f"{jar} does not exist")

    with zipfile.ZipFile(jar) as archive:
        names = set(archive.namelist())

    for leaked in ("ParsersBundle.class", "model/ParserMangaSource.class"):
        if f"org/koitharu/kotatsu/parsers/{leaked}" in names:
            fail(f"the bundle api leaked into the plugin ({leaked})")

    for required in ("model/MangaParserSource.class", "MangaParserFactoryKt.class", "MangaParser.class"):
        if f"org/koitharu/kotatsu/parsers/{required}" not in names:
            fail(f"missing org/koitharu/kotatsu/parsers/{required}")

    enum = javap(jar, PARSER_SOURCE)
    if not re.search(rf"extends java\.lang\.Enum<{re.escape(PARSER_SOURCE)}>", enum):
        fail("MangaParserSource is not an enum; the client resolves sources from an enum")
    if f"implements {MODEL_SOURCE}" not in enum:
        fail(f"MangaParserSource does not implement {MODEL_SOURCE}")
    entries = len(re.findall(rf"public static final {re.escape(PARSER_SOURCE)} [A-Z0-9_]+;", enum))
    if entries < MIN_SOURCES:
        fail(f"only {entries} sources in the enum, expected at least {MIN_SOURCES}")

    # KSP quietly skips declarations whose file fails to validate, which leaves the enum
    # short with no compile error at all: compare against the sources themselves.
    if len(sys.argv) > 2:
        sources = Path(sys.argv[2]) / "src/main"
        declared = sum(
            text.count("@MangaSourceParser(")
            for text in (p.read_text(encoding="utf-8") for p in sources.rglob("*.kt"))
        )
        if declared != entries:
            fail(f"{declared} parsers declare a source but the enum has {entries}; KSP dropped some")

    get_source = re.search(r"public (?:abstract )?([\w.]+) getSource\(\);", javap(jar, PARSER))
    if not get_source or get_source.group(1) != MODEL_SOURCE:
        fail(f"MangaParser.getSource() returns {get_source.group(1) if get_source else 'nothing'}, not {MODEL_SOURCE}")

    factory = javap(jar, FACTORY)
    signature = f"newParser({PARSER_SOURCE}, org.koitharu.kotatsu.parsers.MangaLoaderContext)"
    if f"static final org.koitharu.kotatsu.parsers.MangaParser {signature};" not in factory:
        fail(f"{FACTORY} has no `{signature}`; the client cannot create parsers")

    # A source-retention annotation never reaches the class file, so a parser class that
    # references Broken at all proves the client can read it at runtime.
    annotated = 0
    with zipfile.ZipFile(jar) as archive:
        candidates = [
            name for name in names
            if name.startswith("org/koitharu/kotatsu/parsers/site/") and name.endswith(".class") and "$" not in name
        ]
        for name in candidates[:300]:
            if b"org/koitharu/kotatsu/parsers/Broken" in archive.read(name):
                annotated += 1
    if not annotated:
        fail("@Broken is not visible at runtime; broken sources would be offered as working")

    print(f"[verify] {entries} sources, {annotated} broken-annotated, enum + factory + source type match the client api", flush=True)


if __name__ == "__main__":
    main()
