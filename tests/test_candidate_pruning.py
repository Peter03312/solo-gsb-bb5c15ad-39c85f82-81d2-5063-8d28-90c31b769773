"""Differential tests: SQL/box pruning must return a lossless superset.

For every adjudication chain the database pruning may over-include rows,
but it must never drop a record the exact rules would report.  These tests
build a database with many records (lots of them time-disjoint and
frequency/area-disjoint), compute the answers with full-set loading and
with the pruned candidate loading, and assert the adjudicated results are
identical.
"""
from __future__ import annotations

import os
import random
import tempfile

_TMP = tempfile.mkdtemp(prefix="radio-prune-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/prune.db")
os.environ.setdefault("GUARD_BAND", "2.0")
os.environ.setdefault("SCHEDULED_APPROVAL_BACKGROUND", "0")

import pytest

from app import adjudication
from app.adjudication import Circle, License, RestrictionWindow
from app.db import SessionLocal, init_db
from app import main as api_main
from app.models import (
    Application,
    Base,
    RestrictionWindow as RestrictionWindowRow,
)
from app.db import engine

from tests.factories import make_payload


def _license(i, cx, cy, r, lo, hi, start, end):
    from datetime import datetime, timezone

    def dt(s):
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    return License(
        application_id=i,
        circle=Circle(cx, cy, r),
        freq_low=lo,
        freq_high=hi,
        start_time=dt(start),
        end_time=dt(end),
    )


BASE_START = "2026-10-01T08:00:00"


def _hour(h):
    return f"2026-10-01T{8 + h:02d}:00:00"


@pytest.fixture()
def session():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as s:
        yield s


def _seed(session, licenses, *, status="approved", windows=()):
    for lic in licenses:
        session.add(
            Application(
                id=lic.application_id,
                name=f"app-{lic.application_id}",
                cx=lic.circle.cx,
                cy=lic.circle.cy,
                radius=lic.circle.radius,
                freq_low=lic.freq_low,
                freq_high=lic.freq_high,
                start_time=lic.start_time,
                end_time=lic.end_time,
                status=status,
            )
        )
    for wid, win in enumerate(windows, start=1):
        session.add(
            RestrictionWindowRow(
                id=wid,
                revision=wid,
                reason="w",
                cx=win.circle.cx,
                cy=win.circle.cy,
                radius=win.circle.radius,
                freq_low=win.freq_low,
                freq_high=win.freq_high,
                start_time=win.start_time,
                end_time=win.end_time,
            )
        )
    session.flush()


def _random_licenses(n, seed):
    rng = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        h = rng.randrange(0, 20)  # many disjoint time windows across 28h
        duration = rng.choice([0, 1, 2])
        start = f"2026-10-{(1 + h // 24):02d}T{(h % 24):02d}:00:00"
        day = 1 + (h + duration) // 24
        end = f"2026-10-{day:02d}T{((h + duration) % 24):02d}:30:00"
        out.append(
            _license(
                i,
                cx=rng.uniform(-100, 100),
                cy=rng.uniform(-100, 100),
                r=rng.choice([1.0, 5.0, 20.0]),
                lo=rng.choice([100.0, 200.0, 300.0, 400.0])
                + rng.uniform(-0.5, 0.5),
                hi=rng.choice([110.0, 210.0, 310.0, 410.0])
                + rng.uniform(-0.5, 0.5),
                start=start + ("Z" if start.endswith("00") else ""),
                end=end + ("Z" if end.endswith("00") else ""),
            )
        )
    return out


def _to_naive_z(s):
    # The factory above builds UTC-aware values; good for direct License use.
    return s


@pytest.mark.parametrize("seed", range(8))
def test_pruned_matches_full_for_single_candidate(session, seed):
    licenses = _random_licenses(120, seed)
    _seed(session, licenses)
    guard = 2.0
    # Candidates chosen from the same random distribution (as if pending).
    candidates = _random_licenses(20, seed + 1000)
    for cand in candidates:
        full = adjudication.evaluate(cand, licenses, guard)
        pruned_rows = api_main.load_effective_candidates(
            session, api_main.box_for_license(cand, guard)
        )
        pruned = adjudication.evaluate(cand, pruned_rows, guard)
        assert [c.other.application_id for c in pruned] == [
            c.other.application_id for c in full
        ]


@pytest.mark.parametrize("seed", range(6))
def test_pruned_matches_full_for_windows(session, seed):
    rng = random.Random(seed + 500)
    licenses = _random_licenses(80, seed)
    _seed(session, licenses)
    windows = []
    for wid in range(1, 40):
        h = rng.randrange(0, 24)
        windows.append(
            RestrictionWindow(
                window_id=wid,
                circle=Circle(
                    rng.uniform(-100, 100), rng.uniform(-100, 100), 10.0
                ),
                freq_low=rng.choice([100.0, 200.0, 300.0]),
                freq_high=rng.choice([110.0, 210.0, 310.0]),
                start_time=_license(
                    0, 0, 0, 0, 0, 0,
                    f"2026-10-01T{h:02d}:00:00+00:00",
                    f"2026-10-01T{(h + 1) % 24:02d}:30:00+00:00",
                ).start_time,
                end_time=_license(
                    0, 0, 0, 0, 0, 0,
                    f"2026-10-01T{h:02d}:00:00+00:00",
                    f"2026-10-01T{(h + 1) % 24:02d}:30:00+00:00",
                ).end_time,
            )
        )
    _seed(session, [], windows=windows)
    for cand in _random_licenses(15, seed + 300):
        full = adjudication.evaluate_restriction_windows(cand, windows)
        pruned_rows = api_main.load_active_window_candidates(
            session, api_main.box_for_license(cand)
        )
        pruned = adjudication.evaluate_restriction_windows(
            cand, [api_main.to_window(r) for r in pruned_rows]
        )
        assert [h.window.window_id for h in pruned] == [
            h.window.window_id for h in full
        ]


@pytest.mark.parametrize("seed", range(4))
def test_group_union_box_is_lossless(session, seed):
    licenses = _random_licenses(100, seed)
    _seed(session, licenses)
    guard = 2.0
    group = _random_licenses(6, seed + 700)
    full_eff, full_inner = adjudication.evaluate_group(group, licenses, guard)
    union = api_main.union_box(
        [api_main.box_for_license(c, guard) for c in group]
    )
    pruned_effective = api_main.load_effective_candidates(session, union)
    pr_eff, pr_inner = adjudication.evaluate_group(
        group, pruned_effective, guard
    )
    for c in group:
        assert [x.other.application_id for x in pr_eff[c.application_id]] == [
            x.other.application_id for x in full_eff[c.application_id]
        ]
        assert [x.other.application_id for x in pr_inner[c.application_id]] == [
            x.other.application_id for x in full_inner[c.application_id]
        ]


def test_guard_band_change_still_checks_all_pairs(session):
    # Even fully disjoint (time/area) licenses are loaded for the pair
    # check: the guard-band change must examine the entire effective set.
    a = _license(1, 0, 0, 1, 100, 105, "2026-10-01T08:00:00+00:00",
                 "2026-10-01T09:00:00+00:00")
    b = _license(2, 5000, 5000, 1, 900, 905, "2030-01-01T00:00:00+00:00",
                 "2030-01-01T01:00:00+00:00")
    _seed(session, [a, b])
    loaded = api_main.load_effective_licenses(session)
    assert {l.application_id for l in loaded} == {1, 2}
    assert adjudication.find_interfering_pairs(loaded, 2.0) == []


def test_available_ranges_box_covers_full_candidate_interval(session):
    # A license only conflicts when the candidate lower bound sits far in
    # the swept interval; it must still be in the pruned candidate set.
    other = _license(
        1, 0, 0, 10, 500, 505,
        "2026-10-01T08:00:00+00:00", "2026-10-01T10:00:00+00:00",
    )
    _seed(session, [other])
    candidate = _license(
        -1, 0, 0, 10, 100, 102,
        "2026-10-01T08:00:00+00:00", "2026-10-01T10:00:00+00:00",
    )
    box = api_main.available_ranges_box(candidate, 2.0, 100.0, 600.0)
    pruned = api_main.load_effective_candidates(session, box)
    assert [l.application_id for l in pruned] == [1]
    # And the available ranges themselves agree with the full-set answer.
    full = adjudication.available_ranges(
        candidate, [other], 2.0, 100.0, 600.0
    )
    pr = adjudication.available_ranges(
        candidate, pruned, 2.0, 100.0, 600.0
    )
    assert [(r.min, r.max, r.min_inclusive, r.max_inclusive) for r in pr] == [
        (r.min, r.max, r.min_inclusive, r.max_inclusive) for r in full
    ]


def test_boundary_touching_records_are_not_dropped(session):
    # Exact half-open-time touch and closed-band touch cases must survive
    # the inclusive SQL prefilter.
    # Time: candidate starts exactly when another ends -> half-open touch,
    # not a conflict, but the row may be over-included (never dropped).
    lic = _license(
        1, 0, 0, 10, 100, 110,
        "2026-10-01T08:00:00+00:00", "2026-10-01T09:00:00+00:00",
    )
    # Touching circle at one point (distance == r1 + r2), closed bands
    # touching at 110.
    touch = _license(
        2, 0, 20, 10, 110, 120,
        "2026-10-01T08:30:00+00:00", "2026-10-01T09:30:00+00:00",
    )
    # Guard-band gap exactly == band is allowed (not a conflict), but the
    # row must still be pruned in (SQL uses >= / <=).
    near = _license(
        3, 0, 0, 10, 112, 120,
        "2026-10-01T08:30:00+00:00", "2026-10-01T09:30:00+00:00",
    )
    _seed(session, [lic, touch, near])
    candidate = _license(
        99, 0, 0, 10, 100, 110,
        "2026-10-01T08:30:00+00:00", "2026-10-01T09:30:00+00:00",
    )
    ids = {
        l.application_id
        for l in api_main.load_effective_candidates(
            session, api_main.box_for_license(candidate, 2.0)
        )
    }
    assert ids == {1, 2, 3}
    full = adjudication.evaluate(candidate, [lic, touch, near], 2.0)
    pruned_rows = api_main.load_effective_candidates(
        session, api_main.box_for_license(candidate, 2.0)
    )
    pruned = adjudication.evaluate(candidate, pruned_rows, 2.0)
    assert [c.other.application_id for c in pruned] == [
        c.other.application_id for c in full
    ]
    # Licence 2 genuinely conflicts via touching circle + touching band.
    assert 2 in [c.other.application_id for c in full]
