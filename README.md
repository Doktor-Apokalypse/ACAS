# Apokalypse Coder Bot

A multi-language code analysis engine with a secure FastAPI WebUI and a local Ollama model
for complex semantic review. The engine parses source, indexes dependencies, checks deterministic
facts and manages resumable project analysis. It supports file/folder/ZIP uploads, persistent
accounts and conversations, evidence-grounded findings, and owner/admin controls.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com/) or Lemonade Server running locally or reachable over HTTP
- A compatible local model with enough memory for the configured context size
- SMTP access for normal account registration and password-reset emails

The application stores its data in SQLite and does not require a separate database server.

## Local setup

Run these commands in PowerShell from the project directory:

```powershell
py -3.13 -m venv .venv313
.\.venv313\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
ollama pull deepseek-coder-v2:16B
python main.py
```

The default local address is <http://127.0.0.1:8000>. Ollama is expected at
<http://127.0.0.1:11434> unless `OLLAMA_URL` is changed.
The WebUI header shows the configured Ollama model used for chat and analysis requests.
Lemonade models whose names end in `-Hybrid` automatically use Lemonade's native
`/v1/chat/completions` transport; other models continue to use `/api/chat`. Keep
`OLLAMA_URL` set to the server base address, including when Lemonade uses port 11434.

`python main.py` also starts an ngrok tunnel when `NGROK_AUTHTOKEN` is present. Starting with
`uvicorn main:app` does not create that tunnel.

## Configuration

[`.env.example`](.env.example) documents every supported setting. It is a reference template:
the application reads process environment variables and does not automatically load `.env`.
Set the values needed by the current PowerShell session before starting it, for example:

```powershell
$env:OWNER_USERNAME = "your_admin_name"
$env:OWNER_EMAIL = "<owner-email>"
$env:SMTP_HOST = "smtp.example.com"
$env:SMTP_PORT = "587"
$env:SMTP_USERNAME = "<smtp-user>"
$env:SMTP_PASSWORD = "use-a-secret-or-app-password"
$env:SMTP_FROM = "<sender-address>"
$env:PUBLIC_BASE_URL = "https://chat.example.com"
$env:TRUSTED_HOSTS = "chat.example.com"
python main.py
```

Do not put real passwords, API tokens, or private hostnames in `.env.example` or source control.
The local `.env` filename is ignored by Git.

### Project uploads

The `+` button beside the chat composer offers **Select file(s)**, **Select project folder**, and
**Select ZIP file**. Select one or more individual files to analyse them together as one project
attachment. A single-file attachment uses its filename; multiple files use a shared project label.
Individual selections use filenames only; use the folder picker to preserve directory structure.
Duplicate filenames are rejected. Uploaded files are normalized, checked for traversal paths, duplicate names,
links, special files, encryption, unsafe compression ratios, and configured size/count limits,
then stored in SQLite without executing or extracting them onto the host filesystem. Common VCS,
dependency-cache, virtual-environment, and build-output directories are skipped. Project data is
owned by its chat, counts toward per-user storage, is included in account exports, and is removed
when the project, chat, or account is deleted. The `PROJECT_UPLOAD_*` settings in `.env.example`
control the upload limits.

Choose an existing project in **Upload to** to append files, folders or ZIP archives through the
`+` menu. After creating a project, it becomes the selected upload target. Choose **New project**
to keep the next upload separate. Uploads never silently replace an existing path; conflicts and
the combined project's file/byte limits are checked before anything is committed.

The expandable tree shows the project name, each upload's origin, and its files and directories. For
folder uploads, the upload branch also represents the selected root directory instead of repeating
the same directory as its first child.
Right-click an entry, or use its **⋮** button, to delete a file, a folder and its descendants, or
an entire upload. **Set as main file** marks a source file as the project's entry point and adds a
Main badge; it does not rename the file. The choice is saved and supplied to the analysis engine.
Deleting the selected main file clears the choice. An emptied project remains available for new
uploads. Edits reset stored reports and rebuild dependency indexes; active analysis must be stopped
before editing. The three-dot button beside the project name can rename or delete the whole project;
deletion is refused while that project's analysis is active. Migration 32 adds the tree metadata
while preserving existing uploaded contents.

The WebUI is an analysis-only workspace titled **Apokalypse Code Analysis System**. It opens the
most recent analysis workspace automatically without showing the chat-history sidebar or a request-
mode switch. Pasted source submissions always use analysis mode.

On desktop, the project tree occupies the left of the full-width workspace and the conversation or live
analysis log occupies the right. Project status actions stay aligned to the right so the status and
current function text can use the remaining width. During analysis (including queued and paused work), upload and
message-entry controls disappear to expand these panels; the timer, pause/resume and stop remain
available. Starting a project analysis selects that project and locks the project dropdown to it;
the selector unlocks after completion, cancellation or failure so another project's tree and report
can be viewed. Smaller screens stack the tree above the live log, with each panel scrolling independently.

The selected main file appears green. Files and functions currently being analysed turn cyan, while
failed functions appear red. A file appears red when all its processed functions failed and yellow
when its completed results are a mixture of passed and failed. After every indexed function in a
successful file has been processed, a tick appears and the filename returns to white (green for the
main file). Paused files show a pause marker. Hover over a file for processed, failed and skipped counts:
a tick indicates processing has finished, not that the file is defect-free.
These states refresh with the live log and are restored after page reloads. Live progress updates
preserve collapsed folders. Files without indexed functions do not receive a completion tick.
Expand a source file to see its indexed functions. The active function turns cyan with its file and
its branch opens automatically. Hover over a function to see its source declaration, completed description,
and its return statements. Alternative returns are labelled **flow dependent** and include their source
lines; exact text already present in the description is not repeated. Model-reviewed descriptions appear
directly; functions completed by the local AST pass show a **Static analysis** description, and functions
with neither result show **Unknown**. Click a function to open its statically resolved project callers,
including each enclosing function or module-level location, call line, usage kind and exact call expression.
A function without a completed model
description retains a three-dot **Get description** action, which queues a forced semantic review for
that function alone. Descriptions reuse the validated stored summary and are capped at 400 characters,
so the model response contract does not need a duplicate field.
The live status reports monotonic project results separately from the current file. Because analysis
follows dependency order, the current symbol's source position can move backwards within a file; it is
labelled **Source position** rather than presented as completed-work progress.

Analysis reports distinguish model review, deterministic static analysis, fallback results and
older results without quality metadata. Empty or generic model responses are rejected. Invalid
individual findings retain validation diagnostics while valid contract information and static
findings remain available; incomplete model reviews keep the project partial and are not cached.
The **Review quality** panel separates completed-review coverage from model confidence. The 95%
confidence target is shown against self-reported model scores, which are not calibrated accuracy
probabilities. The engine does not raise scores to meet the target. Benchmark output also lists
clean regions that have not been fully reviewed.

Every upload also receives a deterministic file inventory without contacting Ollama. Extensions,
special filenames, project manifests, shebangs, conservative content signatures, and project-wide
context classify each file and select a primary project language. The inventory currently
recognizes Python, Pascal, C#, C/C++, Rust, JavaScript/TypeScript, HTML, SQL, POSIX shell and
PowerShell, along with common ancillary configuration, data, documentation, and stylesheet files.
It records encoding, line count, confidence, detection method, binary/generated status, likely
entry points, and whether a file is eligible for later structural analysis. Ambiguous `.h` and
`.inc` files use the surrounding project's unambiguous files and manifests. Existing uploads are
backfilled on startup after migration.

Tree-sitter structural analysis currently supports Python, C/C++, C#, JavaScript, TypeScript,
Rust, POSIX-style shell, PowerShell, HTML (including inline JavaScript), and SQL. Project analysis
indexes functions and call sites before reviewing pending functions.
Each target receives a source-derived dependency slice containing only its referenced imports,
module declarations, and project symbol signatures, while results remain independently validated
and persisted. Stored contracts describe parameters, return
values, issues, and confidence; resolved internal calls are then checked conservatively against
those contracts. The project chip's **Report** button shows the stored function results and call
compatibility findings without returning uploaded source code to the browser. Ambiguous targets
and values whose types cannot be proven are reported as unknown instead of being treated as
errors. `FUNCTION_ANALYSIS_CHUNK_CHARS` now defaults to **20,000 characters**: a function at
or below that size is reviewed whole. Functions larger than this threshold are split into contiguous,
line-aware fragments and their validated results are merged without another model request.

Before making model requests, a project pass now runs the existing local checks across its selected
pending functions. Functions covered by the conservative local completion rules are saved immediately;
remaining functions receive semantic review in dependency order. Parsing never executes uploaded code.
The local path recognizes built-in exception constructors used directly by `raise`, allowing small,
fully resolved validators to complete without an unnecessary model request. Arbitrary calls and
user-defined exception constructors still require review. Complex functions still need a model:
extracting their structure alone does not establish correct behavior.

For Python and supported ordinary TypeScript function signatures, the engine owns syntax, parameter
names/kinds/defaults, declared types, the stored summary, uncertainty and confidence. Python also
supplies bounded return/raise occurrences and decorator facts. These facts are sent alongside the
complete function and dependency context. Ollama returns a strict semantic JSON object containing only
one source-anchored behavior claim, missing type inferences, return evidence, escaping exceptions, side
effects and proof-bearing issues. Every claim includes 1-based source-relative lines and an exact source
excerpt. After matching the excerpt inside the target, the engine converts those coordinates to absolute
file lines for storage. It also accepts already-absolute coordinates when the excerpt proves them. If the
model supplies an incorrect line but its exact excerpt occurs only once inside the target, the engine
recovers the source line from that unique match and records the correction. A unique token sequence whose
only difference is collapsed whitespace is restored to the exact multiline source before acceptance.
Ambiguous excerpts remain rejected. The
model cannot replace the target name, parsed signature, syntax verdict, summary or confidence. Declared types
remain declarations rather than runtime guarantees; caught raises are not automatically reported as
escaping exceptions. Other languages and unsupported signature shapes retain the existing full-review
path. Compact reviews use individual requests even when batching is enabled for the full-review path.

For unannotated Python functions, the local pass also resolves source-proven return relationships before
asking Ollama. This includes returning a typed parameter, `self`/`cls`, literals and container expressions,
unshadowed built-in constructors, conditional expressions whose branches are known, and boolean fallback
expressions whose operands are known. It still requests inference for implicit fallthrough, generators,
unknown calls and shadowed constructors. These rules reduce the model's work without executing uploads.

The engine verifies each line range and excerpt against the supplied target before merging the response
into the existing report/database format. Unsupported individual claims and issues are discarded locally
and recorded in source-fact verification metadata. A missing behavior anchor spends one bounded model
repair; if both model anchors fail, the engine selects and verifies one exact target-source statement for
the description while retaining the rejected model claims in provenance. Missing contract inferences stay
partial, with confidence capped at 0.8. A clean, completely
grounded contract can receive engine confidence 0.97; model type inference or accepted semantic findings
cap it at 0.95, rejected claims at 0.90, and truncated source observations at 0.90. Confidence therefore
measures verified evidence coverage rather than model self-assessment. Malformed/truncated JSON,
unavailable tool requests and a missing required behavior anchor enter the one-repair path. Backend failures
and cancellation still stop promptly.
Live project totals refresh after each persisted review and are included in queued and active job responses.
Completed reviews survive restart; the new facts
and cache versions prevent reuse of older response contracts for pending work.
Common semantic response deviations are normalized before strict validation: an unsupported behavior
kind is inferred from its exact evidence, and extra behavior claims are discarded after the first because
the compact contract requests exactly one. The retained claim still has to pass source-line, evidence and
behavior-kind verification.

The compact schema is specialized for each function. If source facts already provide every parameter
type, the model-facing grammar requires `parameter_inferences: []`. Return inference uses five flat
fields: `return_has_value`, `return_types`, `return_nullable`, `return_line` and `return_evidence`. All five
are forced to null/empty values when the engine already owns the return contract. The engine checks their
coherence and source anchor, then constructs the nested stored return contract locally. Parameter names
are restricted to unresolved names. Irrelevant or duplicate guesses are discarded where they cannot
override engine facts; malformed required inference still fails or remains partial. Discarded wire fields
and rejected claims are recorded in stored source-fact metadata for later review.

`FUNCTION_ANALYSIS_CONTEXT_CHARS` defaults to **20,000 characters** in addition to the function
source. The engine reserves space for completed callee contracts and a partial map of Python branch
and exception paths, then includes referenced declarations and remaining source excerpts. Long
contracts can omit descriptions while retaining structured types. Legacy and incomplete model
reviews are not forwarded as completed-callee contracts; every inferred contract remains a hypothesis.

Function analysis uses adaptive Ollama context by default. `OLLAMA_ANALYSIS_CONTEXT_MIN=8192`
and `OLLAMA_ANALYSIS_CONTEXT_MAX=65536` bound the request tiers. The full assembled prompt,
schema and response allowance are budgeted using a conservative UTF-8 byte estimate plus a
2,048-token margin; this is not an exact tokenizer count. A project job retains its largest chosen
tier until it finishes, avoiding repeated downward resizing. Separate jobs have independent context
state. This does not guarantee a speedup: model loading and actual prompt length both matter.
Requests whose estimate exceeds the ceiling fail explicitly without silently dropping source.

Full function and chunk reviews select **4,096 / 8,192 / 16,384 output tokens** using structural
heuristics by default (`FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT=true`). Python AST and the installed
language parsers measure branches, loops, nesting, exception handlers, parameters and return
paths without executing source. Missing grammar support or incomplete fragments raise uncertainty.
Distinct resolved/unresolved callees, cross-file dependencies and recursion determine a separate
internal dependency score. Repeated calls count once; recognized unshadowed Python built-ins are
excluded from unresolved calls. The WebUI displays the actual number of distinct resolved plus
unresolved dependencies, while the weighted score remains available internally for budgeting.
The complexity and internal dependency scores are bounded from 1 to 100.

The initial formulas are `C = min(100, 1 + 3B + 5L + 6×max(0,N−1) + 4H + 2×max(0,R−1))`
and `D = min(100, 1 + 3K + 8U + 2X + 15×recursive)`, where B/L/N/H/R are branches, loops,
nesting depth, handlers and return/yield paths, and K/U/X count distinct resolved, unresolved and
cross-file callees. An incomplete structural parse sets C to at least 50. A representative JSON
structure using parameter names, plus allowances for descriptions and evidence, estimates J.
It reserves space for potential findings without requiring the model to invent any.
The initial budget is `ceil(1.25 × (J + 1024 + 32C + 16D + uncertainty_allowance))`;
uncertainty adds 1,024 tokens when structural parsing is incomplete. The smallest fitting tier
is selected; estimates beyond 16,384 are flagged, never presented as guaranteed fits.

Compact semantic reviews use separate **1,024 / 2,048 / 4,096 output-token tiers** and estimate J from
their evidence-only response structure. For explicitly named `qwen2.5-coder:*` models, their estimate is
`ceil(1.25 * J)` with no separate thinking allowance; GPT-OSS retains its reasoning allowance. A matching
truncation advances to the next compact tier when one is available. Compact-response usage history is
separated from the old full-response history and versioned with the evidence response format, and Qwen's
history does not depend on the GPT-OSS reasoning setting. During streaming, the engine checks the latest
3,000 answer characters every 500 characters and interrupts two consecutive strong repetition detections.
`FUNCTION_ANALYSIS_REQUEST_TIMEOUT` defaults to **300 seconds** and bounds the wall-clock duration of
each function, chunk, or batch generation. A timed-out function is recorded as incomplete and the project
continues; ordinary chat generation retains the independent idle socket timeout.

Previous truncation of matching source raises its starting tier. Valid observed generation
counts add a 15% margin at the 90th percentile; comparable cross-project observations only
influence the estimate after eight samples. History is isolated by user, language, model tag,
reasoning setting and estimator version. Calibration only raises an initial estimate. Batch
counts are never attributed to individual functions. Up to 10,000 observations per user are
retained, including total generated tokens and separate thinking/answer character counts;
the latter are **not exact token counts**. Historical measurements begin with this version.
Resetting analysis retains this history; deleting its project removes it.

The active budget appears above the project tree, with per-file tooltips and per-function report
API data. Scores and estimates are heuristics, not measured accuracy or guaranteed reasoning limits.
Set `FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT=false` to restore the fixed
`FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS` allowance (default 8,192). Batches share at most
`FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS` (default 16,384), splitting before generation when
combined estimates exceed this ceiling. An individual truncation retry can grow to 16,384 tokens.
The context budget reserves this output space as well as the prompt and schema. These are
maximum allowances, not required response lengths. More output space addresses truncation;
missing fields and unsupported claims still require structured-output and source validation.

While an Ollama response is streaming, the engine checks sufficiently long output for the same strong
repetition signals used by final-response validation. Two consecutive detections close the response at
about 7,000 answer characters and use the existing single transport retry. This prevents a token loop
from consuming an entire 4,096/8,192/16,384-token allowance; normal short output is never checked, and a
response still has to pass the final degeneracy test before it is accepted.

For a fixed-window comparison, set process environment variables
`OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT=false` and `OLLAMA_CONTEXT_SIZE=65536`. Ordinary chat continues
to use `OLLAMA_CONTEXT_SIZE` (default 32768). The engine logs `Ollama analysis budget` and
`Ollama analysis timing` records with context size, actual token counts and load/prompt/generation
times. Existing environment overrides take precedence over defaults; `.env.example` is a reference,
not an automatically loaded file. Restart the application to apply changes. Reset the project's
analysis before comparing complete runs on identical uploaded files; the cache version has changed.

To use `qwen2.5-coder:14b-instruct-q5_K_M`, install that exact Ollama tag and set
`OLLAMA_MODEL=qwen2.5-coder:14b-instruct-q5_K_M` in the PyCharm run configuration before restarting.
Keep `FUNCTION_ANALYSIS_BATCH_SIZE=1` for the comparison. The engine omits the GPT-OSS `think`
option for Qwen2.5-Coder. Resume preserves earlier completed GPT-OSS reviews; reset analysis or use a
fresh project when you want a complete Qwen-only comparison. Model tags are distinct, including
their quantization suffixes. These code changes do not download or switch the running model.

`OLLAMA_GPT_OSS_REASONING` has no effect on Qwen2.5-Coder. Ollama's thinking controls currently list
Qwen 3 and GPT-OSS among supported model families, while this Qwen2.5-Coder tag is a standard instruct
model. Raising `low` to `medium` or `high` therefore cannot trade speed for better Qwen2.5 reasoning;
the application deliberately sends no `think` field for it. Use a thinking-capable model family if an
explicit reasoning trace is required, then benchmark its JSON adherence and throughput separately.

The existing 65,536-token engine ceiling is not a declaration of a model's native context support.
[Qwen's model card](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct#processing-long-texts)
specifies a 32,768-token default configuration and YaRN for longer contexts in supported frameworks.
Verify the local backend's long-context configuration before treating 65,536 or 131,072 as supported;
`OLLAMA_ANALYSIS_CONTEXT_MAX=32768` explicitly bounds analysis to the native size if needed. Requests
that exceed the configured ceiling fail visibly rather than silently shortening the source. Source
chunking and dependency-context caps remain at 20,000 characters with this change.

`FUNCTION_ANALYSIS_BATCH_SIZE` and `FUNCTION_ANALYSIS_BATCH_MAX_CHARS` bound each model request;
an invalid batch automatically falls back to isolated function requests. Completed contracts are
cached per user, language, model, contract version, function source, and dependency context so
unchanged functions can be reused safely without retaining stale callee or global declarations. The maintenance worker
removes cache entries older than `FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS` and enforces
`FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER` so reuse cannot cause unbounded database growth.

Resolved callee excerpts are available across supported languages; Python also receives referenced
module declarations. Callees are scheduled before callers. Recursive components retain stable
ordering and receive source context without exchanging inferred contracts. Completed callee
contracts are labelled as hypotheses for subsequent model review. Batching defers callers whose
dependencies still need analysis. Cache versioning covers the proof rules, and dependency context
includes fingerprints of referenced symbol bodies even when only a signature fits in the prompt.

Large pasted-code reviews use the same language detection and Tree-sitter adapters as uploads,
while retaining Python's richer AST facts. Unknown languages or failed grammars fall back to an
explicitly limited lexical inventory. For multiple fenced blocks, evidence verification covers
the largest block and states how many other blocks were excluded.

New model findings must include an exact source excerpt, line, failure type, and concrete trigger.
They must also include a concise reachable failure path, a guard check, exact excerpts of any
relevant guards, and a defect/contract-risk assessment. Missing proof or fabricated guard excerpts
are rejected. These fields remain in the stored analysis JSON; conditional risks use the Unsafe
report lane. Exact source matching establishes provenance, not the truth of a model's reasoning.
Parser and source-derived checks can reject contradictory claims before persistence. The report's
default view contains actionable Error, Unsafe, and Warning findings; maintainability advisories
are stored separately and remain collapsed unless explicitly requested.

Finding one deterministic defect no longer skips semantic review of the remaining function.
Invalid syntax can finish at the parser stage; simple Python contracts retain a fast path, while
branches, I/O and unresolved helper calls receive model review. A narrow JavaScript/TypeScript
rule detects literal-null property access in an unconditional return, excluding optional chains,
guarded branches, handlers and nested functions. Other semantic rules remain language-specific;
parser support does not imply complete semantic coverage.
Ordinary TypeScript parameter names, required/default/optional status and declared types are
extracted directly from the grammar and override model guesses. Unsupported signatures such as
destructuring remain on the model path. These declared contracts also feed the existing call
compatibility checks, allowing missing arguments to be detected without model-inferred types.

### Analysis benchmarks

`analysis_benchmark.py` scores a stored report against source anchors instead of fragile fixed line
numbers. It reports true positives, false positives, missed defects, duplicates, advisories, clean
region findings, and the unanchored rate under an explicit analyzer-version label. The supplied
`analysis_benchmark_fixed.json` describes the three deliberate faults in the `Fixed` test project
and treats `read_limited`'s one-byte boundary probe as a clean control.
Evaluation opens the database read-only and includes per-language scores and recorded model,
batch, deterministic and cache counts. Missing or ambiguous source anchors cannot match arbitrary
lines, and advisories cannot satisfy an expected defect.
Syntax expectations use the file-level parser diagnostics, including errors that prevent a
function from being indexed; matching copies in function findings are counted only once.

```powershell
& .\.venv313\Scripts\python.exe analysis_benchmark.py evaluate chat_memory.db PROJECT_ID analysis_benchmark_fixed.json
```

Generate nine clean/mutated pairs across Python, JavaScript, TypeScript, C++, C#, Rust, shell and
PowerShell, covering scope defects, null access, syntax diagnostics and call compatibility:

```powershell
& .\.venv313\Scripts\python.exe analysis_benchmark.py generate .analysis-benchmark
```

Upload and analyse the generated corpus, then evaluate its stored report against
`.analysis-benchmark/benchmark_manifest.json`. Keep the same model and settings when comparing
versions. Regression tests also exercise the corpus with a model stand-in contributing no findings;
that measures engine coverage only and is not a live Ollama accuracy measurement.

### Account ownership

Set `OWNER_USERNAME` and `OWNER_EMAIL` before the matching account completes registration. Both
values must match that registration; the account is then created as the protected owner/admin.
Changing only one of these settings does not transfer ownership.

Administrators can search the user directory by username or email, filter it by role and ban
status, sort by identity or usage, and move through bounded result pages. The server performs all
filtering and ordering before returning `ADMIN_USER_PAGE_SIZE` rows (50 by default), so the Admin
WebUI does not need to load the entire account table.

The same table shows account creation and last-login times, active session counts, temporary-ban
reason and expiry, and account-specific login failures or lock expiry. Administrators can revoke
every session, clear member login restrictions, issue a time-limited or permanent member ban, or
queue a password-reset email without receiving the secret reset token. Those operations against
another administrator require the owner, and the protected owner account cannot be changed. Each
successful operation is written to the administrator audit trail.

The owner can set tighter per-user storage, active-job, and pending-input limits. Per-user job and
input limits cannot exceed the server-wide safeguards. The owner can also anonymize an account and
its stored conversations or permanently delete it after typing the exact username; active jobs and
the owner account are protected from either operation.

The Admin WebUI also monitors every queued or processing model job, including its owner, chat,
type, progress, elapsed time, and worker state. Administrators can cancel member jobs immediately
while queued or cooperatively while processing. Only the owner can cancel administrator jobs;
owner jobs are protected. Administrative cancellations include the job ID in the owner-only audit
trail.

All administrators can also view a read-only system-health dashboard. It shows application uptime,
database availability and size, schema and record counts, Ollama/model readiness, job-worker and
queue state, and periodic-backup scheduling. The dashboard omits service URLs, filesystem paths,
mail settings, credentials, and tokens. Its successful background refreshes are excluded from the
routine HTTP access log.

The owner can create an on-demand backup from the same dashboard. Manual backups use SQLite's
online backup API, must pass an integrity check before publication, share the configured bounded
routine-backup retention, and never expose their server filesystem path or contents to the browser.
Each successful manual backup is included in the administrator audit trail.

The owner can also run retention maintenance on demand. This uses the same rules as automatic
maintenance: it removes expired sessions and one-time authentication records, prunes stale
throttles and audit entries beyond their configured retention, and clears aged duplicate job
payloads only when canonical chat messages remain available. Active credentials, current
throttles, canonical conversations, and active jobs are preserved. The resulting totals are
reported in the WebUI and audit trail.

The owner can run a SQLite integrity check from the health dashboard. The newest result and check
time are stored and shown only to the owner; a failed check returns a safe error and is also handled
by the configured error-notification path.

The owner can persistently open or close new-account registration from the health dashboard.
Closing registration blocks new verification-email requests and prevents outstanding verification
links from creating accounts until registration is reopened; it does not affect login, password
reset, existing sessions, or existing accounts. The public registration page clearly reports the
closed state, and every actual setting change is audited.

The owner can independently pause new AI work. While paused, new chat, analysis, and evidence-
verification retry submissions receive a temporary-unavailable response before their input is
stored. Existing queued and processing jobs are not cancelled, and account access, history,
exports, administration, and password recovery continue normally. The persistent state is shown
to all administrators, and actual pause/resume changes are audited.

The owner can publish or clear a site announcement with an information, warning, or critical
level and an optional expiry of up to 30 days. All administrators can see the current announcement,
while signed-in users receive a dismissible banner that refreshes quietly in the background. The
message is always inserted as plain text rather than HTML, expiries are enforced server-side, and
publication and clearing are audited without exposing server details.

The owner-only Pending registrations panel can search, page through, and revoke unused account
verification requests without exposing token digests. The audit panel supports actor, target,
request-ID, action, and date filtering with server-side pagination, plus a UTF-8 CSV export. CSV
cells beginning with spreadsheet formula characters are escaped before download.

### Error push notifications

Application `ERROR` and `CRITICAL` console records are also published to the configured NTFY
topic. Delivery uses a bounded background queue, so a slow or unavailable notification server
does not delay requests or model work. Identical errors are suppressed for 60 seconds by default,
long tracebacks are bounded, and delivery failures produce at most one console warning every five
minutes instead of recursively generating more notifications.

Set `NTFY_TOPIC` to a private topic name to enable alerts, or leave it empty to disable them.
Use `NTFY_SERVER_URL` for a self-hosted server. A protected topic can use an access token supplied
only through the `NTFY_ACCESS_TOKEN` environment variable.

### Public deployment

For a public instance:

1. Put the application behind HTTPS or let `python main.py` create an ngrok HTTPS tunnel.
2. Set `PUBLIC_BASE_URL` to the exact external origin, with no path or trailing slash.
3. Put the corresponding hostname in the comma-separated `TRUSTED_HOSTS` setting.
4. Configure SMTP and keep its credentials in the deployment environment or secret store.
5. Copy the configured backup directory to separate storage so a host or disk failure cannot
   destroy both the database and its backups.

When an existing database has pending schema migrations, the application automatically creates
and validates an online pre-migration copy in `MIGRATION_BACKUP_DIR`. Fresh databases and normal
restarts do not create redundant copies. Keep ordinary off-machine backups as well; migration
copies protect upgrades but are not a disaster-recovery strategy. The application also makes a
verified online backup every `PERIODIC_BACKUP_INTERVAL_SECONDS` (24 hours by default), keeping the
newest `PERIODIC_BACKUP_RETENTION_COUNT` routine copies. These settings can be disabled with
`CREATE_PERIODIC_BACKUPS=false`; routine rotation never deletes pre-migration copies.

To restore, stop the service, preserve the current `CHAT_DB_PATH`, copy the chosen periodic `.db`
file into that path, and run `PRAGMA quick_check` before restarting. Restore testing and an
off-machine copy remain necessary: local rotation protects against accidental data damage, not
loss of the whole host.

The server refuses incomplete email/public-URL configuration at startup. Cross-origin unsafe
requests and untrusted `Host` headers are rejected. Responses also include a nonce-based content
security policy, clickjacking/MIME protections, a no-referrer policy, and HSTS on HTTPS requests.
Malformed, out-of-range, or contradictory environment settings stop startup with an error that
names every invalid relationship instead of being silently clamped.

## Common settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | HTTP bind address and port |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server base URL |
| `OLLAMA_MODEL` | `deepseek-coder-v2:16B` | Model used for chat and analysis |
| `CHAT_DB_PATH` | `chat_memory.db` beside the code | SQLite database location |
| `PUBLIC_BASE_URL` | empty | Canonical external HTTP(S) origin |
| `TRUSTED_HOSTS` | local hosts | Additional comma-separated hostnames |
| `NTFY_TOPIC` | NTFY notification topic name | NTFY topic for error alerts; empty disables alerts |
| `JOB_QUEUE_CAPACITY` | `20` | Maximum queued Ollama jobs |
| `MAX_ACTIVE_JOBS_PER_USER` | `3` | Per-user queued/processing limit |
| `MAX_MESSAGE_CHARS` | `1500000` | Maximum submitted message size |

See `.env.example` for model limits, throttling, session lifetime, SMTP, and analysis settings.

## Health probes

- `GET /health` is a lightweight liveness probe. It returns HTTP 200 while the web process can
  serve requests and does not expose internal configuration.
- `GET /ready` checks SQLite, the dedicated job worker, Ollama, and the configured model. It
  returns HTTP 200 only when chat work can be completed, otherwise HTTP 503. Results are cached
  briefly to prevent monitoring traffic from repeatedly contacting Ollama.

## Request correlation

Every HTTP response includes `X-Request-ID`. A safe ID supplied by a trusted upstream is
preserved; malformed values are replaced with a generated ID. Completion logs include that ID,
the method, path, status, and elapsed time. Query strings are deliberately excluded so email
verification and password-reset tokens are not written to access logs. Unexpected HTTP 500
responses are generic and include the request ID for correlation with the server traceback.

## Database maintenance

Expired login sessions, registration links, and password-reset links are removed at startup and
periodically while the service runs. Stale login and registration throttles are pruned on the same
schedule. Dedicated expiry indexes keep maintenance efficient without deleting active sessions,
valid one-time links, or current lockouts.

Terminal chat-job rows retain their status and links to canonical chat messages, but duplicate
prompt/reply payloads and detailed progress logs are cleared after
`TERMINAL_JOB_PAYLOAD_RETENTION_DAYS` (seven days by default). Delayed polling still resolves a
completed reply from the linked assistant message. Chat history content itself is not removed by
this maintenance task.

Opening a chat initially returns only the newest `CHAT_HISTORY_PAGE_SIZE` messages (20 by default).
The browser offers a cursor-based “Load earlier messages” control, so long conversations do not
require one unbounded database query, JSON response, and DOM render. Progress history is queried
only for assistant messages present in the requested page.

Analysis-workspace lookup uses `CHAT_LIST_PAGE_SIZE` (50 by default). Its opaque keyset cursor
includes every list-ordering field; the WebUI automatically selects the newest returned workspace.

## Account data export

Signed-in users can select **Export data** in the account header to download a streaming JSON copy
of their profile and complete canonical chat/message history. The export includes compact context
stored alongside messages, but never includes password hashes, session credentials, verification
or reset tokens, rate-limit records, or duplicate background-job payloads. Export responses are
marked `no-store` and use an attachment filename derived from the validated username.

Passwords are stored with bounded scrypt parameters. Older valid hashes are accepted and upgraded
to the current work factor after a successful login, so existing users do not need to reset their
passwords when the hashing policy changes. New and reset passwords require at least 15 characters,
allow spaces and Unicode, and are checked against a local common/expected-password blocklist rather
than requiring predictable uppercase, number, and symbol combinations.

Registration and password-reset email requests also have separate persistent per-IP allowances.
Rate-limited requests retain the same generic response as accepted requests, preventing the limit
from becoming an account-enumeration signal. Valid requests also share a short configurable
response-time floor so the faster unknown-account path does not reveal whether an identity exists.

Successful login rotates any session presented by that browser. Each account retains only its
newest configured number of sessions, limiting forgotten or duplicated credentials while allowing
normal multi-device use. Logout expires the cookie with attributes matching its HTTPS session.

Every successful administrator account or job-control action is recorded transactionally with the
acting and target account snapshots, action, time, HTTP request ID, and relevant safe details. The
owner can review and filter events at `GET /api/admin/audit` or download the filtered results from
`GET /api/admin/audit/export`; ordinary administrators cannot read this trail. Records are retained for
`ADMIN_AUDIT_RETENTION_DAYS` (365 by default) and pruned by the normal security-maintenance cycle.

## Changelog

Signed-in users can open **Changelog** from the account header. Database entries are generated from
the ordered migration registry and show whether and when each migration was applied to the current
database. Curated entries cover application changes that do not require a schema migration.

The repository copy is [`changelog.md`](changelog.md), newest first, with headings and bullet lists.
The WebUI's **Download Markdown** link downloads the same format. Regenerate it after adding a
migration or curated entry with:

```powershell
python changelog.py
```

Use `python changelog.py --database chat_memory.db` when a separate output should also include that
database's installation status. Migration timestamps are chronology markers, not release dates.

## Verification

The regression suite uses temporary databases and fake Ollama streams; it does not modify the
live chat database or require Ollama to be running:

```powershell
python -m unittest discover -s tests -v
python -m py_compile main.py app_config.py api_models.py authentication.py database.py analysis_engine.py changelog.py project_uploads.py project_inventory.py web_assets.py migrations.py
```

## Troubleshooting

- **Ollama connection failure:** confirm `ollama serve` is running and `OLLAMA_URL` is reachable.
- **Model occupies VRAM but inference never starts:** inspect Ollama's `server.log` in
  `%LOCALAPPDATA%\Ollama`. `timed out waiting for llama-server to start` is a backend
  loading failure. The engine stops the pass on this error, model-load/memory failures,
  connection failures or an idle response timeout, preserving completed reviews and leaving
  active functions pending for resumption. Batch requests are not split and retried against
  an unavailable backend. Fix Ollama before resuming; extending output-token limits will
  not resolve a model that cannot finish loading.
- **Integrated GPU memory allocation:** reserving more RAM for the GPU leaves less for
  Windows, model loading and host buffers. Monitor available physical RAM and paging as well
  as VRAM. A larger pagefile increases the commit limit but does not add physical RAM.
  `ollama ps` reports model placement while a model is loaded; it is not a GPU-utilization meter.
- **Model not found:** run `ollama pull <model>` and make `OLLAMA_MODEL` match that name.
- **Registration mail fails:** configure both `SMTP_HOST` and `SMTP_FROM`; authenticated servers
  normally also need `SMTP_USERNAME` and `SMTP_PASSWORD`.
- **Public host returns 400:** add only the hostname (not a URL) to `TRUSTED_HOSTS`, then restart.
- **Startup rejects the public URL:** use an exact `http://` or `https://` origin without a path,
  query string, or fragment.
