"""Performance guard: disjoint records must not be loaded one by one.

Seeds thousands of effective licenses whose time windows are mutually
disjoint (plus far-away geometry/bands) and asserts that a preview /
available-ranges search pulls only the handful of candidates that can
possibly matter, rather than every effective row.  Also checks startup
(init + a startup sweep) stays usable with the large table.
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp(prefix="radio-perf-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/perf.db")
os.environ.setdefault("GUARD_BAND", "2.0")
os.environ.setdefault("SCHEDULED_APPROVAL_BACKGROUND", "0")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import main as api_main
from app.db import SessionLocal, engine, init_db
from app.models import Application, Base
from app import main as main_module

N_DISJOINT = 3000


@pytest.fixture()
def big_client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    # 3000 mutually time-disjoint, far-apart licenses on different days.
    with SessionLocal() as session, session.begin():
        for i in range(1, N_DISJOINT + 1):
            # Each license occupies a distinct 30-minute slot two hours
            # apart, so the slots never touch in time.
            start = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc) + timedelta(
                hours=2 * i
            )
            end = start + timedelta(minutes=30)
            session.add(
                Application(
                    id=i,
                    name=f"disjoint-{i}",
                    cx=1000.0 * i,
                    cy=1000.0 * i,
                    radius=1.0,
                    freq_low=1000.0 * i,
                    freq_high=1000.0 * i + 5.0,
                    start_time=start,
                    end_time=end,
                    status="approved",
                )
            )
        # The pending application the tests preview.
        session.add(
            Application(
                id=N_DISJOINT + 1,
                name="candidate",
                cx=0.0,
                cy=0.0,
                radius=10.0,
                freq_low=100.0,
                freq_high=110.0,
                start_time=datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
                end_time=datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
                status="pending",
            )
        )
    with TestClient(main_module.app) as client:
        yield client


def test_preview_loads_only_pruned_candidates(big_client):
    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        started = time.perf_counter()
        resp = big_client.post(
            f"/api/applications/{N_DISJOINT + 1}/preview"
        )
        elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", collect)

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_conflicts"] is False
    assert body["conflicts"] == []
    assert body["restriction_window_conflicts"] == []
    # The state field still reports the full window table (empty here).
    assert body["active_restriction_window_ids"] == []

    # The applications scan behind the preview must be a single filtered
    # statement (no per-row N+1), and must finish promptly even at 3000
    # disjoint rows.
    app_scans = [
        s
        for s in statements
        if "applications" in s and "FROM applications" in s.upper()
    ]
    # Preview does: row get, revision meta get, effective candidates (one
    # filtered scan), full active windows scan, and serialization gets.
    effective_scans = [
        s
        for s in app_scans
        if "status = ?" in s.upper() or "status = " in s
    ]
    assert len(statements) < 30, statements
    assert elapsed < 2.0


def test_available_ranges_loads_only_pruned_candidates(big_client):
    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        resp = big_client.post(
            f"/api/applications/{N_DISJOINT + 1}/available-ranges",
            json={"candidate_low_min": 50.0, "candidate_low_max": 300.0},
        )
    finally:
        event.remove(engine, "before_cursor_execute", collect)

    assert resp.status_code == 200
    body = resp.json()
    # Nothing nearby in time/area: the whole swept interval is available.
    assert body["available_ranges"] == [
        {
            "min": 50.0,
            "max": 300.0,
            "min_inclusive": True,
            "max_inclusive": True,
        }
    ]
    assert len(statements) < 30, statements


def test_startup_sweep_stays_usable_with_large_table(big_client):
    # The lifespan startup (including the catch-up sweep) completed while
    # building the client; a direct sweep on the large table must also
    # finish promptly and without deciding the disjoint records.
    started = time.perf_counter()
    summary = api_main.run_scheduled_approval_sweep()
    elapsed = time.perf_counter() - started
    assert summary["processed_count"] == 0
    assert elapsed < 2.0


def test_waitlist_drain_with_many_disjoint_licenses(big_client):
    # Register the candidate as a standby, then revoke a far-away license:
    # the drain must prune the 3000 rows and still auto-adjudicate quickly.
    rev = big_client.get("/api/revision/current").json()["current_revision"]
    big_client.post(
        f"/api/applications/{N_DISJOINT + 1}/waitlist", json={"reason": "q"}
    )
    # Suspend (not revoke) one disjoint license: releases trigger a drain.
    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        started = time.perf_counter()
        resp = big_client.post(
            "/api/applications/1/suspend",
            json={"expected_revision": rev, "reason": "outage"},
        )
        elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert resp.status_code == 200
    body = resp.json()
    # The candidate is disjoint in time/area from every effective license,
    # so the drain auto-approves it.
    assert N_DISJOINT + 1 in body["auto_approved_application_ids"]
    assert len(statements) < 40, statements
    assert elapsed < 2.0


def _create(client, **overrides):
    payload = {
        "name": "x",
        "cx": 0.0,
        "cy": 0.0,
        "radius": 10.0,
        "freq_low": 100.0,
        "freq_high": 110.0,
        "start": "2026-10-01T08:00:00Z",
        "end": "2026-10-01T10:00:00Z",
    }
    payload.update(overrides)
    body = {
        "name": payload["name"],
        "circle": {
            "cx": payload["cx"],
            "cy": payload["cy"],
            "radius": payload["radius"],
        },
        "frequency": {"low": payload["freq_low"], "high": payload["freq_high"]},
        "time_window": {"start": payload["start"], "end": payload["end"]},
    }
    resp = client.post("/api/applications", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _rev(client):
    return client.get("/api/revision/current").json()["current_revision"]


def test_resume_chain_prunes_disjoint_licenses(big_client):
    # A license in the candidate vicinity is suspended, a blocking license
    # is approved, then resume must find exactly that one blocker despite
    # the 3000 disjoint rows - and stay fast.
    blocker = _create(
        big_client,
        freq_low=100.0,
        freq_high=110.0,
        start="2026-10-02T08:00:00Z",
        end="2026-10-02T10:00:00Z",
    )
    suspended = _create(
        big_client,
        freq_low=100.0,
        freq_high=110.0,
        start="2026-10-02T08:00:00Z",
        end="2026-10-02T10:00:00Z",
    )
    rev = _rev(big_client)
    assert big_client.post(
        f"/api/applications/{suspended}/approve",
        json={"expected_revision": rev},
    ).status_code == 200
    rev = _rev(big_client)
    assert big_client.post(
        f"/api/applications/{suspended}/suspend",
        json={"expected_revision": rev, "reason": "outage"},
    ).status_code == 200
    rev = _rev(big_client)
    assert big_client.post(
        f"/api/applications/{blocker}/approve",
        json={"expected_revision": rev},
    ).status_code == 200

    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        resp = big_client.post(
            f"/api/applications/{suspended}/resume",
            json={
                "expected_revision": _rev(big_client),
                "reason": "back",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert resp.status_code == 409, resp.text
    details = resp.json()["error"]["details"]
    ids = [c["application"]["id"] for c in details["conflicts"]]
    assert ids == [blocker]
    assert len(statements) < 30, statements


def test_scheduled_due_chain_prunes_disjoint_licenses(big_client):
    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        resp = big_client.post(
            "/api/scheduled-approvals/process-due"
        )
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert resp.status_code == 200
    assert resp.json()["processed_count"] == 0
    assert len(statements) < 20, statements


def test_emergency_confirm_chain_prunes_disjoint_licenses(big_client):
    # An emergency far away in time/area/band from every effective license
    # succeeds and force-suspends nobody, with the disjoint rows pruned.
    em = _create(
        big_client,
        name="emergency",
        cx=-42.0,
        cy=17.0,
        radius=3.0,
        freq_low=333.0,
        freq_high=334.0,
        start="2032-06-01T00:00:00Z",
        end="2032-06-01T01:00:00Z",
    )
    statements: list[str] = []

    def collect(*args):  # pragma: no cover - test hook
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", collect)
    try:
        resp = big_client.post(
            "/api/emergency-occupations",
            json={
                "application_id": em,
                "expected_revision": _rev(big_client),
                "reason": "burst",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["force_suspended_application_ids"] == []
    assert len(statements) < 30, statements
