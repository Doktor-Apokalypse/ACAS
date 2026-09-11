# Fixed project: analysis quality recheck

The response-handling, prompt, review-quality and benchmark changes are implemented. The 25 previously failed functions were retried once through local Ollama, then their captured responses were processed locally with the final validation checks. The resulting records were published to `chat_memory.db` after verifying that the live project's source and analysis records had not changed during the replay.

## Measured results

| Measurement | Before | After |
| --- | ---: | ---: |
| Completed function records | 469 / 494 | 490 / 494 |
| Failed/incomplete function records | 25 | 4 |
| Compatible local calls | 523 / 1,140 (45.9%) | 538 / 1,140 (47.2%) |
| Unknown local calls | 617 | 602 |
| Calls outside contract coverage | 4,689 | 4,689 |
| Detected benchmark defects | 3 / 3 | 3 / 3 |
| Benchmark clean region review | Failed | Complete, no retained finding |

The benchmark still reports zero missed, duplicate, unanchored or extra defect findings. One conditional model advisory remains for `_absolute_line`: it supposes that an AST node's `lineno` contains a value that cannot be converted to an integer. This is not an independently established defect and is excluded from the benchmark's defect precision calculation.

SQLite `quick_check` returned `ok`, with zero foreign-key violations. The uploaded source files were not modified. Retry snapshots and raw responses are retained under the ignored `chat_memory_backups/` directory:

- Original snapshot and 25 model responses: `review-quality-20260907-234918/`.
- Final local replay and published outcome: `review-quality-20260908-000508/`.

There were 25 retry model requests plus one diagnostic pilot request. The project's accumulated request counter changed from 644 to 669; the separate pilot is not included in that project counter. The final captured-response replay made no additional model requests. No further repeated retries were started for the four remaining failures.

## Remaining failures

| Function | Reason |
| --- | --- |
| `export_account_data` | Malformed, unterminated JSON string. |
| `deterministic_python_contract` | Confident model syntax claim contradicted by the local parser; semantic summary rejected. |
| `mark_symbol_analysis` | Missing return contract. |
| `file_is_generated` | Empty reachability and guard reasoning. |

Usable source-derived contracts and static findings remain available for these functions. Failed and partial results are excluded from reusable analysis caches.

## What the 95% target means

The model supplied scores of at least 95% for 10 of the 21 newly completed reviews. Those are self-reported estimates, not measured probabilities of correctness. The other 469 records predate explicit quality tracking and remain marked as earlier results with unverified quality. Consequently, the new quality panel counts 21 / 494 reviews under the new checks; this differs deliberately from the database's 490 completed-status records.

**Project-wide summary accuracy above 95% has not been established.** The replay demonstrated why the distinction matters:

- A 95%-confidence response claimed that `isinstance(..., ast.With | ast.AsyncWith)` was invalid syntax. The local parser accepted the source. The new contradiction check invalidated that semantic review and replaced its misleading displayed summary, retaining the original in diagnostics.
- The 95%-confidence summary for `deliver_password_reset_email` describes token deletion as a sequential step. In the uploaded source, deletion occurs only inside the exception handler after a mail-delivery failure. Its response is structurally complete, but that prose remains semantically inaccurate. A completed review therefore cannot be treated as an independent correctness certificate.

Recovering actual nested summaries fixes the earlier loss of model output. Rejecting empty output and exposing parser contradictions improves reliability. Neither change proves that every remaining natural-language claim is correct. The next accuracy work should validate branch and exception behavior against source-derived facts and evaluate summaries against a larger, manually checked corpus. Older generic summaries also require fresh analysis before they can be assessed with the new quality tracking.

## Validation

- Full Python suite: 346 tests passed.
- After the final response-alias, parser-contradiction and non-Python fallback changes: all 106 targeted response and function-analysis tests passed, including a C++ malformed-response regression.
- Existing Edge browser checks passed with the new quality panel, including safe text rendering of validation notes, project-tree interactions and analysis layout.
- `changelog.py` and generated `changelog.md` were updated.

Restart the application and refresh the WebUI to load the new report presentation. Detailed measured output is in `analysis-quality-recheck-2026-09-08.json`.
