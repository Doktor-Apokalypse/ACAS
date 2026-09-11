# Analysis error fixture expected findings

Use `main_analysis_error_fixture.py` as a deliberately broken upload fixture. It is
not intended to run or compile.

Expected high-signal findings:

- `fixture_undefined_variable_path`: undefined variable `account_id`.
- `fixture_wrong_function_call_signature`: invalid call signature for `save_message`.
- `fixture_unawaited_async_database_update`: async result from `fixture_update_title_async` is not awaited.
- `fixture_bad_attribute_access`: possible `None` dereference and invalid `sqlite3.Row` attribute access.
- `fixture_sql_injection_candidate`: SQL query built with untrusted string interpolation.
- `fixture_leaked_file_handle`: file handle opened without a context manager or explicit close.
- `fixture_mutable_default_accumulates`: mutable default list leaks state across calls.
- `fixture_return_type_mismatch`: annotated `int` return can return `str`.
- `fixture_broad_exception_swallowing`: broad `except Exception` silently masks malformed input.
- `fixture_missing_required_call_arguments`: required `request` argument missing for `require_user`.
- `fixture_invalid_syntax_branch`: deliberately invalid Python syntax.

Useful scoring guidance:

- A strong analysis should find most of the fixture defects without inventing
  unrelated critical issues in unchanged application code.
- Syntax recovery may vary by parser/model. If the invalid syntax prevents later
  extraction, move `fixture_invalid_syntax_branch` to the end of the file, or test
  it in isolation.
- Treat framework/subclass mismatches, such as `HTMLResponse` vs `Response`, as
  likely static-analysis false positives unless runtime evidence says otherwise.
