# Apokalypse Coder Bot — Changelog

Newest entries appear first. All timestamps are UTC.

Migration timestamps are the earliest reliable chronology in this checkout,
not public release dates. Schema history is complete through the latest migration.
No Git history is present, so older non-database edits cannot be reconstructed
reliably; curated application entries begin on 2026-08-24.

Changes, replacements, and reversions are labelled explicitly.

## Reworked installation and usage guidance

**2026-09-11 20:05:15 UTC** · Documentation / **Changed**

- Replace the Windows-only setup summary with common preparation and step-by-step Windows Command Prompt, Windows PowerShell, Linux and macOS instructions.
- Document required SMTP and owner setup, first sign-in, Lemonade configuration and installation health checks for new users.
- Reduce project upload documentation to supported file, folder and ZIP inputs and supported analysis languages.
- Remove the analysis benchmark documentation and condense account ownership to its principal roles and controls.

**Source:** README.md and changelog.py

## Removed machine-specific and personal defaults

**2026-09-11 19:41:12 UTC** · Privacy / **Changed**

- Remove the previously configured NTFY topic and personal owner identity from documentation and application defaults.
- Default error notifications to disabled until NTFY_TOPIC is explicitly configured.
- Use generic owner defaults and remove the personal username from the password blocklist.
- Resolve the standalone launcher's project directory from the script location instead of a machine-specific user path.
- Replace example email addresses in the README command snippet with neutral placeholders.
- Label the README's NTFY_TOPIC setting with the neutral text NTFY notification topic name.

**Source:** README.md, app_config.py, authentication.py, Start-AnalysisEngine.ps1 and changelog.py

## Removed the standalone Windows launcher instructions

**2026-09-11 19:36:05 UTC** · Documentation / **Changed**

- Remove the README section describing how to start the application outside PyCharm.

**Source:** README.md and changelog.py

## Prepared safe defaults for source control

**2026-09-11 19:23:43 UTC** · Repository / **Changed**

- Ignore alternate local virtual-environment directories such as .venv313 in every clone.
- Replace the local notification topic example with an explicit placeholder before publishing the repository.
- Define consistent cross-platform line endings for source, documentation, data and PowerShell files.

**Source:** .gitignore, .gitattributes, .env.example and changelog.py

## Accepted source-verifiable behavior claim shape deviations

**2026-09-11 19:06:10 UTC** · Function analysis / **Fixed**

- Diagnose require_admin as failing after three attempts because the model used the unsupported behavior kind raise for an exact exception guard.
- Infer the supported behavior kind from the claim's evidence before strict validation, while retaining the existing source-line, excerpt and kind verification.
- Retain the first claim when a model returns more than the one behavior claim requested by the compact schema, fixing the related enforce_project_storage_limit failure.

**Source:** semantic_review.py, README.md, changelog.py and regression tests

## Displayed actual function dependency counts

**2026-09-11 18:13:51 UTC** · Function analysis / **Changed**

- Replace the WebUI's weighted dependency score with the actual number of distinct resolved and unresolved callees for the function.
- Retain the existing resolved, unresolved, cross-file and recursion weights internally for output and reasoning budget selection.
- Derive the visible count from stored dependency details for existing budgets so completed analyses do not need to be rerun.

**Source:** function_budget.py, project_tree_assets.py, README.md, changelog.py and regression tests

## Function source details and caller inspection

**2026-09-11 17:27:37 UTC** · Project explorer / **Added**

- Show each indexed function declaration and its owned return statements in the tree hover text without repeating exact source already present in the description.
- Label multiple return paths as flow dependent with their source lines, while excluding returns owned by nested Python functions.
- Open a keyboard-accessible caller dialog when a function is clicked, listing statically resolved project callers, enclosing functions or module scope, locations, usage kinds and exact call expressions.

**Source:** project_tree_metadata.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests

## Completed descriptions after repeated invalid model line anchors

**2026-09-11 16:48:16 UTC** · Function analysis / **Fixed**

- Diagnose _drop_issues_outside_lines as a complete typed contract whose Hybrid behavior claim was rejected because its line range fell outside the uploaded function on both bounded attempts.
- After both model behavior anchors fail, select and verify one exact target-source statement for the engine-owned description while retaining the rejected model claims and explicit fallback provenance.
- Prefer a complete return statement over partial multiline container setup in the compact response template, and keep unresolved parameter or return contracts partial.

**Source:** semantic_review.py, README.md, changelog.py and regression tests

## Descriptions for deterministically completed functions

**2026-09-10 22:35:21 UTC** · Project tree / **Fixed**

- Expose completed deterministic summaries in function-tree tooltips instead of showing Unknown solely because no model request was needed.
- Generate a source-derived validator description containing its guarded condition, built-in exception and return type; identify it as Static analysis in the tooltip.
- Keep Get description available for deterministic results so a user can still request a forced LLM review, while completed model descriptions retain their existing presentation.

**Source:** project_function_analysis.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests

## Deterministic completion for explicit built-in raises

**2026-09-10 22:28:04 UTC** · Function analysis / **Fixed**

- Identify FunctionIssue.valid_line_order as a fully source-proven validator whose only previously unsupported call was the ValueError constructor used directly by raise.
- Allow built-in exception constructors only when they are the direct expression of a raise statement, so small complete validators bypass unreliable model coordinate output without treating arbitrary calls or user-defined exceptions as pure.
- Verify the actual indexed Fixed/analysis_engine.py symbol now has a complete deterministic receiver, FunctionIssue return, ValueError raise and zero unresolved issues.

**Source:** project_function_analysis.py, README.md, changelog.py and regression tests

## Recover uniquely matching multiline behavior evidence

**2026-09-10 22:20:31 UTC** · Function analysis / **Fixed**

- Diagnose repeated FunctionIssue.valid_line_order and _drop_issues_outside_lines failures as two complete, non-truncated Hybrid responses whose behavior coordinates fell outside both accepted line systems.
- Relocate a behavior claim when its evidence matches exactly one target-source token sequence after whitespace-only normalization, then store the original exact multiline source excerpt and corrected absolute lines.
- Keep ambiguous or changed evidence rejected, and make every prompt field consistently request 1-based source-relative coordinates.

**Source:** semantic_review.py, changelog.py and regression tests

## Project selection locked to active analysis

**2026-09-10 22:08:41 UTC** · Project workspace / **Changed**

- Keep independently uploaded projects selectable while no project analysis is running, so each project tree and report can be revisited from the same workspace.
- Select and pin the project that starts an analysis, disable the project dropdown until that job completes, fails or is cancelled, and reject programmatic selection changes while it is locked.
- Retain the server-side project ID on every analysis job, preventing a UI selection from changing the files or results used by work already in progress.

**Source:** project_tree_assets.py, web_assets.py, README.md, changelog.py and browser regression tests

## Top-level project rename and delete actions

**2026-09-10 22:08:40 UTC** · Project tree / **Added**

- Add a three-dot action menu beside the project name at the root of the file tree with Rename and Delete actions.
- Persist renamed project names immediately across the selector, project status and tree, and remove a confirmed project with its uploaded files, parsed structure, jobs and stored report.
- Refuse project deletion while its analysis is queued or running and retain user-ownership checks for both operations.

**Source:** api_models.py, main.py, project_tree_assets.py, README.md, changelog.py and regression tests

## Recovered more valid function reviews and parameter contracts

**2026-09-10 22:08:39 UTC** · Function analysis / **Fixed**

- Increase exact-source evidence fields from 500 to 1000 characters and bound overlong evidence locally, allowing longer valid claims to survive strict result validation.
- Repair invalid JSON Unicode and backslash-newline escapes and flatten a recognized legacy return_fields wrapper while continuing to reject unknown or contradictory fields.
- Infer unresolved Python parameter contracts conservatively from resolved dependency signatures, matching method override signatures, callable use and accessed attributes, and include matching override contracts in method context.
- Treat a bare yield as a source-proven no-value path, retain review for value-yielding generators and version the source-fact contract so incompatible earlier cache entries are not reused.

**Source:** analysis_engine.py, semantic_review.py, project_function_analysis.py, changelog.py and regression tests

## Distinct active and failed analysis colours

**2026-09-10 22:08:38 UTC** · Project tree / **Changed**

- Show the currently analysed file and function in cyan while reserving red for functions whose analysis failed.
- Show a file in red when all of its processed functions failed, yellow when completed results are mixed between passed and failed, and preserve the normal completed and main-file colours otherwise.

**Source:** project_tree_assets.py, README.md, changelog.py and browser regression tests

## Full-width analysis-only workspace

**2026-09-10 19:15:43 UTC** · WebUI / **Changed**

- Rename the interface to Apokalypse Code Analysis System and make pasted source submissions use analysis mode without displaying a mode switch.
- Remove the visible new-chat and chat-history sidebar so the project tree and live analysis output use the full viewport width.
- Group project controls against the right edge of the status bar, leaving the remaining width for the project, language, file count, progress and current function text.

**Source:** web_assets.py, README.md, changelog.py and WebUI regression tests

## Duplicate folder-upload root branches

**2026-09-10 19:04:20 UTC** · Project tree / **Fixed**

- Collapse the first stored path segment into its matching folder-upload branch so a selected directory such as Fixed appears only once in the tree.
- Retain the complete stored path behind visible nested-folder and file actions, including deletion and main-file selection.

**Source:** project_tree_assets.py, README.md, changelog.py and browser regression tests

## Longer function hover descriptions

**2026-09-10 18:52:49 UTC** · Project tree / **Changed**

- Increase the function-tree hover-description allowance from 249 to 400 characters so complete engine-generated return-contract sentences remain visible.
- Keep whitespace compact and retain an ellipsis for summaries longer than the new bounded tooltip allowance.

**Source:** main.py, README.md, changelog.py and regression tests

## Lemonade Hybrid chat compatibility

**2026-09-10 18:13:32 UTC** · Model transport / **Fixed**

- Route Lemonade `*-Hybrid` models through its native streaming OpenAI chat endpoint because its Ollama-compatible chat adapter produced malformed, schema-ignoring output for project function reviews.
- Continue using the Ollama-compatible endpoint for ordinary Ollama models, and retain cancellation, request deadlines, usage tracking and repetition detection on both transports.
- Recover bounded local-model JSON mistakes including wrapped objects, Python-style mappings, literal control characters and surplus text after a complete object while preserving strict schema and source-evidence validation.
- Clarify that semantic evidence must be copied verbatim from source code rather than supplied as prose.
- Replace the verbose JSON Schema for Lemonade Hybrid semantic reviews with a compact required-output template anchored to a real function-body statement, repeat it after validation errors, reject declaration-only behavior claims and spend the existing repair attempt when no source-grounded behavior survives.

**Source:** analysis_engine.py, semantic_review.py, function_budget.py, README.md, changelog.py and regression tests

## Function branches and on-demand descriptions

**2026-09-10 12:07:50 UTC** · Project tree / **Added**

- List indexed functions as collapsible children of each source file and mirror active, paused, completed, failed and skipped analysis states on their tree rows.
- Show a sub-250-character description from completed model-reviewed summaries on hover, while functions without a completed LLM review display Unknown.
- Offer Get description on unknown function rows and queue one forced semantic model review for exactly that symbol through the existing cancellable job worker.
- Reuse the verified analysis summary instead of expanding the model JSON contract with a duplicate description field.

**Source:** project_tree_assets.py, main.py, project_function_analysis.py, migrations.py, README.md, changelog.py and regression tests

## Migration 34: Function Tree Descriptions

**2026-09-10 12:00:00 UTC** · Database / **Added**

- Target one indexed function when a tree description is requested.

**Source:** migrations.py :: migration_034_function_tree_descriptions

## Visible configured LLM model

**2026-09-10 11:40:37 UTC** · WebUI / **Added**

- Show the configured Ollama model in a compact badge beside the WebUI title, so the model selected for chat and project analysis is visible before work starts.
- Escape the configured model name before rendering it and keep the badge readable on desktop and narrow-screen layouts.

**Source:** web_assets.py, main.py, README.md, changelog.py and regression tests

## Bounded function runtime and recovered evidence anchors

**2026-09-10 07:17:08 UTC** · Analysis / **Fixed**

- Measured the live 494-function Qwen run at 90 completed, 137 failed and one processing result after 218 model requests; 223 dependency positions required more than seven and a half hours.
- Include current project counters in queued and processing job responses, so live progress reports stored completed and failed results instead of displaying zero until the job ends.
- Recover a model claim whose exact source evidence occurs once inside the target even when Qwen supplies an invalid line, while keeping repeated or absent excerpts rejected and recording each correction.
- Request one concise behavior claim per function while retaining the full issue, error and side-effect collections, reducing non-finding output that dominated slow local generation.
- Bound each function, chunk or batch generation to five minutes by default without shortening ordinary chat requests; timed-out reviews remain incomplete and the project continues.

**Source:** app_config.py, analysis_engine.py, semantic_review.py, main.py, Start-AnalysisEngine.ps1, README.md, changelog.py and regression tests

## Dependency-order progress labels and Qwen relative evidence lines

**2026-09-09 23:12:28 UTC** · Analysis / **Fixed**

- Diagnosed the apparent function-counter regression as dependency ordering moving from source position 27 to source position 14 in the same 65-function file; the persisted work sequence continued from 22 to 23 and no completed work was erased.
- Show monotonic finished project-result counts separately and label the current function number as its dependency-ordered source position, so a backwards source jump is no longer presented as lost progress.
- Measured the active evidence-format run at 11 completed, 22 failed and one processing function after 24 model requests; stored rejection metadata showed Qwen consistently emitted function-relative lines while the prompt required absolute lines.
- Request 1-based source-relative coordinates, require at least one behavior claim, verify every excerpt inside the supplied target, and convert proven relative coordinates to absolute file lines before storage; already-absolute proven coordinates remain compatible.
- Correct behavior-kind labels locally when exact evidence supports a deterministic category, record coordinate and kind corrections, and keep unsupported evidence rejected.
- Isolate semantic-v4 usage history and invalidate older pending cache/fact contracts; inspect the active database read-only and save originals under backups/progress-lines-20260909-230838.

**Source:** semantic_review.py, function_budget.py, project_function_analysis.py, web_assets.py, README.md, changelog.py and regression tests

## Evidence-anchored semantic reviews and engine-owned confidence

**2026-09-09 21:40:18 UTC** · Analysis / **Changed**

- Replace Qwen-authored summaries, uncertainty and confidence with engine-generated values derived from the verified target, exact source excerpts, local contracts and explicit confidence caps.
- Require absolute line ranges and exact source evidence for behavior, parameter, return, escaping-error, side-effect and issue claims; discard unsupported individual claims locally and retain rejection metadata without spending a model retry.
- Reserve the one repair generation for malformed or truncated structured output and unavailable tool requests, while preserving partial status for unresolved required behavior or contracts.
- Use separate 1024/2048/4096 semantic output tiers, advance a tier after matching truncation when possible, isolate semantic-v3 usage history, and invalidate older cache/fact contracts.
- Check a rolling 3000-character response tail every 500 characters and interrupt two consecutive strong repetition detections at roughly 3500 answer characters.
- Preserve the database schema and stopped live analysis data; save originals under backups/semantic-evidence-20260909-222602.

**Source:** semantic_review.py, function_budget.py, analysis_engine.py, project_function_analysis.py, README.md, changelog.py and regression tests

## Flat Qwen return inference and early loop interruption

**2026-09-09 19:27:55 UTC** · Analysis / **Changed**

- Infer conservative unannotated Python return relationships locally for typed parameters, self/cls, literals and containers, unshadowed built-in constructors, known conditional branches and known boolean fallback operands; retain model inference for fallthrough, generators, unknown calls and shadowed constructors.
- Replace the nested model-facing return contract with four flat fields, force null/empty values when return inference is unnecessary, validate cross-field coherence locally and construct the existing nested storage contract inside the engine.
- Interrupt a streaming response after two consecutive strong repetition detections at roughly 7000 answer characters, record the failed usage sample and retain the existing single transport retry rather than consuming the full output allowance.
- Version source facts, function-analysis cache entries and compact-response usage history so older response shapes and truncation samples do not contaminate the new Qwen comparison.
- Preserve the database schema and live analysis data; save verified originals under backups/qwen-flat-return-20260909-201500.

**Source:** project_function_analysis.py, semantic_review.py, analysis_engine.py, function_budget.py, README.md, changelog.py and regression tests

## Qwen-specific semantic response constraints

**2026-09-09 18:15:40 UTC** · Analysis / **Fixed**

- Measured faster Qwen2.5-Coder generation but only three accepted model reviews against nineteen failed functions in the initial compact-contract pass; most failures filled inference fields already owned by the engine.
- Specialized each response schema from source facts, requiring an empty parameter-inference array and null return inference when those values are already known, restricting unresolved parameter names, and encoding coherent value/no-value return alternatives.
- Discarded only irrelevant or duplicate model guesses before nested validation while preserving strict validation for required inferences, source evidence, unknown fields and engine-owned contracts; recorded ignored wire fields in source-fact metadata.
- Retained zero-item array bounds in the Ollama decoding grammar without restoring expensive general repetition bounds, and kept one bounded repair for genuinely invalid semantic output.
- Documented that Qwen2.5-Coder has no Ollama reasoning-level control and that OLLAMA_GPT_OSS_REASONING remains specific to GPT-OSS; saved verified originals under backups/qwen-contract-20260909-191021.

**Source:** semantic_review.py, analysis_engine.py, README.md, changelog.py and regression tests

## Engine-owned facts and compact semantic reviews

**2026-09-09 16:02:23 UTC** · Analysis / **Changed**

- Run existing local completion checks across selected pending functions before model work, then review remaining functions in dependency order and refresh persisted progress after each result.
- Extract Python and supported TypeScript signature facts locally; request only semantic summaries, missing type inferences, escaping exceptions, side effects, proof-bearing issues and explicit uncertainties in a smaller strict JSON response.
- Keep source declarations distinct from runtime behavior, caught raise occurrences separate from escaping exceptions, and unresolved single/chunk inferences partial; preserve existing source-proof validation and legacy review interfaces for other signatures.
- Reject unexpected tool calls and allow one semantic repair attempt; retain cancellation, backend-failure handling, completed reviews and the 20000-character source/context limits.
- Estimate compact JSON output separately, omit a dedicated thinking allowance for Qwen2.5-Coder, isolate usage history by response format and update cache versioning without a database migration.
- Document the exact Qwen model tag, PyCharm settings, model comparison and native-context limits; save verified originals and restoration instructions under backups/engine-facts-20260909-170223.

**Source:** semantic_review.py, analysis_engine.py, project_function_analysis.py, function_budget.py, README.md and regression tests

## Lower-reasoning single-function desktop analysis profile

**2026-09-09 15:11:15 UTC** · Startup / **Changed**

- Diagnosed active GPU generation with high reasoning, repeated output exhaustion before any answer text, and only eight valid responses among 24 recorded batch responses in the current pass.
- Changed the desktop launcher's defaults to low GPT-OSS reasoning and one function per request, avoiding the expensive batch prepass while retaining source context, output tiers and review validation.
- Retained inherited environment overrides and documented replacing the desktop copy, checking effective settings and resuming completed work without resetting the project.
- Saved verified originals under backups/ollama-throughput-20260909-161115; the current engine process and live analysis data were left untouched.

**Source:** Start-AnalysisEngine.ps1 and README.md

## Stop project passes when Ollama cannot load its model

**2026-09-09 07:50:45 UTC** · Analysis / **Fixed**

- Diagnosed 64 function failures caused by repeated five-minute llama-server startup timeouts before any completed generation.
- Distinguished backend startup, model-load/memory, connection and idle-timeout errors from invalid function responses in HTTP and streamed Ollama errors.
- Stopped the pass on backend failure, preserving completed reviews and resetting active single, batch and chunk targets to pending without retrying every function.
- Retained existing handling of malformed function responses and documented RAM/VRAM loading diagnostics; saved verified originals under backups/ollama-startup-failure-20260909-085045.

**Source:** analysis_engine.py, project_function_analysis.py, README.md and regression tests

## Desktop PowerShell launcher using the Python 3.13 environment

**2026-09-09 01:25:34 UTC** · Startup / **Added**

- Added a standalone desktop-copyable launcher that invokes .venv313/Scripts/python.exe directly and anchors the working directory to the existing project.
- Preserved the current PyCharm Ollama model, reasoning and ordinary chat-token settings as editable defaults, with inherited environment settings taking precedence.
- Added a check-only mode, visible startup failures and restoration of the calling session's environment and working directory.
- Documented desktop startup and the process-only PowerShell execution-policy option; saved verified originals under backups/desktop-launcher-20260909-022534.

**Source:** Start-AnalysisEngine.ps1 and README.md

## Structural output budgets and observed usage calibration

**2026-09-09 00:04:23 UTC** · Analysis / **Added**

- Calculated 1–100 function/dependency scores and approximate JSON size from language-aware source structure and distinct indexed dependencies, with explicit uncertainty for incomplete parsing.
- Selected 4096, 8192 or 16384 output tokens from a documented provisional formula, raising allowances for matching-source truncation history and sufficiently sampled comparable generation usage.
- Split batches before generation when combined estimates exceed the shared output allowance; preserved whole functions up to 20000 characters and the independent context-window budget.
- Added migration 33 for latest estimates and bounded user/model/language-isolated observations; recorded total generation tokens and separate reasoning/answer character counts without storing reasoning text.
- Displayed the active function budget above the tree and included estimates in file tooltips and function-report API data; retained a fixed-output configuration switch.
- Backed up originals under backups/adaptive-output-20260909-010423; migration is applied on the user's next restart, leaving the stopped engine's reset analysis unchanged.

**Source:** function_budget.py, analysis_engine.py, project_function_analysis.py, migrations.py, main.py, project_tree_assets.py, app_config.py and regression tests

## Migration 33: Function Output Budgets

**2026-09-09 00:04:23 UTC** · Database / **Added**

- Store explainable output estimates and bounded model-usage observations.

**Source:** migrations.py :: migration_033_function_output_budgets

## Avoid Ollama grammar expansion failures

**2026-09-08 21:50:38 UTC** · Analysis / **Fixed**

- Diagnosed repeated grammar-parser complexity errors in the local Ollama server log alongside long token generations.
- Removed maxLength and maxItems upper bounds only from the function-analysis decoder schema, avoiding nested repetition expansion while preserving required fields, types, enums and closed objects.
- Kept the full schema in the prompt and retained local validation limits and output-token ceilings; no fallback to unconstrained prose was introduced.
- Verified backups under backups/ollama-grammar-20260908-225037; the active analysis and live database were left running untouched.

**Source:** analysis_engine.py and tests/test_structured_reviews.py

## Larger output allowances for complete JSON reviews

**2026-09-08 20:12:19 UTC** · Analysis / **Changed**

- Doubled default individual function/chunk output from 4096 to 8192 tokens and shared batch output from 8192 to 16384 tokens.
- Raised the bounded individual truncation retry allowance to 16384 tokens with the default configuration, retaining full prompt/schema budgeting within the adaptive context ceiling.
- Updated the environment reference and README; schema/source validation still rejects invalid reviews independently of response length.
- Saved verified originals under backups/output-tokens-20260908-211218; apply these defaults by restarting the application, with existing environment overrides taking precedence.

**Source:** app_config.py, analysis_engine.py, .env.example and README.md

## Stricter Ollama JSON reviews and bounded validation retries

**2026-09-08 20:02:15 UTC** · Analysis / **Changed**

- Required all fields in the function-analysis wire schema, including arrays and nested return/parameter fields, while preserving compatibility with stored results.
- Grounded function requests with the compact output schema, explicit JSON field rules, temperature zero and no repetition penalty; removed the artificial opening-brace assistant prefill.
- Kept transport retries in JSON mode and removed conflicting prose/Markdown instructions from structured requests.
- Retried an invalid individual function or chunk review once with its original source and validation feedback; successful reviews still need only one generation and batch splitting remains bounded.
- Detected output-token exhaustion explicitly and allowed the single retry a larger output allowance, still subject to the adaptive context ceiling.
- Preserved outer fields when recovering a nested model summary, rejected conflicting duplicate values, and stopped batch validation from filling a missing parameter review before checking it.
- Kept incomplete reviews incomplete after unsuccessful retries, retained source-proof checks, and labelled a finished pass with outstanding reviews accurately in the live log.
- Saved verified pre-edit originals under backups/structured-reviews-20260908-210215; the running analysis and live database were not modified by this update.

**Source:** analysis_engine.py, main.py and structured-output/job regression tests

## Name new projects before uploading

**2026-09-08 16:29:23 UTC** · WebUI / **Added**

- Prompt for an editable project name when files, a folder or a ZIP create a new project, with the upload-derived name prefilled.
- Cancel stops the upload and permits selecting the same files again; blank or overlong names are prompted again.
- Persist the chosen name in the existing project record and tree while preserving upload group labels and names of projects receiving additional uploads.
- Retain upload-derived names for API clients that omit the optional project_name field; saved verified originals under backups/project-name-20260908-172923.

**Source:** main.py, web_assets.py and upload/browser regression coverage

## Whole 20,000-character functions and adaptive dependency context

**2026-09-08 16:19:10 UTC** · Analysis / **Changed**

- Raised function chunks, general input chunks and the direct-message threshold to 20,000 characters; indexed functions at or below the threshold remain whole.
- Raised per-function dependency context to 20,000 characters, reserving space for completed callee contracts and a bounded source-derived map of branches, exception handlers, returns and statements.
- Compacted long callee contracts instead of dropping their structured fields, and excluded legacy, failed and partial reviews from completed-callee hypotheses.
- Strengthened prompts to preserve conditional side effects, handler scope and return paths, while keeping inferred contracts provisional and incomplete control-flow maps explicitly partial.
- Added configurable per-request analysis context tiers from 8,192 to 65,536 tokens with growth retained within each project job, reducing repeated context resizing without reordering dependencies.
- Budgeted the assembled prompt, structured schema, output allowance and safety margin using a conservative UTF-8 estimate; oversized estimates fail explicitly instead of silently truncating source.
- Logged chosen context size, actual prompt and generated-token counts, and loading/evaluation timings for comparison with fixed-context runs.
- Advanced the cache version and preserved reuse across unchanged function relocation and safe comment-only edits; saved verified pre-edit file backups with a manifest under backups/analysis-context-20260908-161031.

**Source:** app_config.py, analysis_engine.py, dependency_context.py, project_function_analysis.py, ollama_budget.py, main.py, configuration reference and regression tests

## Live file-tree analysis colours and completion ticks

**2026-09-08 13:44:57 UTC** · WebUI / **Changed**

- Displayed the selected main filename in green and files with actively processing functions in red, including batches spanning multiple files.
- Added a completion tick once all indexed functions in a file have been processed; ordinary files return to white and the main file returns to green.
- Exposed per-file function counts and processing states from stored symbol statuses, with tooltips showing failed and skipped counts so processed does not imply defect-free.
- Refreshed tree states alongside live job polling while preserving expanded folders and scroll position, and restored accurate states after pause, cancellation, reset and reload.
- Added API and Edge browser regression coverage for file transitions, main-file colour priority, multi-file batches and files without indexed functions.

**Source:** main.py, project_tree_assets.py, tests/test_project_tree.py and tests/project_tree_browser.cjs

## Meaningful model responses and explicit review quality

**2026-09-07 22:42:01 UTC** · Analysis / **Changed**

- Rejected empty, generic and schema-only model responses instead of manufacturing successful default summaries and confidence scores.
- Recovered observed Ollama responses that nest behavior and contracts inside the summary field, preserving their actual descriptions instead of replacing them with defaults.
- Validated model findings independently, retained usable contracts and static findings, and recorded incomplete proof fields without inventing guard reasoning.
- Recorded findings excluded by source verification and invalidated confident model summaries when their syntax assessment contradicts the local parser.
- Kept non-Python fallback reviews from manufacturing Python syntax errors; syntax diagnostics remain the responsibility of the indexed language parser.
- Persisted review method, completeness, validation diagnostics and response hashes; marked failed or partial model reviews incomplete and excluded them from reusable caches.
- Added report quality coverage and an explicit 95% model-confidence target, distinguishing self-reported confidence from measured accuracy and marking older results unverified.
- Strengthened shared model prompts with explicit guard field types and meaningful summary requirements, and expanded local analysis to small typed branches with pure operations.
- Extended benchmark reports to expose incomplete clean regions and review coverage alongside seeded-fault precision and recall.
- Advanced the analysis cache version so earlier default or malformed results are not reused.

**Source:** analysis_engine.py, analysis_quality.py, project_function_analysis.py, analysis_benchmark.py, main.py, WebUI and regression tests

## Split project workspace with more room during analysis

**2026-09-07 19:10:33 UTC** · Application / **Changed**

- Placed the project tree on the left and conversation or live analysis log on the right, using the available window width and height.
- Hid the upload button, message input, send button and mode controls during queued, running or paused analysis; restored them after completion, failure or cancellation.
- Kept the live log, project status, timer, pause/resume and stop controls visible, with independently scrolling tree and log panels.
- Added a stacked layout for smaller screens and preserved the correct analysis layout across chat switching and page reloads.
- Exposed request mode in job responses so pasted-source analysis receives the same expanded layout as project analysis.

**Source:** project_tree_assets.py, web_assets.py, main.py and browser/job regression tests

## Markdown changelog generation and download

**2026-09-07 18:45:05 UTC** · Application / **Changed**

- Replaced the generated changelog.txt with changelog.md, using headings, metadata and bullet lists for easier reading.
- Updated the generator's default output, authenticated download, WebUI link and documentation to use Markdown.
- Preserved all recorded entries, newest-first ordering and optional database installation status.

**Source:** changelog.py, main.py, web_assets.py, README.md and changelog regression tests

## Cumulative project uploads and editable project tree

**2026-09-07 18:43:22 UTC** · Application / **Added**

- Added individual and multiple-file selection alongside folder and ZIP uploads.
- Added an Upload to selector so further uploads can join an existing project or create a separate project.
- Added an expandable tree headed by the project name, with upload origins and folder and ZIP contents.
- Added right-click and action menus to delete individual files, folders with their descendants, or entire uploads from the database and tree.
- Added a persistent main-file designation and badge, supplied as entry-point context to analysis without renaming the file.
- Rejected duplicate and conflicting paths without overwriting, enforced cumulative project limits, and prevented edits during active analysis.
- Rebuilt dependency indexes and reset outdated reports after edits; retained empty projects for subsequent uploads.
- Preserved existing uploads through migration 32 and included upload origins and main-file selection in account exports.
- Verified the implementation with 333 passing Python tests and isolated Edge checks for tree rendering, deletion, main-file selection, cumulative uploads, and mobile width.

**Source:** main.py, api_models.py, project_uploads.py, project_workspace.py, project_tree_assets.py, web_assets.py, project_function_analysis.py, migration 32, and regression tests

## Migration 32: Editable Project Tree

**2026-09-07 18:30:09 UTC** · Database / **Added**

- Track upload origins and an explicitly selected project entry point.

**Source:** migrations.py :: migration_032_editable_project_tree

## Made All Results the primary report filter

**2026-08-25 00:04:29 UTC** · Application / **Changed**

- Moved All Results above Actionable Findings in the project-report type dropdown.
- Changed newly opened reports to select All Results by default.

**Source:** web_assets.py and report UI regression tests

## Deduplicated consecutive live analysis messages

**2026-08-24 23:59:19 UTC** · Application / **Changed**

- Stopped consecutive project-analysis stages with the same path/function message from adding duplicate rows to the live activity list.
- Continued updating the persisted stage, counters, file, and function fields even when the duplicate display event is suppressed.

**Source:** main.py and project-analysis job regression tests

## Lower-noise call typing, model findings, and batch recovery

**2026-08-24 23:30:49 UTC** · Analysis / **Changed**

- Made Python expression typing conservative when any conditional, Boolean, arithmetic, or container element branch has an unknown type, preventing a known branch from being reported as the type of the whole expression.
- Recognized concrete Python ast node classes as compatible with ast.AST parameters, removing false subtype mismatches without weakening unrelated type checks.
- Rejected model exception findings when the exact operation is protected by a matching non-reraising handler, and rejected IndexError claims when short-circuit control flow proves the requested element exists.
- Accepted wrapped, flattened, keyed, and exact-length ordered function batch responses; structural response failures now fall back once instead of recursively retrying the same incompatible schema.
- Classified closure parameters, locally bound callables, nested receiver methods, and unresolved externally inherited methods outside local contract coverage while retaining direct unresolved project methods as actionable unknowns.
- Made call-compatibility recalculation idempotent by replacing prior finding rows together with their compatibility rows.
- Added regression coverage for every corrected report pattern and for nearby unsafe cases that must remain visible.

**Source:** analysis_engine.py, project_function_analysis.py, project_call_compatibility.py, language_adapters/python_adapter.py, and regression tests

## Source-proven findings, broader local call coverage, and adaptive batching

**2026-08-24 22:18:46 UTC** · Analysis / **Changed**

- Rejected model claims contradicted by explicit validation raises, safe Python slices, unlink(missing_ok=True), isinstance-refined loop values, and annotated input contracts while exposing legitimate raised exceptions in each function contract.
- Added conservative Python type-flow inference for annotated parameters, prior same-path assignments, comprehensions, formatted strings, comparisons, local return annotations, and class instances while refusing later or conditional-only bindings.
- Resolved lexical closures, inherited self/cls methods, and explicit project constructors without confusing bare method names with Python class scope; classified callable parameters and arbitrary dynamic receiver calls outside local contract coverage.
- Expanded deterministic-only analysis to ordinary typed functions with calls, async work, exceptions, globals, and side effects, reserving LLM review for unknown contracts, very complex functions, and dynamic code execution.
- Changed failed model batches to split adaptively down to two-function groups, retained successful subgroups, and recorded fallback counts and the latest fallback reason.
- Marked a finished call-check pass as completed even when local targets remain unresolved, and added an explicit local-call coverage percentage so unresolved work remains visible without implying the run stopped early.
- Replaced the function-analysis cache key with cache-v6-proof-and-flow so lower-signal cached analyses are not reused.

**Source:** project_function_analysis.py, project_structure.py, project_call_compatibility.py, language_adapters/python_adapter.py, report UI, and migration 31

## Migration 31: Analysis Accuracy And Coverage

**2026-08-24 22:18:46 UTC** · Database / **Changed**

- Record adaptive batch fallbacks and treat completed local-call passes as complete.

**Source:** migrations.py :: migration_031_analysis_accuracy_and_coverage

## Higher-signal call checks and measured analysis work

**2026-08-24 20:00:28 UTC** · Analysis / **Changed**

- Fixed generic container parsing and added tuple, list, set, and dictionary literal element inference so valid container arguments are no longer reported as type mismatches.
- Rejected model syntax claims without matching parser diagnostics and variable-flow claims contradicted by deterministic scope analysis.
- Reduced broad-exception noise for structural cleanup/re-raise, reported worker boundaries, and per-item isolation while retaining warnings for handlers that silently swallow failures.
- Separated built-in, external, and dynamic calls outside local contract coverage from genuinely unresolved project calls, so they no longer make a report partial.
- Persisted and displayed actual LLM request, batch request, deterministic-only, cache reuse, unresolved-call, and outside-scope counts.
- Replaced the function-analysis cache key with cache-v5-signal-validation so older lower-signal cached reports are not reused.

**Source:** project_call_compatibility.py, project_function_analysis.py, language_adapters/python_adapter.py, report UI, and migration 30

## Migration 30: Analysis Signal Metrics

**2026-08-24 20:00:28 UTC** · Database / **Changed**

- Separate out-of-scope calls and persist model, batch, and deterministic analysis counts.

**Source:** migrations.py :: migration_030_analysis_signal_metrics

## Migration-driven application changelog

**2026-08-24 18:14:42 UTC** · Application / **Added**

- Added a generated changelog that combines migration history with curated non-database changes.
- Added an authenticated changelog page, a text download, and the repository changelog.txt file.
- Added validation so every migration must have a timestamp, action, and human-readable description.

**Source:** Curated application history

## Quieter live analysis progress and active-time timing

**2026-08-24 18:06:18 UTC** · Application / **Changed**

- Shortened function activity entries to path/function while retaining detailed counters in the static progress box.
- Changed elapsed timers and new activity timestamps to stop while paused and continue when resumed.
- Persisted pause duration so active elapsed time remains correct after a browser refresh.

**Source:** main.py, web_assets.py, and migration 29

## Migration 29: Active Job Elapsed Time

**2026-08-24 18:06:18 UTC** · Database / **Changed**

- Track paused time so job timers report active analysis time.

**Source:** migrations.py :: migration_029_active_job_elapsed_time

## Migration 28: Issue Verification Details

**2026-08-24 17:54:18 UTC** · Database / **Changed**

- Persist source proof and separate actionable defects from advisories.

**Source:** migrations.py :: migration_028_issue_verification_details

## Higher-signal analysis with fewer model requests

**2026-08-24 17:29:24 UTC** · Analysis / **Changed**

- Required proof-bearing model findings and rejected claims contradicted by the indexed source.
- Added deterministic Python scope, control-flow, and trusted-contract checks that work without an LLM call.
- Separated actionable defects from advisories and reduced broad-exception and assumption-based noise.
- Classified assumption-based hazards as Unsafe while retaining specific failure types for definite errors.
- Batched eligible function analysis within a configurable character budget and reused deterministic or cached results.

**Source:** analysis_engine.py, project_function_analysis.py, language adapters, and configuration

## Repeatable analysis accuracy benchmark

**2026-08-24 17:22:00 UTC** · Analysis / **Added**

- Added a benchmark runner and expected-results fixture for measuring true findings, missed defects, and noise.
- Added known seeded defects covering NameError and unsafe IndexError behavior for report comparison.

**Source:** analysis_benchmark.py and analysis_benchmark_fixed.json

## Migration 27: Issue Unsafe Severity

**2026-08-23 15:11:27 UTC** · Database / **Replaced**

- Allow assumption-based hazards to be persisted as first-class unsafe issues.

**Source:** migrations.py :: migration_027_issue_unsafe_severity

## Migration 26: Issue Provenance

**2026-08-15 15:55:57 UTC** · Database / **Added**

- Record whether function-analysis issues came from model, static checks, cache, or fallback.

**Source:** migrations.py :: migration_026_issue_provenance

## Migration 25: Oversized Function Chunking

**2026-08-11 20:09:21 UTC** · Database / **Reverted**

- Revert former hard-limit skips so oversized functions can use chunk analysis.

**Source:** migrations.py :: migration_025_oversized_function_chunking

## Migration 24: Function Analysis Cache

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Cache validated function contracts without sharing results between users.

**Source:** migrations.py :: migration_024_function_analysis_cache

## Migration 23: Call Compatibility

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Persist call arguments, resolved targets, and contract compatibility findings.

**Source:** migrations.py :: migration_023_call_compatibility

## Migration 22: Project Analysis Jobs

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Extend the shared Ollama queue with durable project-analysis progress.

**Source:** migrations.py :: migration_022_project_analysis_jobs

## Migration 21: Function Analysis Contract

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Store validated, versioned Ollama results for individual indexed functions.

**Source:** migrations.py :: migration_021_function_analysis_contract

## Migration 20: Project Structure Index

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Persist definition, dependency, and call ranges from Tree-sitter queries.

**Source:** migrations.py :: migration_020_project_structure_index

## Migration 19: Tree Sitter Adapters

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Store Tree-sitter adapter coverage and bounded parse diagnostics.

**Source:** migrations.py :: migration_019_tree_sitter_adapters

## Migration 18: Project File Inventory

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Add deterministic language and file-classification metadata.

**Source:** migrations.py :: migration_018_project_file_inventory

## Migration 17: Project Uploads

**2026-08-11 20:09:21 UTC** · Database / **Added**

- Store safely expanded project uploads and their file inventory.

**Source:** migrations.py :: migration_017_project_uploads

## Migration 16: Advanced Admin Controls

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Add account lifecycle, limits, and remaining audited administration tools.

**Source:** migrations.py :: migration_016_advanced_admin_controls

## Migration 15: Announcement Control

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Allow site-announcement publication and clearing to be audited.

**Source:** migrations.py :: migration_015_announcement_control

## Migration 14: Ai Work Control

**2026-08-05 21:37:27 UTC** · Database / **Added**

- Persist the owner-controlled AI-work state and its audit action.

**Source:** migrations.py :: migration_014_ai_work_control

## Migration 13: Registration Control

**2026-08-05 21:37:27 UTC** · Database / **Added**

- Persist the owner-controlled registration state and its audit action.

**Source:** migrations.py :: migration_013_registration_control

## Migration 12: Admin Maintenance Audit Action

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Allow owner-triggered retention maintenance to be audited.

**Source:** migrations.py :: migration_012_admin_maintenance_audit_action

## Migration 11: Admin Backup Audit Action

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Allow owner-triggered database backups to be recorded in the audit trail.

**Source:** migrations.py :: migration_011_admin_backup_audit_action

## Migration 10: Admin Job Controls

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Replace the audit constraint and add durable administrator job cancellation.

**Source:** migrations.py :: migration_010_admin_job_controls

## Migration 9: Admin Session Audit Actions

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Replace the audit constraint for session revocation and account unlocking.

**Source:** migrations.py :: migration_009_admin_session_audit_actions

## Migration 8: Chat List Order Index

**2026-08-05 21:37:27 UTC** · Database / **Changed**

- Index chat histories in stable newest-first display order.

**Source:** migrations.py :: migration_008_chat_list_order_index

## Migration 7: Terminal Job Maintenance Index

**2026-08-05 21:37:27 UTC** · Database / **Changed**

- Index completed and failed jobs for bounded retention maintenance.

**Source:** migrations.py :: migration_007_terminal_job_maintenance_index

## Migration 6: Admin Audit Events

**2026-08-05 21:37:27 UTC** · Database / **Added**

- Persist a durable record of privileged account changes.

**Source:** migrations.py :: migration_006_admin_audit_events

## Migration 5: Session User Index

**2026-08-05 21:37:27 UTC** · Database / **Changed**

- Index each user's sessions in newest-first order for bounded management.

**Source:** migrations.py :: migration_005_session_user_index

## Migration 4: General Auth Request Limits

**2026-08-05 21:37:27 UTC** · Database / **Replaced**

- Replace the registration limiter with reusable authentication request limits.

**Source:** migrations.py :: migration_004_general_auth_request_limits

## Migration 3: Security Cleanup Indexes

**2026-08-05 21:37:27 UTC** · Database / **Changed**

- Keep periodic expiry cleanup bounded as authentication tables grow.

**Source:** migrations.py :: migration_003_security_cleanup_indexes

## Migration 2: One Active Job Per Chat

**2026-08-05 17:33:49 UTC** · Database / **Changed**

- Prevent more than one queued or processing job in the same user chat.

**Source:** migrations.py :: migration_002_active_job_constraint

## Migration 1: Core Schema

**2026-08-05 17:33:49 UTC** · Database / **Added**

- Create the core account, authentication, chat, message, and job schema.

**Source:** migrations.py :: migration_001_core_schema
