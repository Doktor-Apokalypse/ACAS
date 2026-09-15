"""Source-derived output estimates and bounded, user-isolated usage calibration."""
from __future__ import annotations

import ast
import builtins
import hashlib
import json
import math
import sqlite3
import textwrap
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, TypeVar

from language_adapters.registry import get_adapter

VERSION = "structural-output-v1"
TIERS = (4096, 8192, 16384)
SEMANTIC_TIERS = (1024, 2048, 4096)


def _is_qwen25_coder_model(model_name: str) -> bool:
    """Recognize Ollama tags and Lemonade's catalog name for Qwen2.5-Coder."""
    normalized = model_name.strip().casefold().replace("_", "-")
    base = normalized.split(":", 1)[0]
    return base == "qwen2.5-coder" or base.startswith("qwen2.5-coder-")


@lru_cache(maxsize=8)
def module_bindings(content: bytes) -> set[str]:
    """Exclude module-level assignments/imports that shadow built-in names."""
    try:
        stack = [ast.parse(content)]
    except (SyntaxError, ValueError, RecursionError):
        return set(dir(builtins))
    names = set()
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return set(dir(builtins))
                names.add(alias.asname or alias.name.split(".")[0])
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def structural_features(language: str, source: str) -> dict:
    f = dict(branches=0, loops=0, nesting=0, handlers=0, returns=0,
             parameters=[], known_builtins=[], uncertainty=[], source_characters=len(source))
    if language == "python":
        try:
            tree = ast.parse(textwrap.dedent(source))
            target = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
            args = target.args
            f["parameters"] = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                                                *([args.vararg] if args.vararg else []),
                                                *([args.kwarg] if args.kwarg else [])]]
            bound = set(f["parameters"])
            calls = set()
            stack = [(target, 0)]
            while stack:
                node, depth = stack.pop()
                if node is not target and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    bound.add(getattr(node, "name", ""))
                    continue
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound.add(node.id)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    bound.update(a.asname or a.name.split(".")[0] for a in node.names)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                branch = isinstance(node, (ast.If, ast.IfExp, ast.match_case))
                loop = isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.comprehension))
                handler = isinstance(node, ast.ExceptHandler)
                f["branches"] += int(branch) + (len(node.values)-1 if isinstance(node, ast.BoolOp) else 0)
                f["loops"] += int(loop)
                f["handlers"] += int(handler)
                f["returns"] += int(isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)))
                depth += int(branch or loop or handler)
                f["nesting"] = max(f["nesting"], depth)
                stack.extend((child, depth) for child in ast.iter_child_nodes(node))
            f["known_builtins"] = sorted(calls & set(dir(builtins)) - bound)
            return f
        except (SyntaxError, StopIteration, ValueError, RecursionError):
            f["uncertainty"].append("Python function could not be fully parsed")
    else:
        adapter = get_adapter(language)
        if adapter:
            try:
                tree = adapter.new_parser().parse(source.encode("utf-8"))
                if tree.root_node.has_error:
                    f["uncertainty"].append("Fragment or syntax errors limit structural measurements")
                branches = {"if_statement", "if_expression", "elif_clause", "else_if_clause", "conditional_expression", "ternary_expression", "switch_case", "case_statement", "match_arm"}
                loops = {"for_statement", "for_in_statement", "for_expression", "while_statement", "while_expression", "do_statement", "loop_expression", "foreach_statement"}
                handlers = {"catch_clause", "except_clause", "trap_statement"}
                definitions = {"function_definition", "function_declaration", "method_declaration", "function_item", "arrow_function", "function_expression", "lambda_expression"}
                stack = [(tree.root_node, 0)]
                found_target = False
                found_parameters = False
                while stack:
                    node, depth = stack.pop()
                    kind = node.type
                    if kind in definitions:
                        if found_target:
                            continue
                        found_target = True
                    if kind in {"parameters", "formal_parameters", "parameter_list"} and not found_parameters:
                        f["parameters"] = [child.text.decode("utf-8", errors="replace")[:100]
                                           for child in node.named_children if "comment" not in child.type]
                        found_parameters = True
                    branch, loop, handler = kind in branches, kind in loops, kind in handlers
                    f["branches"] += int(branch)
                    f["loops"] += int(loop)
                    f["handlers"] += int(handler)
                    f["returns"] += int(kind in {"return_statement", "return_expression", "yield_expression"})
                    depth += int(branch or loop or handler)
                    f["nesting"] = max(f["nesting"], depth)
                    stack.extend((child, depth) for child in reversed(node.named_children))
                if not found_target:
                    f["uncertainty"].append("No complete function boundary recognized")
                return f
            except (ImportError, OSError, ValueError, RuntimeError):
                f["uncertainty"].append("Language parser unavailable")
        else:
            f["uncertainty"].append("No structural parser for this language")
    # Never count keywords in comments/strings as if they were parsed branches.
    return f


def estimate_output(features: dict, dependencies: dict, history: list[dict] = ()) -> dict:
    uncertainty = list(features["uncertainty"])
    dependency_count = dependencies.get("resolved", 0) + dependencies.get("unresolved", 0)
    c = min(100, 1 + 3*features["branches"] + 5*features["loops"] +
            6*max(0, features["nesting"]-1) + 4*features["handlers"] + 2*max(0, features["returns"]-1))
    d = min(100, 1 + 3*dependencies.get("resolved", 0) + 8*dependencies.get("unresolved", 0)
            + 2*dependencies.get("cross_file", 0) + 15*bool(dependencies.get("recursive")))
    if uncertainty:
        c = max(c, 50)
    if dependencies.get("unresolved", 0):
        uncertainty.append("Some callees have no resolved source contract")
    parameters = features["parameters"]
    skeleton = {"summary": "", "syntax_valid": True, "parameters": [{"name": p, "kind": "unknown", "required": True,
                "accepted_types": ["unknown"], "default_description": None, "description": ""} for p in parameters],
                "returns": {"may_return_value": True, "possible_types": [{"type": "unknown", "description": ""}],
                            "nullable": False, "description": ""}, "raised_errors": [], "side_effects": [], "issues": [],
                "contract_version": "1.0", "confidence": 0.5}
    if features.get("semantic_review"):
        skeleton = {
            "behavior_claims": [{"kind": "return", "start_line": 1,
                                 "end_line": 1, "evidence": "return value"}],
            "parameter_inferences": [
                {"name": p, "accepted_types": ["unknown"], "start_line": 1,
                 "end_line": 1, "evidence": "source expression"}
                for p in parameters
            ],
            "return_has_value": True,
            "return_types": ["unknown"],
            "return_nullable": False,
            "return_line": 1,
            "return_evidence": "return value",
            "escaping_errors": [],
            "side_effects": [],
            "issues": [],
        }
    # Approximate JSON token density plus prose/evidence allowance, never an
    # instruction to invent findings or a measured tokenizer count.
    j = math.ceil((len(json.dumps(skeleton, ensure_ascii=False).encode("utf-8")) +
                   140*len(parameters) + 180*max(1, features["returns"]) + 260*math.ceil(c/25))/3) + 350
    reasoning = 1024 + 32*c + 16*d + (1024 if features["uncertainty"] else 0)
    if features.get("dedicated_reasoning") is False:
        reasoning = 0
    required = math.ceil(1.25*(j + reasoning))
    floor = 0
    reasons = ["structural estimate"]
    tiers = SEMANTIC_TIERS if features.get("semantic_review") else TIERS
    exact = [h for h in history if h.get("exact")]
    for h in exact:
        if h.get("truncated"):
            floor = max(floor, next((t for t in tiers if t > h["output_limit"]), tiers[-1]))
            reasons.append("previous truncation")
    completed = [int(h["generated_tokens"]) for h in history
                 if h.get("outcome") == "valid_response" and h.get("generated_tokens") is not None and not h.get("truncated")]
    exact_completed = [int(h["generated_tokens"]) for h in exact
                       if h.get("outcome") == "valid_response" and h.get("generated_tokens") is not None and not h.get("truncated")]
    samples = exact_completed or (completed if len(completed) >= 8 else [])
    if samples:
        samples.sort()
        floor = max(floor, math.ceil(samples[math.ceil(.9*len(samples))-1]*1.15))
        reasons.append("observed generation usage")
    required = max(required, floor)
    tier = next((t for t in tiers if t >= required), tiers[-1])
    return dict(version=VERSION, complexity_score=c, dependency_score=d, dependency_count=dependency_count,
                estimated_json_tokens=j,
                estimated_reasoning_tokens=reasoning, estimated_total_tokens=required, output_tokens=tier,
                ceiling_exceeded=required>tiers[-1], history_samples=len(history), reasons=sorted(set(reasons)),
                uncertainty=uncertainty, features=features, dependencies=dependencies)


def prepare_budget(db, task, *, source: str | None = None, recursive: bool = False, semantic_review: bool = False) -> dict:
    source = task.source if source is None else source
    features = structural_features(task.language, source)
    if semantic_review:
        from app_config import OLLAMA_MODEL, current_ollama_model
        features["semantic_review"] = True
        features["dedicated_reasoning"] = not _is_qwen25_coder_model(
            current_ollama_model(OLLAMA_MODEL)
        )
    rows = db.execute("""SELECT c.callee,c.resolution_status,c.resolved_symbol_id,s.file_id
                         FROM project_calls c LEFT JOIN project_symbols s ON s.id=c.resolved_symbol_id
                         WHERE c.project_id=? AND c.caller_symbol_id=?""", (task.project_id, task.symbol_id)).fetchall()
    # Shadowed built-ins are excluded by the structural pass. A module-level
    # binding also prevents treating an unresolved name as a known built-in.
    bindings = {r[0] for r in db.execute("SELECT name FROM project_symbols WHERE file_id=?", (task.file_id,))}
    known = set(features["known_builtins"]) - bindings
    if known:
        content = bytes(db.execute("SELECT content FROM project_files WHERE id=?", (task.file_id,)).fetchone()[0])
        known -= module_bindings(content) if len(content) <= 1_000_000 else set(known)
    resolved = {r["resolved_symbol_id"] for r in rows if r["resolution_status"] == "internal"}
    unresolved = {r["callee"] for r in rows if r["resolution_status"] != "internal" and r["callee"] not in known}
    cross_file = {r["resolved_symbol_id"] for r in rows if r["resolution_status"] == "internal" and r["file_id"] != task.file_id}
    deps = dict(resolved=len(resolved), unresolved=len(unresolved), cross_file=len(cross_file),
                recursive=recursive or task.symbol_id in resolved)
    initial = estimate_output(features, deps)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    from app_config import OLLAMA_MODEL, OLLAMA_GPT_OSS_REASONING, current_ollama_model
    reasoning_profile = "none" if features.get("dedicated_reasoning") is False else OLLAMA_GPT_OSS_REASONING
    model = current_ollama_model(OLLAMA_MODEL) + ":reasoning=" + reasoning_profile
    if semantic_review:
        model += ":semantic-v4"
    rows = db.execute("""SELECT source_hash,generated_tokens,output_limit,truncated,outcome
                         FROM function_analysis_usage WHERE user_id=? AND model=? AND language=? AND budget_version=?
                         AND request_kind='single' AND (source_hash=? OR (complexity_bucket=? AND dependency_bucket=?))
                         ORDER BY id DESC LIMIT 80""",
                      (task.user_id, model, task.language, VERSION, digest, initial["complexity_score"]//20, initial["dependency_score"]//20)).fetchall()
    history = [{**dict(row), "exact": row["source_hash"] == digest} for row in rows]
    result = estimate_output(features, deps, history)
    result.update(source_hash=digest, model=model, symbol_name=task.qualified_name)
    return result


@dataclass
class RequestBudget:
    output_tokens: int
    emit: Callable[[dict[str, object]], None]


class BudgetTask(Protocol):
    symbol_id: int
    project_id: str
    user_id: int
    file_id: int
    language: str
    qualified_name: str
    source: str


class ReviewResult(Protocol):
    review_status: str


BudgetPayload = dict[str, object]
ReviewPayload = ReviewResult | dict[str, ReviewResult]
ReviewPayloadT = TypeVar("ReviewPayloadT", bound=ReviewPayload)
ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


_active: ContextVar[RequestBudget | None] = ContextVar("function_output_budget", default=None)


def selected_output_limit(default: int) -> int:
    active = _active.get()
    return active.output_tokens if active else default


def observe_usage(event: dict[str, object]) -> None:
    active = _active.get()
    if active:
        active.emit(event)


def save_budget(
    db: sqlite3.Connection,
    task: BudgetTask,
    budget: BudgetPayload,
) -> None:
    db.execute("""INSERT INTO function_analysis_budgets(symbol_id,budget_json) VALUES (?,?)
                  ON CONFLICT(symbol_id) DO UPDATE SET budget_json=excluded.budget_json,updated_at=CURRENT_TIMESTAMP""",
               (task.symbol_id, json.dumps(budget)))


@contextmanager
def budgeted_request(
    connection_factory: ConnectionFactory,
    tasks: Sequence[BudgetTask],
    budgets: Sequence[BudgetPayload],
    *,
    batch: bool = False,
) -> Iterator[Callable[[ReviewPayload], None]]:
    """Isolate each request, persist only counters, and never assign batch tokens to a function."""
    from app_config import FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT, FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS, FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS
    if not tasks or not budgets or len(tasks) != len(budgets):
        raise ValueError("Budgeted requests require one budget for every task")
    required = (
        sum(int(b["estimated_total_tokens"]) for b in budgets)
        if batch else int(budgets[0]["output_tokens"])
    )
    features = budgets[0].get("features")
    if not batch and isinstance(features, dict) and features.get("semantic_review"):
        limit = int(budgets[0]["output_tokens"])
    else:
        limit = next((t for t in TIERS if t>=required), TIERS[-1])
    if batch:
        limit = min(limit, FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS)
    if not FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT:
        limit = FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS if batch else FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS
    events: list[dict[str, object]] = []
    def emit(event: dict[str, object]) -> None:
        if event.get("event") == "start":
            with connection_factory() as db:
                for task, budget in zip(tasks, budgets):
                    budget = {**budget, "active_output_tokens": event["output_limit"], "request_kind": "batch" if batch else "single"}
                    save_budget(db, task, budget)
        else:
            events.append(event)
    token = _active.set(RequestBudget(limit, emit))
    outcome = "error"
    def validated(result: ReviewPayload) -> None:
        nonlocal outcome
        results = result.values() if isinstance(result, dict) else [result]
        outcome = "valid_response" if all(r.review_status == "complete" for r in results) else "invalid_response"
    try:
        yield validated
    finally:
        _active.reset(token)
        if events:
            task, budget = tasks[0], budgets[0]
            with connection_factory() as db:
                for index, event in enumerate(events):
                    db.execute("""INSERT INTO function_analysis_usage(
                        user_id,project_id,symbol_id,source_hash,model,language,budget_version,
                        complexity_bucket,dependency_bucket,output_limit,generated_tokens,prompt_tokens,
                        reasoning_characters,answer_characters,truncated,outcome,request_kind)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        task.user_id,task.project_id,task.symbol_id,budget["source_hash"],budget["model"],task.language,VERSION,
                        budget["complexity_score"]//20,budget["dependency_score"]//20,event["output_limit"],
                        event.get("generated_tokens"),event.get("prompt_tokens"),event.get("reasoning_characters",0),
                        event.get("answer_characters",0),int(event.get("truncated",False)),
                        outcome if index == len(events)-1 and not event.get("truncated") else "invalid_response",
                        "batch" if batch else "single"))
                db.execute("""DELETE FROM function_analysis_usage WHERE user_id=? AND id NOT IN
                              (SELECT id FROM function_analysis_usage WHERE user_id=? ORDER BY id DESC LIMIT 10000)""",
                           (task.user_id, task.user_id))


def run_budgeted(
    connection_factory: ConnectionFactory,
    tasks: Sequence[BudgetTask],
    budgets: Sequence[BudgetPayload],
    call: Callable[..., ReviewPayloadT],
    *,
    batch: bool = False,
    **kwargs: object,
) -> ReviewPayloadT:
    with budgeted_request(connection_factory, tasks, budgets, batch=batch) as validated:
        result = call(**kwargs)
        validated(result)
        return result
