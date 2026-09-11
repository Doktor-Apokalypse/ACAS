"""Migration-derived and curated application changelog support."""

from __future__ import annotations

import argparse
import html
import inspect
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from migrations import (
    MIGRATIONS,
    MIGRATION_CHANGE_ACTIONS,
    MIGRATION_RECORDED_AT_UTC,
)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"


@dataclass(frozen=True)
class ChangelogEntry:
    recorded_at_utc: str
    area: str
    action: str
    title: str
    details: tuple[str, ...]
    source: str
    migration_version: int | None = None
    migration_name: str | None = None
    installed: bool | None = None
    applied_at_utc: str | None = None


MANUAL_CHANGELOG_ENTRIES: tuple[ChangelogEntry, ...] = (
    ChangelogEntry(
        recorded_at_utc="2026-09-11 20:05:15 UTC",
        area="Documentation",
        action="Changed",
        title="Reworked installation and usage guidance",
        details=(
            "Replace the Windows-only setup summary with common preparation and step-by-step Windows Command Prompt, Windows PowerShell, Linux and macOS instructions.",
            "Document required SMTP and owner setup, first sign-in, Lemonade configuration and installation health checks for new users.",
            "Reduce project upload documentation to supported file, folder and ZIP inputs and supported analysis languages.",
            "Remove the analysis benchmark documentation and condense account ownership to its principal roles and controls.",
        ),
        source="README.md and changelog.py",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 19:41:12 UTC",
        area="Privacy",
        action="Changed",
        title="Removed machine-specific and personal defaults",
        details=(
            "Remove the previously configured NTFY topic and personal owner identity from documentation and application defaults.",
            "Default error notifications to disabled until NTFY_TOPIC is explicitly configured.",
            "Use generic owner defaults and remove the personal username from the password blocklist.",
            "Resolve the standalone launcher's project directory from the script location instead of a machine-specific user path.",
            "Replace example email addresses in the README command snippet with neutral placeholders.",
            "Label the README's NTFY_TOPIC setting with the neutral text NTFY notification topic name.",
        ),
        source="README.md, app_config.py, authentication.py, Start-AnalysisEngine.ps1 and changelog.py",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 19:36:05 UTC",
        area="Documentation",
        action="Changed",
        title="Removed the standalone Windows launcher instructions",
        details=(
            "Remove the README section describing how to start the application outside PyCharm.",
        ),
        source="README.md and changelog.py",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 19:23:43 UTC",
        area="Repository",
        action="Changed",
        title="Prepared safe defaults for source control",
        details=(
            "Ignore alternate local virtual-environment directories such as .venv313 in every clone.",
            "Replace the local notification topic example with an explicit placeholder before publishing the repository.",
            "Define consistent cross-platform line endings for source, documentation, data and PowerShell files.",
        ),
        source=".gitignore, .gitattributes, .env.example and changelog.py",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 19:06:10 UTC",
        area="Function analysis",
        action="Fixed",
        title="Accepted source-verifiable behavior claim shape deviations",
        details=(
            "Diagnose require_admin as failing after three attempts because the model used the unsupported behavior kind raise for an exact exception guard.",
            "Infer the supported behavior kind from the claim's evidence before strict validation, while retaining the existing source-line, excerpt and kind verification.",
            "Retain the first claim when a model returns more than the one behavior claim requested by the compact schema, fixing the related enforce_project_storage_limit failure.",
        ),
        source="semantic_review.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 18:13:51 UTC",
        area="Function analysis",
        action="Changed",
        title="Displayed actual function dependency counts",
        details=(
            "Replace the WebUI's weighted dependency score with the actual number of distinct resolved and unresolved callees for the function.",
            "Retain the existing resolved, unresolved, cross-file and recursion weights internally for output and reasoning budget selection.",
            "Derive the visible count from stored dependency details for existing budgets so completed analyses do not need to be rerun.",
        ),
        source="function_budget.py, project_tree_assets.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 17:27:37 UTC",
        area="Project explorer",
        action="Added",
        title="Function source details and caller inspection",
        details=(
            "Show each indexed function declaration and its owned return statements in the tree hover text without repeating exact source already present in the description.",
            "Label multiple return paths as flow dependent with their source lines, while excluding returns owned by nested Python functions.",
            "Open a keyboard-accessible caller dialog when a function is clicked, listing statically resolved project callers, enclosing functions or module scope, locations, usage kinds and exact call expressions.",
        ),
        source="project_tree_metadata.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-11 16:48:16 UTC",
        area="Function analysis",
        action="Fixed",
        title="Completed descriptions after repeated invalid model line anchors",
        details=(
            "Diagnose _drop_issues_outside_lines as a complete typed contract whose Hybrid behavior claim was rejected because its line range fell outside the uploaded function on both bounded attempts.",
            "After both model behavior anchors fail, select and verify one exact target-source statement for the engine-owned description while retaining the rejected model claims and explicit fallback provenance.",
            "Prefer a complete return statement over partial multiline container setup in the compact response template, and keep unresolved parameter or return contracts partial.",
        ),
        source="semantic_review.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:35:21 UTC",
        area="Project tree",
        action="Fixed",
        title="Descriptions for deterministically completed functions",
        details=(
            "Expose completed deterministic summaries in function-tree tooltips instead of showing Unknown solely because no model request was needed.",
            "Generate a source-derived validator description containing its guarded condition, built-in exception and return type; identify it as Static analysis in the tooltip.",
            "Keep Get description available for deterministic results so a user can still request a forced LLM review, while completed model descriptions retain their existing presentation.",
        ),
        source="project_function_analysis.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:28:04 UTC",
        area="Function analysis",
        action="Fixed",
        title="Deterministic completion for explicit built-in raises",
        details=(
            "Identify FunctionIssue.valid_line_order as a fully source-proven validator whose only previously unsupported call was the ValueError constructor used directly by raise.",
            "Allow built-in exception constructors only when they are the direct expression of a raise statement, so small complete validators bypass unreliable model coordinate output without treating arbitrary calls or user-defined exceptions as pure.",
            "Verify the actual indexed Fixed/analysis_engine.py symbol now has a complete deterministic receiver, FunctionIssue return, ValueError raise and zero unresolved issues.",
        ),
        source="project_function_analysis.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:20:31 UTC",
        area="Function analysis",
        action="Fixed",
        title="Recover uniquely matching multiline behavior evidence",
        details=(
            "Diagnose repeated FunctionIssue.valid_line_order and _drop_issues_outside_lines failures as two complete, non-truncated Hybrid responses whose behavior coordinates fell outside both accepted line systems.",
            "Relocate a behavior claim when its evidence matches exactly one target-source token sequence after whitespace-only normalization, then store the original exact multiline source excerpt and corrected absolute lines.",
            "Keep ambiguous or changed evidence rejected, and make every prompt field consistently request 1-based source-relative coordinates.",
        ),
        source="semantic_review.py, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:08:41 UTC",
        area="Project workspace",
        action="Changed",
        title="Project selection locked to active analysis",
        details=(
            "Keep independently uploaded projects selectable while no project analysis is running, so each project tree and report can be revisited from the same workspace.",
            "Select and pin the project that starts an analysis, disable the project dropdown until that job completes, fails or is cancelled, and reject programmatic selection changes while it is locked.",
            "Retain the server-side project ID on every analysis job, preventing a UI selection from changing the files or results used by work already in progress.",
        ),
        source="project_tree_assets.py, web_assets.py, README.md, changelog.py and browser regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:08:40 UTC",
        area="Project tree",
        action="Added",
        title="Top-level project rename and delete actions",
        details=(
            "Add a three-dot action menu beside the project name at the root of the file tree with Rename and Delete actions.",
            "Persist renamed project names immediately across the selector, project status and tree, and remove a confirmed project with its uploaded files, parsed structure, jobs and stored report.",
            "Refuse project deletion while its analysis is queued or running and retain user-ownership checks for both operations.",
        ),
        source="api_models.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:08:39 UTC",
        area="Function analysis",
        action="Fixed",
        title="Recovered more valid function reviews and parameter contracts",
        details=(
            "Increase exact-source evidence fields from 500 to 1000 characters and bound overlong evidence locally, allowing longer valid claims to survive strict result validation.",
            "Repair invalid JSON Unicode and backslash-newline escapes and flatten a recognized legacy return_fields wrapper while continuing to reject unknown or contradictory fields.",
            "Infer unresolved Python parameter contracts conservatively from resolved dependency signatures, matching method override signatures, callable use and accessed attributes, and include matching override contracts in method context.",
            "Treat a bare yield as a source-proven no-value path, retain review for value-yielding generators and version the source-fact contract so incompatible earlier cache entries are not reused.",
        ),
        source="analysis_engine.py, semantic_review.py, project_function_analysis.py, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 22:08:38 UTC",
        area="Project tree",
        action="Changed",
        title="Distinct active and failed analysis colours",
        details=(
            "Show the currently analysed file and function in cyan while reserving red for functions whose analysis failed.",
            "Show a file in red when all of its processed functions failed, yellow when completed results are mixed between passed and failed, and preserve the normal completed and main-file colours otherwise.",
        ),
        source="project_tree_assets.py, README.md, changelog.py and browser regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 19:15:43 UTC",
        area="WebUI",
        action="Changed",
        title="Full-width analysis-only workspace",
        details=(
            "Rename the interface to Apokalypse Code Analysis System and make pasted source submissions use analysis mode without displaying a mode switch.",
            "Remove the visible new-chat and chat-history sidebar so the project tree and live analysis output use the full viewport width.",
            "Group project controls against the right edge of the status bar, leaving the remaining width for the project, language, file count, progress and current function text.",
        ),
        source="web_assets.py, README.md, changelog.py and WebUI regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 19:04:20 UTC",
        area="Project tree",
        action="Fixed",
        title="Duplicate folder-upload root branches",
        details=(
            "Collapse the first stored path segment into its matching folder-upload branch so a selected directory such as Fixed appears only once in the tree.",
            "Retain the complete stored path behind visible nested-folder and file actions, including deletion and main-file selection.",
        ),
        source="project_tree_assets.py, README.md, changelog.py and browser regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 18:52:49 UTC",
        area="Project tree",
        action="Changed",
        title="Longer function hover descriptions",
        details=(
            "Increase the function-tree hover-description allowance from 249 to 400 characters so complete engine-generated return-contract sentences remain visible.",
            "Keep whitespace compact and retain an ellipsis for summaries longer than the new bounded tooltip allowance.",
        ),
        source="main.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 18:13:32 UTC",
        area="Model transport",
        action="Fixed",
        title="Lemonade Hybrid chat compatibility",
        details=(
            "Route Lemonade `*-Hybrid` models through its native streaming OpenAI chat endpoint because its Ollama-compatible chat adapter produced malformed, schema-ignoring output for project function reviews.",
            "Continue using the Ollama-compatible endpoint for ordinary Ollama models, and retain cancellation, request deadlines, usage tracking and repetition detection on both transports.",
            "Recover bounded local-model JSON mistakes including wrapped objects, Python-style mappings, literal control characters and surplus text after a complete object while preserving strict schema and source-evidence validation.",
            "Clarify that semantic evidence must be copied verbatim from source code rather than supplied as prose.",
            "Replace the verbose JSON Schema for Lemonade Hybrid semantic reviews with a compact required-output template anchored to a real function-body statement, repeat it after validation errors, reject declaration-only behavior claims and spend the existing repair attempt when no source-grounded behavior survives.",
        ),
        source="analysis_engine.py, semantic_review.py, function_budget.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 12:07:50 UTC",
        area="Project tree",
        action="Added",
        title="Function branches and on-demand descriptions",
        details=(
            "List indexed functions as collapsible children of each source file and mirror active, paused, completed, failed and skipped analysis states on their tree rows.",
            "Show a sub-250-character description from completed model-reviewed summaries on hover, while functions without a completed LLM review display Unknown.",
            "Offer Get description on unknown function rows and queue one forced semantic model review for exactly that symbol through the existing cancellable job worker.",
            "Reuse the verified analysis summary instead of expanding the model JSON contract with a duplicate description field.",
        ),
        source="project_tree_assets.py, main.py, project_function_analysis.py, migrations.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 11:40:37 UTC",
        area="WebUI",
        action="Added",
        title="Visible configured LLM model",
        details=(
            "Show the configured Ollama model in a compact badge beside the WebUI title, so the model selected for chat and project analysis is visible before work starts.",
            "Escape the configured model name before rendering it and keep the badge readable on desktop and narrow-screen layouts.",
        ),
        source="web_assets.py, main.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-10 07:17:08 UTC",
        area="Analysis",
        action="Fixed",
        title="Bounded function runtime and recovered evidence anchors",
        details=(
            "Measured the live 494-function Qwen run at 90 completed, 137 failed and one processing result after 218 model requests; 223 dependency positions required more than seven and a half hours.",
            "Include current project counters in queued and processing job responses, so live progress reports stored completed and failed results instead of displaying zero until the job ends.",
            "Recover a model claim whose exact source evidence occurs once inside the target even when Qwen supplies an invalid line, while keeping repeated or absent excerpts rejected and recording each correction.",
            "Request one concise behavior claim per function while retaining the full issue, error and side-effect collections, reducing non-finding output that dominated slow local generation.",
            "Bound each function, chunk or batch generation to five minutes by default without shortening ordinary chat requests; timed-out reviews remain incomplete and the project continues.",
        ),
        source="app_config.py, analysis_engine.py, semantic_review.py, main.py, Start-AnalysisEngine.ps1, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 23:12:28 UTC",
        area="Analysis",
        action="Fixed",
        title="Dependency-order progress labels and Qwen relative evidence lines",
        details=(
            "Diagnosed the apparent function-counter regression as dependency ordering moving from source position 27 to source position 14 in the same 65-function file; the persisted work sequence continued from 22 to 23 and no completed work was erased.",
            "Show monotonic finished project-result counts separately and label the current function number as its dependency-ordered source position, so a backwards source jump is no longer presented as lost progress.",
            "Measured the active evidence-format run at 11 completed, 22 failed and one processing function after 24 model requests; stored rejection metadata showed Qwen consistently emitted function-relative lines while the prompt required absolute lines.",
            "Request 1-based source-relative coordinates, require at least one behavior claim, verify every excerpt inside the supplied target, and convert proven relative coordinates to absolute file lines before storage; already-absolute proven coordinates remain compatible.",
            "Correct behavior-kind labels locally when exact evidence supports a deterministic category, record coordinate and kind corrections, and keep unsupported evidence rejected.",
            "Isolate semantic-v4 usage history and invalidate older pending cache/fact contracts; inspect the active database read-only and save originals under backups/progress-lines-20260909-230838.",
        ),
        source="semantic_review.py, function_budget.py, project_function_analysis.py, web_assets.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 21:40:18 UTC",
        area="Analysis",
        action="Changed",
        title="Evidence-anchored semantic reviews and engine-owned confidence",
        details=(
            "Replace Qwen-authored summaries, uncertainty and confidence with engine-generated values derived from the verified target, exact source excerpts, local contracts and explicit confidence caps.",
            "Require absolute line ranges and exact source evidence for behavior, parameter, return, escaping-error, side-effect and issue claims; discard unsupported individual claims locally and retain rejection metadata without spending a model retry.",
            "Reserve the one repair generation for malformed or truncated structured output and unavailable tool requests, while preserving partial status for unresolved required behavior or contracts.",
            "Use separate 1024/2048/4096 semantic output tiers, advance a tier after matching truncation when possible, isolate semantic-v3 usage history, and invalidate older cache/fact contracts.",
            "Check a rolling 3000-character response tail every 500 characters and interrupt two consecutive strong repetition detections at roughly 3500 answer characters.",
            "Preserve the database schema and stopped live analysis data; save originals under backups/semantic-evidence-20260909-222602.",
        ),
        source="semantic_review.py, function_budget.py, analysis_engine.py, project_function_analysis.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 19:27:55 UTC",
        area="Analysis",
        action="Changed",
        title="Flat Qwen return inference and early loop interruption",
        details=(
            "Infer conservative unannotated Python return relationships locally for typed parameters, self/cls, literals and containers, unshadowed built-in constructors, known conditional branches and known boolean fallback operands; retain model inference for fallthrough, generators, unknown calls and shadowed constructors.",
            "Replace the nested model-facing return contract with four flat fields, force null/empty values when return inference is unnecessary, validate cross-field coherence locally and construct the existing nested storage contract inside the engine.",
            "Interrupt a streaming response after two consecutive strong repetition detections at roughly 7000 answer characters, record the failed usage sample and retain the existing single transport retry rather than consuming the full output allowance.",
            "Version source facts, function-analysis cache entries and compact-response usage history so older response shapes and truncation samples do not contaminate the new Qwen comparison.",
            "Preserve the database schema and live analysis data; save verified originals under backups/qwen-flat-return-20260909-201500.",
        ),
        source="project_function_analysis.py, semantic_review.py, analysis_engine.py, function_budget.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 18:15:40 UTC",
        area="Analysis",
        action="Fixed",
        title="Qwen-specific semantic response constraints",
        details=(
            "Measured faster Qwen2.5-Coder generation but only three accepted model reviews against nineteen failed functions in the initial compact-contract pass; most failures filled inference fields already owned by the engine.",
            "Specialized each response schema from source facts, requiring an empty parameter-inference array and null return inference when those values are already known, restricting unresolved parameter names, and encoding coherent value/no-value return alternatives.",
            "Discarded only irrelevant or duplicate model guesses before nested validation while preserving strict validation for required inferences, source evidence, unknown fields and engine-owned contracts; recorded ignored wire fields in source-fact metadata.",
            "Retained zero-item array bounds in the Ollama decoding grammar without restoring expensive general repetition bounds, and kept one bounded repair for genuinely invalid semantic output.",
            "Documented that Qwen2.5-Coder has no Ollama reasoning-level control and that OLLAMA_GPT_OSS_REASONING remains specific to GPT-OSS; saved verified originals under backups/qwen-contract-20260909-191021.",
        ),
        source="semantic_review.py, analysis_engine.py, README.md, changelog.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 16:02:23 UTC",
        area="Analysis",
        action="Changed",
        title="Engine-owned facts and compact semantic reviews",
        details=(
            "Run existing local completion checks across selected pending functions before model work, then review remaining functions in dependency order and refresh persisted progress after each result.",
            "Extract Python and supported TypeScript signature facts locally; request only semantic summaries, missing type inferences, escaping exceptions, side effects, proof-bearing issues and explicit uncertainties in a smaller strict JSON response.",
            "Keep source declarations distinct from runtime behavior, caught raise occurrences separate from escaping exceptions, and unresolved single/chunk inferences partial; preserve existing source-proof validation and legacy review interfaces for other signatures.",
            "Reject unexpected tool calls and allow one semantic repair attempt; retain cancellation, backend-failure handling, completed reviews and the 20000-character source/context limits.",
            "Estimate compact JSON output separately, omit a dedicated thinking allowance for Qwen2.5-Coder, isolate usage history by response format and update cache versioning without a database migration.",
            "Document the exact Qwen model tag, PyCharm settings, model comparison and native-context limits; save verified originals and restoration instructions under backups/engine-facts-20260909-170223.",
        ),
        source="semantic_review.py, analysis_engine.py, project_function_analysis.py, function_budget.py, README.md and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 15:11:15 UTC",
        area="Startup",
        action="Changed",
        title="Lower-reasoning single-function desktop analysis profile",
        details=(
            "Diagnosed active GPU generation with high reasoning, repeated output exhaustion before any answer text, and only eight valid responses among 24 recorded batch responses in the current pass.",
            "Changed the desktop launcher's defaults to low GPT-OSS reasoning and one function per request, avoiding the expensive batch prepass while retaining source context, output tiers and review validation.",
            "Retained inherited environment overrides and documented replacing the desktop copy, checking effective settings and resuming completed work without resetting the project.",
            "Saved verified originals under backups/ollama-throughput-20260909-161115; the current engine process and live analysis data were left untouched.",
        ),
        source="Start-AnalysisEngine.ps1 and README.md",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 07:50:45 UTC",
        area="Analysis",
        action="Fixed",
        title="Stop project passes when Ollama cannot load its model",
        details=(
            "Diagnosed 64 function failures caused by repeated five-minute llama-server startup timeouts before any completed generation.",
            "Distinguished backend startup, model-load/memory, connection and idle-timeout errors from invalid function responses in HTTP and streamed Ollama errors.",
            "Stopped the pass on backend failure, preserving completed reviews and resetting active single, batch and chunk targets to pending without retrying every function.",
            "Retained existing handling of malformed function responses and documented RAM/VRAM loading diagnostics; saved verified originals under backups/ollama-startup-failure-20260909-085045.",
        ),
        source="analysis_engine.py, project_function_analysis.py, README.md and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 01:25:34 UTC",
        area="Startup",
        action="Added",
        title="Desktop PowerShell launcher using the Python 3.13 environment",
        details=(
            "Added a standalone desktop-copyable launcher that invokes .venv313/Scripts/python.exe directly and anchors the working directory to the existing project.",
            "Preserved the current PyCharm Ollama model, reasoning and ordinary chat-token settings as editable defaults, with inherited environment settings taking precedence.",
            "Added a check-only mode, visible startup failures and restoration of the calling session's environment and working directory.",
            "Documented desktop startup and the process-only PowerShell execution-policy option; saved verified originals under backups/desktop-launcher-20260909-022534.",
        ),
        source="Start-AnalysisEngine.ps1 and README.md",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-09 00:04:23 UTC",
        area="Analysis",
        action="Added",
        title="Structural output budgets and observed usage calibration",
        details=(
            "Calculated 1–100 function/dependency scores and approximate JSON size from language-aware source structure and distinct indexed dependencies, with explicit uncertainty for incomplete parsing.",
            "Selected 4096, 8192 or 16384 output tokens from a documented provisional formula, raising allowances for matching-source truncation history and sufficiently sampled comparable generation usage.",
            "Split batches before generation when combined estimates exceed the shared output allowance; preserved whole functions up to 20000 characters and the independent context-window budget.",
            "Added migration 33 for latest estimates and bounded user/model/language-isolated observations; recorded total generation tokens and separate reasoning/answer character counts without storing reasoning text.",
            "Displayed the active function budget above the tree and included estimates in file tooltips and function-report API data; retained a fixed-output configuration switch.",
            "Backed up originals under backups/adaptive-output-20260909-010423; migration is applied on the user's next restart, leaving the stopped engine's reset analysis unchanged.",
        ),
        source="function_budget.py, analysis_engine.py, project_function_analysis.py, migrations.py, main.py, project_tree_assets.py, app_config.py and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 21:50:38 UTC",
        area="Analysis",
        action="Fixed",
        title="Avoid Ollama grammar expansion failures",
        details=(
            "Diagnosed repeated grammar-parser complexity errors in the local Ollama server log alongside long token generations.",
            "Removed maxLength and maxItems upper bounds only from the function-analysis decoder schema, avoiding nested repetition expansion while preserving required fields, types, enums and closed objects.",
            "Kept the full schema in the prompt and retained local validation limits and output-token ceilings; no fallback to unconstrained prose was introduced.",
            "Verified backups under backups/ollama-grammar-20260908-225037; the active analysis and live database were left running untouched.",
        ),
        source="analysis_engine.py and tests/test_structured_reviews.py",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 20:12:19 UTC",
        area="Analysis",
        action="Changed",
        title="Larger output allowances for complete JSON reviews",
        details=(
            "Doubled default individual function/chunk output from 4096 to 8192 tokens and shared batch output from 8192 to 16384 tokens.",
            "Raised the bounded individual truncation retry allowance to 16384 tokens with the default configuration, retaining full prompt/schema budgeting within the adaptive context ceiling.",
            "Updated the environment reference and README; schema/source validation still rejects invalid reviews independently of response length.",
            "Saved verified originals under backups/output-tokens-20260908-211218; apply these defaults by restarting the application, with existing environment overrides taking precedence.",
        ),
        source="app_config.py, analysis_engine.py, .env.example and README.md",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 20:02:15 UTC",
        area="Analysis",
        action="Changed",
        title="Stricter Ollama JSON reviews and bounded validation retries",
        details=(
            "Required all fields in the function-analysis wire schema, including arrays and nested return/parameter fields, while preserving compatibility with stored results.",
            "Grounded function requests with the compact output schema, explicit JSON field rules, temperature zero and no repetition penalty; removed the artificial opening-brace assistant prefill.",
            "Kept transport retries in JSON mode and removed conflicting prose/Markdown instructions from structured requests.",
            "Retried an invalid individual function or chunk review once with its original source and validation feedback; successful reviews still need only one generation and batch splitting remains bounded.",
            "Detected output-token exhaustion explicitly and allowed the single retry a larger output allowance, still subject to the adaptive context ceiling.",
            "Preserved outer fields when recovering a nested model summary, rejected conflicting duplicate values, and stopped batch validation from filling a missing parameter review before checking it.",
            "Kept incomplete reviews incomplete after unsuccessful retries, retained source-proof checks, and labelled a finished pass with outstanding reviews accurately in the live log.",
            "Saved verified pre-edit originals under backups/structured-reviews-20260908-210215; the running analysis and live database were not modified by this update.",
        ),
        source="analysis_engine.py, main.py and structured-output/job regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 16:29:23 UTC",
        area="WebUI",
        action="Added",
        title="Name new projects before uploading",
        details=(
            "Prompt for an editable project name when files, a folder or a ZIP create a new project, with the upload-derived name prefilled.",
            "Cancel stops the upload and permits selecting the same files again; blank or overlong names are prompted again.",
            "Persist the chosen name in the existing project record and tree while preserving upload group labels and names of projects receiving additional uploads.",
            "Retain upload-derived names for API clients that omit the optional project_name field; saved verified originals under backups/project-name-20260908-172923.",
        ),
        source="main.py, web_assets.py and upload/browser regression coverage",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 16:19:10 UTC",
        area="Analysis",
        action="Changed",
        title="Whole 20,000-character functions and adaptive dependency context",
        details=(
            "Raised function chunks, general input chunks and the direct-message threshold to 20,000 characters; indexed functions at or below the threshold remain whole.",
            "Raised per-function dependency context to 20,000 characters, reserving space for completed callee contracts and a bounded source-derived map of branches, exception handlers, returns and statements.",
            "Compacted long callee contracts instead of dropping their structured fields, and excluded legacy, failed and partial reviews from completed-callee hypotheses.",
            "Strengthened prompts to preserve conditional side effects, handler scope and return paths, while keeping inferred contracts provisional and incomplete control-flow maps explicitly partial.",
            "Added configurable per-request analysis context tiers from 8,192 to 65,536 tokens with growth retained within each project job, reducing repeated context resizing without reordering dependencies.",
            "Budgeted the assembled prompt, structured schema, output allowance and safety margin using a conservative UTF-8 estimate; oversized estimates fail explicitly instead of silently truncating source.",
            "Logged chosen context size, actual prompt and generated-token counts, and loading/evaluation timings for comparison with fixed-context runs.",
            "Advanced the cache version and preserved reuse across unchanged function relocation and safe comment-only edits; saved verified pre-edit file backups with a manifest under backups/analysis-context-20260908-161031.",
        ),
        source="app_config.py, analysis_engine.py, dependency_context.py, project_function_analysis.py, ollama_budget.py, main.py, configuration reference and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-08 13:44:57 UTC",
        area="WebUI",
        action="Changed",
        title="Live file-tree analysis colours and completion ticks",
        details=(
            "Displayed the selected main filename in green and files with actively processing functions in red, including batches spanning multiple files.",
            "Added a completion tick once all indexed functions in a file have been processed; ordinary files return to white and the main file returns to green.",
            "Exposed per-file function counts and processing states from stored symbol statuses, with tooltips showing failed and skipped counts so processed does not imply defect-free.",
            "Refreshed tree states alongside live job polling while preserving expanded folders and scroll position, and restored accurate states after pause, cancellation, reset and reload.",
            "Added API and Edge browser regression coverage for file transitions, main-file colour priority, multi-file batches and files without indexed functions.",
        ),
        source="main.py, project_tree_assets.py, tests/test_project_tree.py and tests/project_tree_browser.cjs",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-07 22:42:01 UTC",
        area="Analysis",
        action="Changed",
        title="Meaningful model responses and explicit review quality",
        details=(
            "Rejected empty, generic and schema-only model responses instead of manufacturing successful default summaries and confidence scores.",
            "Recovered observed Ollama responses that nest behavior and contracts inside the summary field, preserving their actual descriptions instead of replacing them with defaults.",
            "Validated model findings independently, retained usable contracts and static findings, and recorded incomplete proof fields without inventing guard reasoning.",
            "Recorded findings excluded by source verification and invalidated confident model summaries when their syntax assessment contradicts the local parser.",
            "Kept non-Python fallback reviews from manufacturing Python syntax errors; syntax diagnostics remain the responsibility of the indexed language parser.",
            "Persisted review method, completeness, validation diagnostics and response hashes; marked failed or partial model reviews incomplete and excluded them from reusable caches.",
            "Added report quality coverage and an explicit 95% model-confidence target, distinguishing self-reported confidence from measured accuracy and marking older results unverified.",
            "Strengthened shared model prompts with explicit guard field types and meaningful summary requirements, and expanded local analysis to small typed branches with pure operations.",
            "Extended benchmark reports to expose incomplete clean regions and review coverage alongside seeded-fault precision and recall.",
            "Advanced the analysis cache version so earlier default or malformed results are not reused.",
        ),
        source="analysis_engine.py, analysis_quality.py, project_function_analysis.py, analysis_benchmark.py, main.py, WebUI and regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-07 19:10:33 UTC",
        area="Application",
        action="Changed",
        title="Split project workspace with more room during analysis",
        details=(
            "Placed the project tree on the left and conversation or live analysis log on the right, using the available window width and height.",
            "Hid the upload button, message input, send button and mode controls during queued, running or paused analysis; restored them after completion, failure or cancellation.",
            "Kept the live log, project status, timer, pause/resume and stop controls visible, with independently scrolling tree and log panels.",
            "Added a stacked layout for smaller screens and preserved the correct analysis layout across chat switching and page reloads.",
            "Exposed request mode in job responses so pasted-source analysis receives the same expanded layout as project analysis.",
        ),
        source="project_tree_assets.py, web_assets.py, main.py and browser/job regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-07 18:45:05 UTC",
        area="Application",
        action="Changed",
        title="Markdown changelog generation and download",
        details=(
            "Replaced the generated changelog.txt with changelog.md, using headings, metadata and bullet lists for easier reading.",
            "Updated the generator's default output, authenticated download, WebUI link and documentation to use Markdown.",
            "Preserved all recorded entries, newest-first ordering and optional database installation status.",
        ),
        source="changelog.py, main.py, web_assets.py, README.md and changelog regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-09-07 18:43:22 UTC",
        area="Application",
        action="Added",
        title="Cumulative project uploads and editable project tree",
        details=(
            "Added individual and multiple-file selection alongside folder and ZIP uploads.",
            "Added an Upload to selector so further uploads can join an existing project or create a separate project.",
            "Added an expandable tree headed by the project name, with upload origins and folder and ZIP contents.",
            "Added right-click and action menus to delete individual files, folders with their descendants, or entire uploads from the database and tree.",
            "Added a persistent main-file designation and badge, supplied as entry-point context to analysis without renaming the file.",
            "Rejected duplicate and conflicting paths without overwriting, enforced cumulative project limits, and prevented edits during active analysis.",
            "Rebuilt dependency indexes and reset outdated reports after edits; retained empty projects for subsequent uploads.",
            "Preserved existing uploads through migration 32 and included upload origins and main-file selection in account exports.",
            "Verified the implementation with 333 passing Python tests and isolated Edge checks for tree rendering, deletion, main-file selection, cumulative uploads, and mobile width.",
        ),
        source=(
            "main.py, api_models.py, project_uploads.py, project_workspace.py, "
            "project_tree_assets.py, web_assets.py, project_function_analysis.py, "
            "migration 32, and regression tests"
        ),
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-25 00:04:29 UTC",
        area="Application",
        action="Changed",
        title="Made All Results the primary report filter",
        details=(
            "Moved All Results above Actionable Findings in the project-report type dropdown.",
            "Changed newly opened reports to select All Results by default.",
        ),
        source="web_assets.py and report UI regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 23:59:19 UTC",
        area="Application",
        action="Changed",
        title="Deduplicated consecutive live analysis messages",
        details=(
            "Stopped consecutive project-analysis stages with the same path/function message from adding duplicate rows to the live activity list.",
            "Continued updating the persisted stage, counters, file, and function fields even when the duplicate display event is suppressed.",
        ),
        source="main.py and project-analysis job regression tests",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 23:30:49 UTC",
        area="Analysis",
        action="Changed",
        title="Lower-noise call typing, model findings, and batch recovery",
        details=(
            "Made Python expression typing conservative when any conditional, Boolean, arithmetic, or container element branch has an unknown type, preventing a known branch from being reported as the type of the whole expression.",
            "Recognized concrete Python ast node classes as compatible with ast.AST parameters, removing false subtype mismatches without weakening unrelated type checks.",
            "Rejected model exception findings when the exact operation is protected by a matching non-reraising handler, and rejected IndexError claims when short-circuit control flow proves the requested element exists.",
            "Accepted wrapped, flattened, keyed, and exact-length ordered function batch responses; structural response failures now fall back once instead of recursively retrying the same incompatible schema.",
            "Classified closure parameters, locally bound callables, nested receiver methods, and unresolved externally inherited methods outside local contract coverage while retaining direct unresolved project methods as actionable unknowns.",
            "Made call-compatibility recalculation idempotent by replacing prior finding rows together with their compatibility rows.",
            "Added regression coverage for every corrected report pattern and for nearby unsafe cases that must remain visible.",
        ),
        source=(
            "analysis_engine.py, project_function_analysis.py, "
            "project_call_compatibility.py, language_adapters/python_adapter.py, "
            "and regression tests"
        ),
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 22:18:46 UTC",
        area="Analysis",
        action="Changed",
        title="Source-proven findings, broader local call coverage, and adaptive batching",
        details=(
            "Rejected model claims contradicted by explicit validation raises, safe Python slices, unlink(missing_ok=True), isinstance-refined loop values, and annotated input contracts while exposing legitimate raised exceptions in each function contract.",
            "Added conservative Python type-flow inference for annotated parameters, prior same-path assignments, comprehensions, formatted strings, comparisons, local return annotations, and class instances while refusing later or conditional-only bindings.",
            "Resolved lexical closures, inherited self/cls methods, and explicit project constructors without confusing bare method names with Python class scope; classified callable parameters and arbitrary dynamic receiver calls outside local contract coverage.",
            "Expanded deterministic-only analysis to ordinary typed functions with calls, async work, exceptions, globals, and side effects, reserving LLM review for unknown contracts, very complex functions, and dynamic code execution.",
            "Changed failed model batches to split adaptively down to two-function groups, retained successful subgroups, and recorded fallback counts and the latest fallback reason.",
            "Marked a finished call-check pass as completed even when local targets remain unresolved, and added an explicit local-call coverage percentage so unresolved work remains visible without implying the run stopped early.",
            "Replaced the function-analysis cache key with cache-v6-proof-and-flow so lower-signal cached analyses are not reused.",
        ),
        source=(
            "project_function_analysis.py, project_structure.py, "
            "project_call_compatibility.py, language_adapters/python_adapter.py, "
            "report UI, and migration 31"
        ),
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 20:00:28 UTC",
        area="Analysis",
        action="Changed",
        title="Higher-signal call checks and measured analysis work",
        details=(
            "Fixed generic container parsing and added tuple, list, set, and dictionary literal element inference so valid container arguments are no longer reported as type mismatches.",
            "Rejected model syntax claims without matching parser diagnostics and variable-flow claims contradicted by deterministic scope analysis.",
            "Reduced broad-exception noise for structural cleanup/re-raise, reported worker boundaries, and per-item isolation while retaining warnings for handlers that silently swallow failures.",
            "Separated built-in, external, and dynamic calls outside local contract coverage from genuinely unresolved project calls, so they no longer make a report partial.",
            "Persisted and displayed actual LLM request, batch request, deterministic-only, cache reuse, unresolved-call, and outside-scope counts.",
            "Replaced the function-analysis cache key with cache-v5-signal-validation so older lower-signal cached reports are not reused.",
        ),
        source=(
            "project_call_compatibility.py, project_function_analysis.py, "
            "language_adapters/python_adapter.py, report UI, and migration 30"
        ),
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 18:14:42 UTC",
        area="Application",
        action="Added",
        title="Migration-driven application changelog",
        details=(
            "Added a generated changelog that combines migration history with curated non-database changes.",
            "Added an authenticated changelog page, a text download, and the repository changelog.txt file.",
            "Added validation so every migration must have a timestamp, action, and human-readable description.",
        ),
        source="Curated application history",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 18:06:18 UTC",
        area="Application",
        action="Changed",
        title="Quieter live analysis progress and active-time timing",
        details=(
            "Shortened function activity entries to path/function while retaining detailed counters in the static progress box.",
            "Changed elapsed timers and new activity timestamps to stop while paused and continue when resumed.",
            "Persisted pause duration so active elapsed time remains correct after a browser refresh.",
        ),
        source="main.py, web_assets.py, and migration 29",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 17:29:24 UTC",
        area="Analysis",
        action="Changed",
        title="Higher-signal analysis with fewer model requests",
        details=(
            "Required proof-bearing model findings and rejected claims contradicted by the indexed source.",
            "Added deterministic Python scope, control-flow, and trusted-contract checks that work without an LLM call.",
            "Separated actionable defects from advisories and reduced broad-exception and assumption-based noise.",
            "Classified assumption-based hazards as Unsafe while retaining specific failure types for definite errors.",
            "Batched eligible function analysis within a configurable character budget and reused deterministic or cached results.",
        ),
        source="analysis_engine.py, project_function_analysis.py, language adapters, and configuration",
    ),
    ChangelogEntry(
        recorded_at_utc="2026-08-24 17:22:00 UTC",
        area="Analysis",
        action="Added",
        title="Repeatable analysis accuracy benchmark",
        details=(
            "Added a benchmark runner and expected-results fixture for measuring true findings, missed defects, and noise.",
            "Added known seeded defects covering NameError and unsafe IndexError behavior for report comparison.",
        ),
        source="analysis_benchmark.py and analysis_benchmark_fixed.json",
    ),
)


def _parsed_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _database_timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    if text.endswith(" UTC"):
        return text
    return f"{text.replace('T', ' ')} UTC"


def validate_changelog_metadata() -> None:
    versions = {version for version, _name, _function in MIGRATIONS}
    timestamp_versions = set(MIGRATION_RECORDED_AT_UTC)
    action_versions = set(MIGRATION_CHANGE_ACTIONS)
    if timestamp_versions != versions:
        missing = sorted(versions - timestamp_versions)
        extra = sorted(timestamp_versions - versions)
        raise ValueError(
            f"Migration changelog timestamps do not match MIGRATIONS; missing={missing}, extra={extra}"
        )
    if action_versions != versions:
        missing = sorted(versions - action_versions)
        extra = sorted(action_versions - versions)
        raise ValueError(
            f"Migration changelog actions do not match MIGRATIONS; missing={missing}, extra={extra}"
        )
    for version, name, function in MIGRATIONS:
        _parsed_timestamp(MIGRATION_RECORDED_AT_UTC[version])
        if not inspect.getdoc(function):
            raise ValueError(f"Migration {version} ({name}) needs a changelog docstring")


def _applied_migration_times(
    db: sqlite3.Connection | None,
) -> dict[int, str | None] | None:
    if db is None:
        return None
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table_exists is None:
        return {}
    return {
        int(row[0]): _database_timestamp(row[1])
        for row in db.execute("SELECT version, applied_at FROM schema_migrations")
    }


def build_changelog_entries(
    db: sqlite3.Connection | None = None,
) -> list[ChangelogEntry]:
    """Combine curated notes with metadata derived from every migration."""
    validate_changelog_metadata()
    applied = _applied_migration_times(db)
    entries = list(MANUAL_CHANGELOG_ENTRIES)
    for version, name, function in MIGRATIONS:
        installed = None if applied is None else version in applied
        entries.append(
            ChangelogEntry(
                recorded_at_utc=MIGRATION_RECORDED_AT_UTC[version],
                area="Database",
                action=MIGRATION_CHANGE_ACTIONS[version],
                title=f"Migration {version}: {name.replace('_', ' ').title()}",
                details=(inspect.getdoc(function) or "",),
                source=f"migrations.py :: {function.__name__}",
                migration_version=version,
                migration_name=name,
                installed=installed,
                applied_at_utc=applied.get(version) if applied is not None else None,
            )
        )
    return sorted(
        entries,
        key=lambda entry: (
            _parsed_timestamp(entry.recorded_at_utc),
            entry.migration_version or 100_000,
        ),
        reverse=True,
    )


def render_changelog_markdown(
    entries: list[ChangelogEntry],
    *,
    include_installation: bool = False,
) -> str:
    lines = [
        "# Apokalypse Coder Bot — Changelog",
        "",
        "Newest entries appear first. All timestamps are UTC.",
        "",
        "Migration timestamps are the earliest reliable chronology in this checkout,",
        "not public release dates. Schema history is complete through the latest migration.",
        "No Git history is present, so older non-database edits cannot be reconstructed",
        "reliably; curated application entries begin on 2026-08-24.",
        "",
        "Changes, replacements, and reversions are labelled explicitly.",
        "",
    ]
    for entry in entries:
        lines.extend(
            (
                f"## {entry.title}",
                "",
                f"**{entry.recorded_at_utc}** · {entry.area} / **{entry.action}**",
                "",
            )
        )
        lines.extend(f"- {detail}" for detail in entry.details)
        lines.extend(("", f"**Source:** {entry.source}"))
        if include_installation and entry.migration_version is not None:
            lines.append("")
            if entry.installed:
                lines.append(
                    "**Installed on this database:** "
                    + (entry.applied_at_utc or "recorded; timestamp unavailable")
                )
            else:
                lines.append("**Installed on this database:** pending")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_changelog_html(entries: list[ChangelogEntry]) -> str:
    cards: list[str] = []
    for entry in entries:
        details = "".join(f"<li>{html.escape(detail)}</li>" for detail in entry.details)
        installation = ""
        if entry.migration_version is not None:
            if entry.installed:
                status = "Installed on this database: " + (
                    entry.applied_at_utc or "timestamp unavailable"
                )
                status_class = "installed"
            else:
                status = "Pending on this database"
                status_class = "pending"
            installation = (
                f'<span class="migration-status {status_class}">{html.escape(status)}</span>'
            )
        action_class = entry.action.lower().replace(" ", "-")
        cards.append(
            '<article class="change-entry">'
            '<div class="change-meta">'
            f'<time datetime="{html.escape(entry.recorded_at_utc)}">'
            f'{html.escape(entry.recorded_at_utc)}</time>'
            f'<span class="change-badge {html.escape(action_class)}">'
            f'{html.escape(entry.area)} / {html.escape(entry.action)}</span>'
            f"{installation}</div>"
            f"<h2>{html.escape(entry.title)}</h2>"
            f"<ul>{details}</ul>"
            f'<p class="change-source">Source: {html.escape(entry.source)}</p>'
            "</article>"
        )
    return "".join(cards)


def write_changelog_file(path: Path, *, db: sqlite3.Connection | None = None) -> None:
    entries = build_changelog_entries(db)
    path.write_text(
        render_changelog_markdown(entries, include_installation=db is not None),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the project Markdown changelog")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("changelog.md"),
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Optionally include installation status from a SQLite database",
    )
    arguments = parser.parse_args()
    if arguments.database is None:
        write_changelog_file(arguments.output)
    else:
        with sqlite3.connect(arguments.database) as db:
            write_changelog_file(arguments.output, db=db)
    print(f"Wrote {arguments.output}")


if __name__ == "__main__":
    main()
