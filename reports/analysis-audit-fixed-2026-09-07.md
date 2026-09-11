# Fixed project: persisted analysis audit

Audited the stored results and uploaded source in `chat_memory.db` using read-only SQLite connections. No analysis was rerun against Ollama and no application or database records were changed.

- Project: `Fixed`, ID `b891f01b-9a7a-4d6a-8193-a9d9b4a6d769`.
- Latest finished job: `4d092846-8f87-4634-af78-738048a0fa95`, completed at `2026-09-07 22:19:40` UTC.
- Ground truth: `analysis_benchmark_fixed.json`, checked against the actual uploaded source.
- The stored report incorporates several attempts. Project counters below are accumulated values, not measurements of the last job alone.

## Findings versus source facts

All three expected seeded defects were detected. The benchmark reports three true positives, zero extra defect findings, zero misses, zero duplicates and zero unanchored findings. Precision and recall are both 100% **for these three expectations**, not for every possible defect in the project.

The paths and line numbers below refer to uploaded database contents, which differ from the current workspace in 13 of the 27 files.

| Stored location | Finding | Source verification |
| --- | --- | --- |
| `Fixed/analysis_engine.py:1543`, `reviewable_source_identifiers` | Undefined `fact` | The parameter is `facts`, but the loop evaluates `fact[key]`. For parsed input reaching the loop, that undefined name raises `NameError`. |
| `Fixed/main.py:2732`, `upload_project` | Undefined `uploads` | The upload parameter is `files`; the ZIP branch reads `uploads[0].filename`. A valid single-ZIP request reaches this undefined name. |
| `Fixed/project_uploads.py:288`, `ingest_folder` | Insufficient index guard | `first_parts[1] if first_parts else ...` guards only against an empty tuple. A one-component path such as `main.py` raises `IndexError`. A two-component path also selects the filename rather than the folder name. |

All three stored findings have deterministic provenance. There are no retained model-provenance function findings. This does not establish that the model originally proposed no findings: rejected or malformed candidates are not represented by the final issue rows.

The benchmark's clean region, `read_limited`, has no defect finding, but its function analysis **failed**. Its absence of findings must not be counted as a successful clean analysis. Independently inspecting the stored implementation shows that reading one extra byte is deliberate: the size check immediately raises when the configured limit is exceeded.

## Completion and database consistency

| Measurement | Observed result |
| --- | --- |
| Stored files | 27 Python files |
| Independent AST parsing | All 27 parse successfully |
| File content hashes | All 27 match their stored SHA-256 values |
| Symbol and analysis source hashes | No mismatches with the associated stored files |
| SQLite quick check | `ok` |
| Foreign-key violations | 0 |
| Functions/methods targeted | 494 |
| Completed function records | 469, approximately 94.9% |
| Failed function records | 25 |
| Skipped function records | 0 |
| Completed functions missing a persisted contract | 0 |
| Project analysis status | `partial` |
| Latest background job status | `completed` |

A completed background job means processing has ended. It does not mean every function was analysed successfully; the database correctly retains `partial` at project level.

## Model-response problems

Every one of the 25 failed functions has a `guard_check` validation error:

- 22 functions received a list where a string was required.
- 3 functions received an empty string where a nonempty string was required.
- 24 failed functions have four recorded attempts; one has five. Repeating the same request has not resolved this response-format problem.

Examples include `dotted_ast_name`, `history_for`, `export_account_data`, `deterministic_python_contract`, `read_limited` and `decode_relative_paths`.

Of the 469 completed contracts:

- 448 have exactly `Function analysis completed.` as their summary and confidence `0.5`.
- One has a combined-fragments version of that same summary.
- 10 explicitly report deterministic analysis without an LLM call.
- 9 explicitly report fallback static analysis after malformed model JSON.
- One remaining contract has a different summary.

The current `normalize_function_analysis_payload('{}')` independently reproduces a successful-looking default result: `syntax_valid=True`, no parameters, no issues, generic summary and confidence `0.5`. Injecting `guard_check=[]` or `guard_check=''` into an otherwise valid stored result independently reproduces the reported validation errors.

The generic summaries are evidence of limited retained semantic information, not proof that all original responses were empty. The persisted normalized contracts are insufficient to reconstruct those original model responses.

## Call coverage and model cost

| Call outcome | Count |
| --- | ---: |
| Compatible, within local contract coverage | 523 |
| Unknown, within local contract coverage | 617 |
| Outside contract coverage / not checked | 4,689 |
| Incompatible | 0 |
| Total call records processed | 5,829 |

Only 523 of 1,140 in-scope calls have a compatible result: approximately 45.9%. Of the 617 unknown local calls, 573 have unknown argument compatibility with compatible return usage; 44 have both argument and return compatibility unknown and corresponding missing-contract findings. The 4,689 outside-coverage records are principally built-ins, external calls and dynamic receivers; they are not confirmed defects.

Therefore, zero incompatible calls does not establish that all calls are correct. The project-level `checked_count` includes all processed call records, even those outside contract coverage.

Stored accumulated counters show 644 model requests, 10 batch requests, 7 batch fallbacks, 10 deterministic-only function analyses and zero cache hits. Batch counters are not additional requests to add to the model-request total. These results are still heavily dependent on the model despite the three successful defect detections being deterministic.

## Recommended next changes

1. Harden response handling. Reject semantically empty payloads such as `{}`; validate individual model issues separately so one malformed proof field does not discard otherwise usable contract information or deterministic findings. Do not fabricate missing guard reasoning.
2. Track outcome quality explicitly: model success, deterministic-only result, static fallback, and failed semantic review. Preserve enough response/validation diagnostics to distinguish model omissions from rejected findings.
3. Stop repeating unchanged schema failures. Retry the failed functions after the response-handling correction, then reassess their contracts and downstream call coverage.
4. Increase deterministic contract coverage and reduce the 573 unknown-argument local calls before treating absence of call errors as evidence of correctness.
5. Extend benchmark scoring with coverage and failed-clean-region counts, so a 3/3 seeded-defect score cannot conceal incomplete analysis.

This audit verifies the named benchmark defects, consistency of the stored results and the demonstrated response-validation problems. It does not claim an exhaustive manual review of every function or a complete defect inventory for the uploaded project.
