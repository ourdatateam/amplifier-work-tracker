"""Tier 1 -- the pure data/summary derivations in `amplifier_work_tracker.adapter`:
the ready-age histogram, `project_activity`'s throughput/last-activity figures,
and `project_summary`'s health-status honesty.

Everything here is a pure function of constructed `Item`s (or, for the
creation-state branch, a `.create.lock` file under a `tmp_path` workspace) --
none of it needs `bd` installed, a dolt server, or the live workspace. Every
timestamp is forged relative to `datetime.now(UTC)` so nothing sleeps.

These pin the four data-layer invariants the dashboard depends on:
  1. a broken/half-created project surfaces a DISTINCT status, never "ok";
  2. the ready-age histogram sums to EXACTLY `ready` (no fractional-day item
     silently dropped, no undated item vanished) -- the reconciliation of the
     dashboard's "READY 104 / unclaimed 76" split;
  3. resolved throughput is real, with honest zeros and an honest `None` when a
     project records resolutions but no `closed_at`; and
  4. `last_activity` reflects the most recent timestamp of any kind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from amplifier_work_tracker import adapter as A


def _ready(id_: str, days_old: float | None) -> A.Item:
    """An open work-lane (= ready/unclaimed) item aged `days_old` days, or with
    no `created_at` at all when `days_old` is None."""
    created = None if days_old is None else datetime.now(UTC) - timedelta(days=days_old)
    return A.Item(id=id_, status="open", tags=[A.LANE_WORK], created_at=created)


def _dead_pid() -> int:
    """A pid guaranteed no longer running (spawn a trivial child, reap it)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---------------------------------------------------------- ready-age buckets


def test_ready_age_buckets_sum_equals_ready_including_fractional_days():
    """The load-bearing invariant: every ready item is counted exactly once,
    so the histogram total equals `ready`. The fractional-day ages (1.5, 3.5,
    6.5) are the exact items the previous inclusive `[lo, hi]` bounds dropped."""
    items = [
        _ready("a", 0.5),
        _ready("b", 1.5),  # was silently dropped (gap between 0-1 and 2-3)
        _ready("c", 2.5),
        _ready("d", 3.5),  # was silently dropped (gap between 2-3 and 4-6)
        _ready("e", 5.0),
        _ready("f", 6.5),  # was silently dropped (gap between 4-6 and 7+)
        _ready("g", 9.0),
    ]
    buckets = A._ready_age_buckets(items)  # noqa: SLF001 -- pinning a private invariant
    ready = sum(1 for i in items if i.status == "open" and A.LANE_WORK in i.tags)
    assert ready == 7
    assert sum(buckets.values()) == ready


def test_ready_age_buckets_bands_are_floor_day_and_contiguous():
    """Half-open floor-day bands tile the timeline: [0,2) [2,4) [4,7) [7,..)."""
    assert A._ready_age_bucket_label(0.0) == "0-1"  # noqa: SLF001
    assert A._ready_age_bucket_label(1.9) == "0-1"  # noqa: SLF001
    assert A._ready_age_bucket_label(2.0) == "2-3"  # noqa: SLF001
    assert A._ready_age_bucket_label(3.9) == "2-3"  # noqa: SLF001
    assert A._ready_age_bucket_label(4.0) == "4-6"  # noqa: SLF001
    assert A._ready_age_bucket_label(6.9) == "4-6"  # noqa: SLF001
    assert A._ready_age_bucket_label(7.0) == "7+"  # noqa: SLF001
    assert A._ready_age_bucket_label(100.0) == "7+"  # noqa: SLF001
    # A tiny negative age (clock skew) is absorbed by the first band, not lost.
    assert A._ready_age_bucket_label(-0.01) == "0-1"  # noqa: SLF001


def test_ready_age_buckets_undated_item_goes_to_unknown_not_dropped():
    items = [_ready("a", 1.0), _ready("b", None), _ready("c", None)]
    buckets = A._ready_age_buckets(items)  # noqa: SLF001
    assert buckets[A.UNKNOWN_READY_AGE] == 2
    assert sum(buckets.values()) == 3  # still equals `ready`


def test_ready_age_buckets_counts_only_open_work_lane_items():
    items = [
        _ready("ready", 1.0),
        A.Item(id="held", status="held", tags=[A.LANE_WORK], created_at=datetime.now(UTC)),
        A.Item(id="blocked", status="blocked", tags=[A.LANE_WORK], created_at=datetime.now(UTC)),
        A.Item(id="intake", status="open", tags=[A.LANE_INTAKE], created_at=datetime.now(UTC)),
        A.Item(id="resolved", status="resolved", tags=[A.LANE_WORK], created_at=datetime.now(UTC)),
    ]
    buckets = A._ready_age_buckets(items)  # noqa: SLF001
    assert sum(buckets.values()) == 1


def test_ready_derivations_share_only_items_without_active_blockers():
    available = _ready("available", 2.0)
    dependent = _ready("dependent", 30.0)
    blocked_ids = {"dependent"}
    ready = [i for i in [available, dependent] if i.id not in blocked_ids]

    activity = A.project_activity([available, dependent], ready_items=ready)
    buckets = A._ready_age_buckets([available, dependent], ready_items=ready)  # noqa: SLF001

    assert len(ready) == 1
    assert sum(buckets.values()) == len(ready)
    assert activity["oldest_unclaimed_age_seconds"] is not None
    assert activity["oldest_unclaimed_age_seconds"] < 3 * 86400


def test_active_blocked_id_aggregate_reuses_claim_predicate_and_keeps_unknown_active(monkeypatch):
    rows = [
        {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
        *[
            {
                "row_kind": "dependency",
                "item_id": "open",
                "dependency_type": "blocks",
                "status": "open",
            },
            {
                "row_kind": "dependency",
                "item_id": "held",
                "dependency_type": "blocks",
                "status": "in_progress",
            },
            {
                "row_kind": "dependency",
                "item_id": "blocked",
                "dependency_type": "blocks",
                "status": "blocked",
            },
            {
                "row_kind": "dependency",
                "item_id": "deferred",
                "dependency_type": "blocks",
                "status": "deferred",
            },
            {
                "row_kind": "dependency",
                "item_id": "unknown",
                "dependency_type": "blocks",
                "status": None,
            },
            {
                "row_kind": "dependency",
                "item_id": "resolved",
                "dependency_type": "blocks",
                "status": "closed",
            },
            {
                "row_kind": "dependency",
                "item_id": "related",
                "dependency_type": "related",
                "status": "open",
            },
            {
                "row_kind": "dependency",
                "item_id": "discovered",
                "dependency_type": "discovered-from",
                "status": "open",
            },
        ],
    ]
    monkeypatch.setattr(
        A,
        "_dolt_sql_json",
        lambda query: SimpleNamespace(returncode=0, stdout=json.dumps({"rows": rows})),
    )

    assert A._active_blocked_item_ids_via_sql("proj") == {  # noqa: SLF001
        "open",
        "held",
        "blocked",
        "deferred",
        "unknown",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rows": None},
        {"rows": {}},
        [],
        None,
        {"rows": []},
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                None,
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {"row_kind": "dependency", "dependency_type": "blocks", "status": "open"},
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {
                    "row_kind": "dependency",
                    "item_id": None,
                    "dependency_type": "blocks",
                    "status": "open",
                },
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {"row_kind": "dependency", "item_id": "x", "status": "open"},
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {
                    "row_kind": "dependency",
                    "item_id": "x",
                    "dependency_type": None,
                    "status": "open",
                },
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {"row_kind": "dependency", "item_id": "x", "dependency_type": "blocks"},
            ]
        },
        {
            "rows": [
                {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""},
                {
                    "row_kind": "dependency",
                    "item_id": "x",
                    "dependency_type": "blocks",
                    "status": 1,
                },
            ]
        },
        {"rows": [{"row_kind": "control", "item_id": "bad", "dependency_type": "", "status": ""}]},
        "not json",
    ],
)
def test_project_summary_keeps_readiness_unknown_for_malformed_dependency_payloads(
    payload, monkeypatch
):
    item = _ready("dependent", 30.0)
    monkeypatch.setattr(A, "_summary_items_via_sql", lambda _db: [item])
    monkeypatch.setattr(
        A,
        "_dolt_sql_json",
        lambda _query: SimpleNamespace(
            returncode=0,
            stdout=payload if isinstance(payload, str) else json.dumps(payload),
            stderr="",
        ),
    )
    ws = SimpleNamespace(creation_state=lambda _name: "ready")

    summary = A.project_summary(cast(A.Workspace, ws), "proj")

    assert summary.status == A.STATUS_OK
    assert summary.total == 1
    assert summary.intake == 0
    assert summary.ready is None
    assert summary.ready_age_buckets is None
    assert summary.oldest_unclaimed_age_seconds is None


def test_project_summary_accepts_a_valid_control_only_dependency_result(monkeypatch):
    item = _ready("unblocked", 2.0)
    monkeypatch.setattr(A, "_summary_items_via_sql", lambda _db: [item])
    monkeypatch.setattr(
        A,
        "_dolt_sql_json",
        lambda _query: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "rows": [
                        {"row_kind": "control", "item_id": "", "dependency_type": "", "status": ""}
                    ]
                }
            ),
            stderr="",
        ),
    )
    ws = SimpleNamespace(creation_state=lambda _name: "ready")

    summary = A.project_summary(cast(A.Workspace, ws), "proj")

    assert summary.ready == 1
    assert summary.ready_age_buckets is not None
    assert sum(summary.ready_age_buckets.values()) == 1
    assert summary.oldest_unclaimed_age_seconds is not None


# ------------------------------------------------------ resolved throughput


def _resolved(id_: str, closed_days_ago: float | None) -> A.Item:
    closed = (
        None if closed_days_ago is None else datetime.now(UTC) - timedelta(days=closed_days_ago)
    )
    return A.Item(id=id_, status="resolved", tags=[A.LANE_WORK], closed_at=closed)


def test_resolved_throughput_counts_within_windows():
    items = [
        _resolved("h1", 0.2),  # in 24h and 7d
        _resolved("h2", 2.0),  # in 7d only
        _resolved("h3", 10.0),  # in neither
    ]
    a = A.project_activity(items)
    assert a["resolved_24h"] == 1
    assert a["resolved_7d"] == 2


def test_resolved_throughput_honest_zero_when_no_resolutions():
    """No resolved items at all -> genuine 0 throughput, never None."""
    a = A.project_activity([_ready("a", 1.0)])
    assert a["resolved_24h"] == 0
    assert a["resolved_7d"] == 0


def test_resolved_throughput_honest_zero_when_all_resolutions_are_old():
    """Dated resolutions, none in the window -> a real, meaningful 0."""
    a = A.project_activity([_resolved("old", 30.0)])
    assert a["resolved_24h"] == 0
    assert a["resolved_7d"] == 0


def test_resolved_throughput_none_when_resolved_but_undated():
    """A project that HAS resolved items but records no `closed_at` on any of
    them cannot be measured -- report None, never a fabricated 0."""
    items = [_resolved("u1", None), _resolved("u2", None)]
    a = A.project_activity(items)
    assert a["resolved_24h"] is None
    assert a["resolved_7d"] is None


# ------------------------------------------------------------- last_activity


def test_last_activity_uses_most_recent_timestamp_of_any_kind():
    now = datetime.now(UTC)
    # This item carries ONLY a closed_at (no updated_at) -- it must still count.
    only_closed = A.Item(id="c", status="resolved", tags=[A.LANE_WORK], closed_at=now)
    older = A.Item(id="o", status="open", tags=[A.LANE_WORK], updated_at=now - timedelta(days=5))
    a = A.project_activity([only_closed, older])
    assert a["last_activity"] == now.isoformat()


def test_last_activity_none_when_no_timestamps_anywhere():
    a = A.project_activity([A.Item(id="x", status="open", tags=[A.LANE_WORK])])
    assert a["last_activity"] is None


# ---------------------------------------------- project_summary health status


def test_project_summary_creating_is_distinct_and_counts_none(tmp_path):
    ws = A.Workspace(tmp_path)
    d = ws.path("proj")
    d.mkdir(parents=True)
    (d / ".create.lock").write_text(str(os.getpid()))  # a LIVE pid -> "creating"
    s = A.project_summary(ws, "proj")
    assert s.status == A.STATUS_CREATING
    assert s.status != A.STATUS_OK
    assert s.total is None
    assert s.ready is None
    assert s.ready_age_buckets is None


def test_project_summary_broken_is_distinct_and_counts_none(tmp_path):
    ws = A.Workspace(tmp_path)
    d = ws.path("proj")
    d.mkdir(parents=True)
    (d / ".create.lock").write_text(str(_dead_pid()))  # a DEAD pid -> "abandoned"
    s = A.project_summary(ws, "proj")
    assert s.status == A.STATUS_BROKEN
    assert s.status != A.STATUS_OK
    assert s.total is None


# ------------------------------------------- dolt timestamp seam (SQL summary)
#
# `project_summary` now reads its items straight off the shared dolt server over
# a read-only SQL SELECT (`_summary_items_via_sql`) instead of `bd list --all`,
# to sidestep the serialization-retry-exhaustion that failed large projects
# (cortex: 465 items, 23s 8-retry ERROR). dolt renders `datetime` columns as a
# bare naive wall-clock (`YYYY-MM-DD HH:MM:SS`), NOT bd's ISO-8601 `...Z`. These
# pin that `_parse_dolt_timestamp` reconstructs the SAME aware-UTC instant
# `_parse_bd_timestamp` would have -- the invariant every aging/throughput field
# in the summary silently depends on. See work_tracker items pipeline-exz/knu.


def test_parse_dolt_timestamp_matches_the_bd_iso_parse_for_the_same_instant():
    """dolt's bare `2026-08-15 22:56:30` and bd's `2026-08-15T22:56:30Z` name
    the SAME instant -- both parsers must yield the identical aware-UTC datetime,
    or the summary's ages/throughput would silently shift by the local offset."""
    dolt = A._parse_dolt_timestamp("2026-08-15 22:56:30")  # noqa: SLF001
    bd = A._parse_bd_timestamp("2026-08-15T22:56:30Z")  # noqa: SLF001
    assert dolt == bd
    assert dolt is not None and dolt.tzinfo is not None  # aware, never naive


def test_parse_dolt_timestamp_is_aware_utc_so_summary_arithmetic_never_raises():
    """A naive datetime here would raise the moment `project_activity` subtracts
    it from an aware `now(UTC)` -- the exact failure this parser exists to
    prevent. The result must be aware and anchored to UTC."""
    ts = A._parse_dolt_timestamp("2026-08-15 22:56:30")  # noqa: SLF001
    assert ts is not None
    assert ts.utcoffset() == timedelta(0)
    # subtracting from an aware now must not raise (it did, with a naive parse)
    _ = (datetime.now(UTC) - ts).total_seconds()


def test_parse_dolt_timestamp_none_for_empty_null_and_garbage():
    """dolt renders a NULL `closed_at` as an empty CSV field -- that, and any
    unparseable value, must become `None` (a missing timestamp), never a
    fabricated instant. Same discipline as `_parse_bd_timestamp`."""
    for v in ("", "   ", None, 0, "not-a-date"):
        assert A._parse_dolt_timestamp(v) is None  # noqa: SLF001
