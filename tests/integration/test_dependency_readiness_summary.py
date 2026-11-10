"""Dependency-aware project readiness against the isolated Dolt fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from amplifier_work_tracker import adapter as A

pytestmark = pytest.mark.integration


def test_summary_readiness_accepts_real_project_with_no_dependencies(project_factory, workspace):
    name, bd = project_factory("rdy")
    bd.create("unblocked readiness item", tags=[A.LANE_WORK], priority=1)

    summary = A.project_summary(workspace, name)

    assert summary.status == A.STATUS_OK
    assert summary.total == 1
    assert summary.ready == 1
    assert summary.ready_age_buckets is not None
    assert sum(summary.ready_age_buckets.values()) == 1
    assert summary.oldest_unclaimed_age_seconds is not None


def test_summary_readiness_tracks_open_held_and_resolved_blockers(
    project_factory, workspace, unique_actor, monkeypatch
):
    name, bd = project_factory("rdy")
    blocker_id = bd.create("readiness blocker", tags=[A.LANE_WORK], priority=1)
    dependent_id = bd.create("readiness dependent", tags=[A.LANE_WORK], priority=1)
    dep = bd._run(["dep", blocker_id, "--blocks", dependent_id])  # noqa: SLF001
    assert dep.returncode == 0, dep.stderr
    # Keep the dependent's own status open so readiness is gated by the
    # dependency predicate, independent of Beads' optional blocked status.
    reopen = bd._run(["update", dependent_id, "--status", "open", "--assignee", ""])
    assert reopen.returncode == 0, reopen.stderr
    assert bd.get(dependent_id).status == "open"

    original_summary_items = A._summary_items_via_sql  # noqa: SLF001

    def items_with_old_dependent(db):
        items = original_summary_items(db)
        for item in items:
            if item.id == dependent_id:
                item.created_at = datetime.now(UTC) - timedelta(days=30)
        return items

    monkeypatch.setattr(A, "_summary_items_via_sql", items_with_old_dependent)

    before = A.project_summary(workspace, name)
    assert before.status == A.STATUS_OK
    assert before.ready == 1
    assert before.ready_age_buckets is not None
    assert sum(before.ready_age_buckets.values()) == before.ready
    assert before.oldest_unclaimed_age_seconds is not None
    assert before.oldest_unclaimed_age_seconds < 3 * 86400
    with pytest.raises(A.BeadsError, match="blocked"):
        bd.claim_item(dependent_id, actor=unique_actor)

    bd.claim_item(blocker_id, actor=unique_actor)
    held = A.project_summary(workspace, name)
    assert held.ready == 0
    assert held.ready_age_buckets is not None
    assert sum(held.ready_age_buckets.values()) == held.ready
    assert held.oldest_unclaimed_age_seconds is None
    unclaim = bd._run(  # noqa: SLF001 -- return this fixture hold using its owner
        ["update", blocker_id, "--status", "open", "--assignee", ""], actor=unique_actor
    )
    assert unclaim.returncode == 0, unclaim.stderr

    after_release = A.project_summary(workspace, name)
    assert after_release.ready == 1
    with pytest.raises(A.BeadsError, match="blocked"):
        bd.claim_item(dependent_id, actor=unique_actor)

    bd.resolve(blocker_id, "readiness fixture prerequisite resolved")
    resolved = A.project_summary(workspace, name)
    assert resolved.ready == 1
    assert resolved.ready_age_buckets is not None
    assert sum(resolved.ready_age_buckets.values()) == resolved.ready
    assert resolved.oldest_unclaimed_age_seconds is not None
    assert resolved.oldest_unclaimed_age_seconds > 20 * 86400
    claimed = bd.claim_item(dependent_id, actor=unique_actor)
    assert claimed.holder == unique_actor


def test_summary_readiness_is_unknown_when_dependency_read_fails(
    project_factory, workspace, monkeypatch
):
    name, bd = project_factory("rdy")
    bd.create("readiness dependency read failure", tags=[A.LANE_WORK], priority=1)

    def fail_dependency_read(_db):
        raise A.BeadsError("dependency fixture read failed")

    monkeypatch.setattr(A, "_active_blocked_item_ids_via_sql", fail_dependency_read)
    summary = A.project_summary(workspace, name)

    assert summary.status == A.STATUS_OK
    assert summary.total == 1
    assert summary.ready is None
    assert summary.ready_age_buckets is None
    assert summary.oldest_unclaimed_age_seconds is None
    assert summary.intake == 0
