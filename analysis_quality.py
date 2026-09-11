"""Measured review coverage, separate from uncalibrated model confidence."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter


def response_quality(raw: str | None, analysis_status: str) -> dict[str, object]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    method = data.get("analysis_method", "legacy" if raw else "unavailable")
    status = data.get("review_status", "unknown")
    if analysis_status != "completed" and status == "complete":
        status = "failed"
    notes = data.get("validation_notes", [])
    return {
        "method": method, "status": status,
        "notes": [value for value in notes if isinstance(value, str)] if isinstance(notes, list) else [],
        "response_sha256": data.get("response_sha256"),
        "model_confidence": data.get("confidence") if method == "model" and status == "complete" else None,
        "confidence_calibrated": False,
    }


def project_review_quality(db: sqlite3.Connection, project_id: str) -> dict[str, object]:
    rows = db.execute(
        """SELECT s.analysis_status, a.response_json FROM project_symbols s
        LEFT JOIN project_symbol_analyses a ON a.symbol_id=s.id
        WHERE s.project_id=? AND s.symbol_kind IN ('function','method')""", (project_id,)
    ).fetchall()
    methods: Counter[str] = Counter()
    complete = high_confidence = 0
    for row in rows:
        quality = response_quality(row["response_json"], row["analysis_status"])
        methods[str(quality["method"])] += 1
        complete += quality["status"] == "complete"
        value = quality["model_confidence"]
        high_confidence += isinstance(value, (int, float)) and not isinstance(value, bool) and value >= .95
    return {
        "total_count": len(rows), "complete_count": complete,
        "incomplete_count": len(rows) - complete,
        "review_coverage": complete / len(rows) if rows else 0.0,
        "methods": dict(methods), "model_confidence_at_least_95_count": high_confidence,
        "confidence_target": .95, "calibrated_confidence": None,
        "confidence_explanation": "Model confidence is a self-reported estimate, not measured accuracy. Review coverage measures completed reviews, not correctness.",
    }
