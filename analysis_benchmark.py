"""Repeatable precision/recall benchmarks for persisted project-analysis reports."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from analysis_quality import project_review_quality, response_quality

from project_inventory import decode_text_content, inventory_file, InventorySourceFile


@dataclass(frozen=True)
class BenchmarkExpectation:
    id: str
    path: str
    analyzer: str = "function"
    line: int | None = None
    source_contains: str | None = None
    occurrence: int = 1
    category: str | None = None
    title_pattern: str | None = None
    allowed_severities: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanRegion:
    id: str
    path: str
    start_contains: str
    end_contains: str | None = None


@dataclass(frozen=True)
class ObservedFinding:
    id: str
    analyzer: str
    path: str
    start_line: int | None
    end_line: int | None
    severity: str
    category: str
    title: str
    provenance: str
    report_tier: str = "defect"
    evidence: str | None = None
    failure_type: str | None = None


@dataclass(frozen=True)
class BenchmarkMetrics:
    analyzer_version: str
    total_finding_count: int
    expected_count: int
    true_positive_count: int
    false_positive_count: int
    missed_count: int
    duplicate_count: int
    advisory_count: int
    unanchored_count: int
    unanchored_rate: float
    clean_region_finding_count: int
    precision: float
    recall: float
    matched_expectation_ids: tuple[str, ...]
    missed_expectation_ids: tuple[str, ...]
    false_positive_finding_ids: tuple[str, ...]
    duplicate_finding_ids: tuple[str, ...]
    clean_region_finding_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkManifest:
    name: str
    expectations: tuple[BenchmarkExpectation, ...]
    clean_regions: tuple[CleanRegion, ...] = ()


@dataclass(frozen=True)
class MutationRecipe:
    name: str
    language: str
    extension: str
    clean_source: str
    mutated_source: str
    expectation: BenchmarkExpectation


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("./").casefold()


def _path_matches(observed: str, expected: str) -> bool:
    actual = _normalized_path(observed)
    wanted = _normalized_path(expected)
    return actual == wanted or actual.endswith(f"/{wanted}")


def _line_overlaps(finding: ObservedFinding, line: int | None) -> bool:
    if line is None:
        return True
    if finding.start_line is None:
        return False
    return finding.start_line <= line <= (finding.end_line or finding.start_line)


def _finding_matches(
    finding: ObservedFinding,
    expectation: BenchmarkExpectation,
) -> bool:
    if finding.report_tier != "defect":
        return False
    if expectation.source_contains and expectation.line is None:
        return False
    if finding.analyzer != expectation.analyzer:
        return False
    if not _path_matches(finding.path, expectation.path):
        return False
    if not _line_overlaps(finding, expectation.line):
        return False
    if expectation.category and finding.category.casefold() != expectation.category.casefold():
        return False
    if expectation.allowed_severities and finding.severity not in expectation.allowed_severities:
        return False
    if expectation.title_pattern and not re.search(
        expectation.title_pattern,
        f"{finding.title}\n{finding.failure_type or ''}",
        re.IGNORECASE,
    ):
        return False
    return True


def _line_for_occurrence(source: str, needle: str, occurrence: int) -> int | None:
    if occurrence < 1 or not needle:
        return None
    offset = -1
    for _index in range(occurrence):
        offset = source.find(needle, offset + 1)
        if offset < 0:
            return None
    return source.count("\n", 0, offset) + 1


def resolve_manifest_lines(
    manifest: BenchmarkManifest,
    sources: dict[str, str],
) -> BenchmarkManifest:
    resolved: list[BenchmarkExpectation] = []
    for expectation in manifest.expectations:
        if expectation.line is not None or not expectation.source_contains:
            resolved.append(expectation)
            continue
        matches = [value for path, value in sources.items() if _path_matches(path, expectation.path)]
        source = matches[0] if len(matches) == 1 else None
        line = (
            _line_for_occurrence(
                source,
                expectation.source_contains,
                expectation.occurrence,
            )
            if source is not None
            else None
        )
        resolved.append(
            BenchmarkExpectation(**{**asdict(expectation), "line": line})
        )
    return BenchmarkManifest(manifest.name, tuple(resolved), manifest.clean_regions)


def _region_bounds(region: CleanRegion, source: str) -> tuple[int, int] | None:
    start = _line_for_occurrence(source, region.start_contains, 1)
    if start is None:
        return None
    if region.end_contains is None:
        return start, source.count("\n") + 1
    end_offset = source.find(region.end_contains)
    if end_offset < 0:
        return None
    end = max(start, source.count("\n", 0, end_offset))
    return start, end


def evaluate_findings(
    findings: Iterable[ObservedFinding],
    manifest: BenchmarkManifest,
    *,
    analyzer_version: str,
    sources: dict[str, str] | None = None,
) -> BenchmarkMetrics:
    active_analyzers = {item.analyzer for item in manifest.expectations}
    observed = [
        item
        for item in findings
        if not active_analyzers or item.analyzer in active_analyzers
    ]
    if "parser" in active_analyzers:
        parser_findings = [item for item in observed if item.analyzer == "parser"]
        observed = [item for item in observed if not (
            item.analyzer == "function" and item.category == "syntax"
            and any(_path_matches(item.path, diagnostic.path) and _line_overlaps(item, diagnostic.start_line)
                    for diagnostic in parser_findings)
        )]
    source_map = sources or {}
    resolved = resolve_manifest_lines(manifest, source_map)
    unmatched = set(range(len(observed)))
    matched_expectations: list[str] = []
    missed_expectations: list[str] = []
    duplicates: list[str] = []

    for expectation in resolved.expectations:
        candidates = [
            index
            for index in sorted(unmatched)
            if _finding_matches(observed[index], expectation)
        ]
        if not candidates:
            missed_expectations.append(expectation.id)
            continue
        best = min(
            candidates,
            key=lambda index: (
                observed[index].report_tier != "defect",
                observed[index].provenance not in {"deterministic", "fallback"},
                index,
            ),
        )
        unmatched.remove(best)
        matched_expectations.append(expectation.id)
        for index in candidates:
            if index == best:
                continue
            duplicates.append(observed[index].id)
            unmatched.discard(index)

    false_positives = [
        observed[index].id
        for index in sorted(unmatched)
        if observed[index].report_tier == "defect"
    ]
    advisory_count = sum(item.report_tier == "advisory" for item in observed)
    unanchored_count = sum(item.start_line is None for item in observed)

    clean_ids: list[str] = []
    for region in resolved.clean_regions:
        source_entry = next(
            (
                (path, value)
                for path, value in source_map.items()
                if _path_matches(path, region.path)
            ),
            None,
        )
        if source_entry is None:
            continue
        bounds = _region_bounds(region, source_entry[1])
        if bounds is None:
            continue
        start, end = bounds
        clean_ids.extend(
            item.id
            for item in observed
            if _path_matches(item.path, region.path)
            and item.start_line is not None
            and start <= item.start_line <= end
        )

    true_positives = len(matched_expectations)
    precision_denominator = true_positives + len(false_positives) + len(duplicates)
    recall_denominator = len(resolved.expectations)
    return BenchmarkMetrics(
        analyzer_version=analyzer_version,
        total_finding_count=len(observed),
        expected_count=recall_denominator,
        true_positive_count=true_positives,
        false_positive_count=len(false_positives),
        missed_count=len(missed_expectations),
        duplicate_count=len(duplicates),
        advisory_count=advisory_count,
        unanchored_count=unanchored_count,
        unanchored_rate=(unanchored_count / len(observed) if observed else 0.0),
        clean_region_finding_count=len(set(clean_ids)),
        precision=(true_positives / precision_denominator if precision_denominator else 1.0),
        recall=(true_positives / recall_denominator if recall_denominator else 1.0),
        matched_expectation_ids=tuple(matched_expectations),
        missed_expectation_ids=tuple(missed_expectations),
        false_positive_finding_ids=tuple(false_positives),
        duplicate_finding_ids=tuple(duplicates),
        clean_region_finding_ids=tuple(dict.fromkeys(clean_ids)),
    )


def load_manifest(path: str | Path) -> BenchmarkManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValueError("Benchmark manifest must use format_version 1")
    expected_payload = payload.get("expected")
    if not isinstance(expected_payload, list):
        raise ValueError("Benchmark manifest expected must be a list")
    expectations: list[BenchmarkExpectation] = []
    for item in expected_payload:
        if not isinstance(item, dict):
            raise ValueError("Each benchmark expectation must be an object")
        values = dict(item)
        severities = values.pop("allowed_severities", ())
        values["allowed_severities"] = tuple(severities)
        expectations.append(BenchmarkExpectation(**values))
    clean_payload = payload.get("clean_regions", [])
    if not isinstance(clean_payload, list):
        raise ValueError("Benchmark manifest clean_regions must be a list")
    clean_regions = tuple(CleanRegion(**item) for item in clean_payload)
    return BenchmarkManifest(
        name=str(payload.get("name") or Path(path).stem),
        expectations=tuple(expectations),
        clean_regions=clean_regions,
    )


def load_project_observations(
    db: sqlite3.Connection,
    project_id: str,
) -> tuple[list[ObservedFinding], dict[str, str]]:
    issue_columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(project_symbol_issues)")
    }
    optional = {
        name: (name if name in issue_columns else f"NULL AS {name}")
        for name in ("evidence", "failure_type", "report_tier")
    }
    function_rows = db.execute(
        f"""
        SELECT issue.id, file.path, issue.start_line, issue.end_line,
               issue.severity, issue.category, issue.title, issue.provenance,
               {optional['report_tier']}, {optional['evidence']},
               {optional['failure_type']}
        FROM project_symbol_issues AS issue
        JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
        JOIN project_files AS file ON file.id = symbol.file_id
        WHERE symbol.project_id = ?
        ORDER BY issue.id
        """,
        (project_id,),
    ).fetchall()
    findings = [
        ObservedFinding(
            id=f"function:{int(row['id'])}",
            analyzer="function",
            path=str(row["path"]),
            start_line=int(row["start_line"]) if row["start_line"] is not None else None,
            end_line=int(row["end_line"]) if row["end_line"] is not None else None,
            severity=str(row["severity"]),
            category=str(row["category"]),
            title=str(row["title"]),
            provenance=str(row["provenance"]),
            report_tier=(
                str(row["report_tier"])
                if row["report_tier"] is not None
                else (
                    "advisory"
                    if row["category"] == "maintainability" or row["severity"] == "info"
                    else "defect"
                )
            ),
            evidence=str(row["evidence"]) if row["evidence"] is not None else None,
            failure_type=(
                str(row["failure_type"])
                if row["failure_type"] is not None
                else None
            ),
        )
        for row in function_rows
    ]
    call_rows = db.execute(
        """
        SELECT finding.id, file.path, call.start_line, call.end_line,
               finding.severity, finding.finding_kind, finding.message
        FROM project_call_findings AS finding
        JOIN project_calls AS call ON call.id = finding.call_id
        JOIN project_files AS file ON file.id = call.file_id
        WHERE call.project_id = ?
        ORDER BY finding.id
        """,
        (project_id,),
    ).fetchall()
    findings.extend(
        ObservedFinding(
            id=f"call:{int(row['id'])}",
            analyzer="call",
            path=str(row["path"]),
            start_line=int(row["start_line"]) if row["start_line"] is not None else None,
            end_line=int(row["end_line"]) if row["end_line"] is not None else None,
            severity=str(row["severity"]),
            category=str(row["finding_kind"]),
            title=str(row["message"]),
            provenance="deterministic",
            report_tier="advisory" if row["severity"] == "info" else "defect",
        )
        for row in call_rows
    )
    sources: dict[str, str] = {}
    for row in db.execute(
        "SELECT path, content FROM project_files WHERE project_id = ?",
        (project_id,),
    ).fetchall():
        source, _encoding = decode_text_content(bytes(row["content"]))
        if source is not None:
            sources[str(row["path"])] = source
    file_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(project_files)")}
    if "parser_diagnostics_json" in file_columns:
        for row in db.execute(
            "SELECT id, path, parser_diagnostics_json FROM project_files WHERE project_id = ?",
            (project_id,),
        ):
            try:
                diagnostics = json.loads(row["parser_diagnostics_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(diagnostics, list):
                continue
            for index, diagnostic in enumerate(diagnostics):
                if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("start_line"), int):
                    continue
                findings.append(ObservedFinding(
                    id=f"parser:{row['id']}:{index}", analyzer="parser", path=str(row["path"]),
                    start_line=diagnostic["start_line"], end_line=diagnostic.get("end_line"),
                    severity="error", category="syntax", title=f"Parser syntax error: {diagnostic.get('message', '')}",
                    provenance="deterministic", failure_type="SyntaxError",
                ))
    return findings, sources


def evaluate_project(
    db: sqlite3.Connection,
    project_id: str,
    manifest: BenchmarkManifest,
    *,
    analyzer_version: str,
) -> BenchmarkMetrics:
    findings, sources = load_project_observations(db, project_id)
    return evaluate_findings(
        findings,
        manifest,
        analyzer_version=analyzer_version,
        sources=sources,
    )


def project_benchmark_details(db: sqlite3.Connection, project_id: str, manifest: BenchmarkManifest,
                              *, analyzer_version: str) -> dict[str, object]:
    """Expose per-language accuracy and recorded engine/model cost beside totals."""
    findings, sources = load_project_observations(db, project_id)
    languages = {
        path: inventory_file(InventorySourceFile(path, source.encode("utf-8")), Counter(), Counter()).language or "unknown"
        for path, source in sources.items()
    }
    per_language = {}
    for language in sorted(set(languages.values())):
        paths = {path for path, value in languages.items() if value == language}
        subset = BenchmarkManifest(
            manifest.name,
            tuple(item for item in manifest.expectations if any(_path_matches(path, item.path) for path in paths)),
            tuple(item for item in manifest.clean_regions if any(_path_matches(path, item.path) for path in paths)),
        )
        per_language[language] = evaluate_findings(
            [item for item in findings if item.path in paths], subset,
            analyzer_version=analyzer_version, sources={path: sources[path] for path in paths},
        ).to_dict()
    columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(projects)")}
    counters = [name for name in (
        "function_analysis_model_request_count", "function_analysis_batch_request_count",
        "function_analysis_deterministic_count", "function_analysis_cache_hit_count",
    ) if name in columns]
    row = db.execute(f"SELECT {', '.join(counters) if counters else 'id'} FROM projects WHERE id = ?", (project_id,)).fetchone()
    incomplete_clean_regions = []
    for region in manifest.clean_regions:
        source = next((source for path, source in sources.items() if _path_matches(path, region.path)), None)
        bounds = _region_bounds(region, source) if source is not None else None
        if bounds is None:
            incomplete_clean_regions.append(region.id)
            continue
        region_rows = [row for row in db.execute(
            """SELECT f.path,s.analysis_status,a.response_json FROM project_symbols s
            JOIN project_files f ON f.id=s.file_id
            LEFT JOIN project_symbol_analyses a ON a.symbol_id=s.id
            WHERE s.project_id=? AND s.symbol_kind IN ('function','method')
            AND s.start_line<=? AND s.end_line>=?""", (project_id, bounds[1], bounds[0])
        ) if _path_matches(row["path"], region.path)]
        if not region_rows or any(response_quality(row["response_json"], row["analysis_status"])["status"] != "complete" for row in region_rows):
            incomplete_clean_regions.append(region.id)
    return {
        "by_language": per_language,
        "execution": {name: int(row[name]) for name in counters} if row else {},
        "review_quality": project_review_quality(db, project_id),
        "incomplete_clean_region_ids": incomplete_clean_regions,
        "clean_regions_fully_reviewed": not incomplete_clean_regions,
    }


def default_mutation_recipes() -> tuple[MutationRecipe, ...]:
    """Return small clean/mutated pairs spanning function, parser, and call checks."""
    semantic = (
        MutationRecipe(
            name="python_branch_assignment",
            language="python",
            extension="py",
            clean_source=(
                "def status(enabled: bool) -> str:\n"
                "    label = 'ready'\n"
                "    return label\n"
            ),
            mutated_source=(
                "def status(enabled: bool) -> str:\n"
                "    if enabled:\n"
                "        label = 'ready'\n"
                "    return label\n"
            ),
            expectation=BenchmarkExpectation(
                id="python-branch-assignment",
                path="mutated/python_branch_assignment.py",
                source_contains="return label",
                category="type",
                title_pattern=r"assignment|unboundlocalerror",
                allowed_severities=("error", "unsafe"),
            ),
        ),
        MutationRecipe(
            name="javascript_parser_error",
            language="javascript",
            extension="js",
            clean_source="function total(value) { return value + 1; }\n",
            mutated_source="function total(value) { return value + ; }\n",
            expectation=BenchmarkExpectation(
                id="javascript-parser-error",
                path="mutated/javascript_parser_error.js",
                analyzer="parser",
                source_contains="return value + ;",
                category="syntax",
                title_pattern=r"parser|syntax",
                allowed_severities=("error",),
            ),
        ),
        MutationRecipe(
            name="typescript_missing_argument",
            language="typescript",
            extension="ts",
            clean_source=(
                "function label(value: string, suffix: string): string { return value + suffix; }\n"
                "function render(): string { return label('ok', '!'); }\n"
            ),
            mutated_source=(
                "function label(value: string, suffix: string): string { return value + suffix; }\n"
                "function render(): string { return label('ok'); }\n"
            ),
            expectation=BenchmarkExpectation(
                id="typescript-missing-argument",
                path="mutated/typescript_missing_argument.ts",
                analyzer="call",
                source_contains="return label('ok');",
                category="missing_argument",
                title_pattern=r"missing|required|argument",
                allowed_severities=("error",),
            ),
        ),
        MutationRecipe(
            name="javascript_null_member", language="javascript", extension="js",
            clean_source="function value() { return ({name: 'ok'}).name; }\n",
            mutated_source="function value() { return null.name; }\n",
            expectation=BenchmarkExpectation(
                id="javascript-null-member", path="mutated/javascript_null_member.js",
                source_contains="null.name", category="runtime", title_pattern="null|TypeError",
                allowed_severities=("error", "unsafe"),
            ),
        ),
    )
    syntax_pairs = (
        ("cpp", "cpp", "int total(int value) { return value + 1; }\n", "return value + 1", "return value +"),
        ("csharp", "cs", "class Counter { int Total(int value) { return value + 1; } }\n", "return value + 1", "return value +"),
        ("rust", "rs", "fn total(value: i32) -> i32 { value + 1 }\n", "value + 1", "value +"),
        ("shell", "sh", "total() { if true; then printf '%s' ok; fi; }\n", "then", ""),
        ("powershell", "ps1", "function Total { return (1 + 2) }\n", "1 + 2", "1 +"),
    )
    return semantic + tuple(
        MutationRecipe(
            name=f"{language}_parser_error", language=language, extension=extension,
            clean_source=clean, mutated_source=clean.replace(before, after, 1),
            expectation=BenchmarkExpectation(
                id=f"{language}-parser-error", path=f"mutated/{language}_parser_error.{extension}",
                analyzer="parser", line=1, category="syntax", title_pattern="parser|syntax", allowed_severities=("error",),
            ),
        )
        for language, extension, clean, before, after in syntax_pairs
    )


def materialize_mutation_corpus(destination: str | Path) -> Path:
    """Create a deterministic multi-language clean/mutated benchmark project."""
    root = Path(destination)
    (root / "clean").mkdir(parents=True, exist_ok=True)
    (root / "mutated").mkdir(parents=True, exist_ok=True)
    expected: list[dict[str, object]] = []
    clean_regions: list[dict[str, object]] = []
    for recipe in default_mutation_recipes():
        clean_path = root / "clean" / f"{recipe.name}.{recipe.extension}"
        mutated_path = root / "mutated" / f"{recipe.name}.{recipe.extension}"
        clean_path.write_text(recipe.clean_source, encoding="utf-8")
        mutated_path.write_text(recipe.mutated_source, encoding="utf-8")
        expected.append(asdict(recipe.expectation))
        clean_regions.append(
            {
                "id": f"clean-{recipe.name}",
                "path": f"clean/{recipe.name}.{recipe.extension}",
                "start_contains": recipe.clean_source.splitlines()[0],
            }
        )
    manifest_path = root / "benchmark_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "name": "Generated multi-language mutation benchmark",
                "expected": expected,
                "clean_regions": clean_regions,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _current_analyzer_version() -> str:
    from project_function_analysis import _cache_contract_version

    return _cache_contract_version()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="Create the mutation corpus")
    generate.add_argument("destination", type=Path)
    evaluate = subparsers.add_parser("evaluate", help="Score a persisted project report")
    evaluate.add_argument("database", type=Path)
    evaluate.add_argument("project_id")
    evaluate.add_argument("manifest", type=Path)
    evaluate.add_argument(
        "--analyzer-version",
        help="Override the version label when scoring a historical report.",
    )
    arguments = parser.parse_args()
    if arguments.command == "generate":
        print(materialize_mutation_corpus(arguments.destination))
        return 0
    db = sqlite3.connect(arguments.database.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        manifest = load_manifest(arguments.manifest)
        version = arguments.analyzer_version or _current_analyzer_version()
        metrics = evaluate_project(
            db,
            arguments.project_id,
            manifest,
            analyzer_version=version,
        )
        details = project_benchmark_details(db, arguments.project_id, manifest, analyzer_version=version)
    finally:
        db.close()
    print(json.dumps({**metrics.to_dict(), **details}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
