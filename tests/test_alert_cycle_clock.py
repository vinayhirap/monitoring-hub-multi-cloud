# tests/test_alert_cycle_clock.py
"""2026-09-30 -- P1 alerts (RDS/ELB, evaluated every 2 minutes) could never auto-resolve.

The evaluator advances healthy_streak / breach_cycles only when >= 240 s have passed "since
the row was last touched", so that an extra 2-minute evaluation is not counted as an extra
5-minute cycle. "Last touched" was last_seen_at -- which EVERY evaluation overwrites, including
the ones that did not advance the counter -- so with evaluations 120 s apart the gap was never
240 s and the counter never moved. Prod: ELB "Target 5xx Errors 0 / 5" CRITICAL, ACTIVE for hours.

These tests run the evaluator's ACTUAL SQL gate expressions in SQLite (IF/DATE_SUB/UTC_TIMESTAMP
translated) against a simulated timeline, and compare with a plain-Python reference of the
intended rule: one counted cycle per >= 240 s, measured from the last COUNTED cycle."""
import re
import sqlite3
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402
import app.aws.metric_catalog_data  # noqa: F401,E402
import app.collector.polling_model  # noqa: F401,E402
from tests.test_baseline_and_dynamic_bounds import _load_alert_evaluator  # noqa: E402

GAP = 240


def _sqlite(clock):
    conn = sqlite3.connect(":memory:")
    conn.create_function("UTC_TIMESTAMP", 0, lambda: clock["t"])
    conn.create_function("IF_", 3, lambda c, a, b: a if c else b)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, triggered_at INT, last_seen_at INT, "
                 "cycle_at INT, healthy_streak INT, current_value REAL)")
    conn.execute("CREATE TABLE alert_pending (id INTEGER PRIMARY KEY, first_breach_at INT, last_seen_at INT, "
                 "cycle_at INT, breach_cycles INT)")
    return conn


def _tr(sql):
    """MySQL -> SQLite for the few constructs the gate uses."""
    sql = re.sub(r"DATE_SUB\(UTC_TIMESTAMP\(\), INTERVAL (\d+) SECOND\)", r"(UTC_TIMESTAMP() - \1)", sql)
    return re.sub(r"\bIF\(", "IF_(", sql)


def _healthy_update(ev):
    # same assignment order as alert_evaluator._evaluate_row's healthy branch
    return _tr(f"""UPDATE alerts SET current_value = 0,
                       healthy_streak = {ev._GATED_STREAK_SQL},
                       cycle_at = {ev._GATED_CYCLE_AT_SQL},
                       last_seen_at = UTC_TIMESTAMP() WHERE id = 1""")


def _reference(times, first_cycle_at=0):
    """The intended rule: count one cycle per >= GAP seconds since the last COUNTED cycle."""
    streak, cycle_at, out = 0, first_cycle_at, []
    for t in times:
        if t - cycle_at >= GAP:
            streak, cycle_at = streak + 1, t
        out.append(streak)
    return out


def _run_healthy(times, triggered_at=0, cycle_at=0):
    ev = _load_alert_evaluator()
    clock = {"t": triggered_at}
    conn = _sqlite(clock)
    conn.execute("INSERT INTO alerts VALUES (1, ?, ?, ?, 0, 9)", (triggered_at, triggered_at, cycle_at))
    sql, got = _healthy_update(ev), []
    for t in times:
        clock["t"] = t
        conn.execute(sql)
        got.append(conn.execute("SELECT healthy_streak FROM alerts WHERE id = 1").fetchone()[0])
    return got


def test_p1_alert_resolves_when_evaluated_every_two_minutes():
    times = list(range(120, 1201, 120))                       # the 2-minute critical tick
    got = _run_healthy(times)
    assert got[0] == 0 and got[1] == 1, got                   # t=120 gated, t=240 counted
    assert got == _reference(times)
    # a 1-cycle rule (evaluation_period 5) is therefore satisfied by t=240 -> the alert resolves
    assert next(t for t, g in zip(times, got) if g >= 1) == 240


def test_the_old_last_seen_at_gate_really_livelocked_so_this_test_is_not_vacuous():
    ev = _load_alert_evaluator()
    clock = {"t": 0}
    conn = _sqlite(clock)
    conn.execute("INSERT INTO alerts VALUES (1, 0, 0, 0, 0, 9)")
    old = _tr("UPDATE alerts SET current_value = 0, "
              "healthy_streak = IF(COALESCE(last_seen_at, triggered_at) <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 240 SECOND), "
              "healthy_streak + 1, healthy_streak), last_seen_at = UTC_TIMESTAMP() WHERE id = 1")
    for t in range(120, 120 * 500, 120):                      # 16 hours of healthy 2-minute evaluations
        clock["t"] = t
        conn.execute(old)
    assert conn.execute("SELECT healthy_streak FROM alerts").fetchone()[0] == 0   # never resolves: the bug


def test_mixed_two_and_five_minute_ticks_still_count_one_cycle_per_240s():
    times = sorted(set(range(120, 3601, 120)) | set(range(300, 3601, 300)))
    got = _run_healthy(times)
    assert got == _reference(times)
    # an extra evaluation is never an extra cycle: never more than +1 within 240 s
    for i, t in enumerate(times):
        window = [g for tt, g in zip(times, got) if t <= tt < t + GAP]
        assert max(window) - got[i] <= 1 if window else True


def test_multi_cycle_rule_needs_one_counted_cycle_per_240s():
    times = list(range(120, 2401, 120))
    got = _run_healthy(times)
    assert next(t for t, g in zip(times, got) if g >= 3) == 720      # counted at 240, 480, 720


def test_breach_restarts_the_clock_so_recovery_needs_a_full_cycle():
    ev = _load_alert_evaluator()
    clock = {"t": 0}
    conn = _sqlite(clock)
    conn.execute("INSERT INTO alerts VALUES (1, 0, 0, 0, 5, 9)")
    clock["t"] = 1000                                          # breach evaluation (evaluator's update_fields)
    conn.execute("UPDATE alerts SET last_seen_at = UTC_TIMESTAMP(), healthy_streak = 0, cycle_at = UTC_TIMESTAMP() WHERE id = 1")
    sql, got = _healthy_update(ev), []
    for t in (1120, 1240):
        clock["t"] = t
        conn.execute(sql)
        got.append(conn.execute("SELECT healthy_streak FROM alerts").fetchone()[0])
    assert got == [0, 1]


def test_alert_stuck_before_the_migration_resolves_on_its_next_healthy_evaluation():
    # cycle_at is NULL for rows that existed before migration 074 -> falls back to triggered_at
    got = _run_healthy([100000], triggered_at=1000, cycle_at=None)
    assert got == [1]


def test_pending_breach_counter_advances_on_a_two_minute_tick():
    ev = _load_alert_evaluator()
    clock = {"t": 0}
    conn = _sqlite(clock)
    conn.execute("INSERT INTO alert_pending VALUES (1, 0, 0, 0, 1)")          # first breach at t=0, 1 cycle
    sql = _tr(f"""UPDATE alert_pending SET
                    breach_cycles = IF({ev._PENDING_CYCLE_DUE_SQL}, breach_cycles + 1, breach_cycles),
                    cycle_at = IF({ev._PENDING_CYCLE_DUE_SQL}, UTC_TIMESTAMP(), cycle_at),
                    last_seen_at = UTC_TIMESTAMP() WHERE id = 1""")
    seen = {}
    for t in range(120, 1201, 120):
        clock["t"] = t
        conn.execute(sql)
        seen[t] = conn.execute("SELECT breach_cycles FROM alert_pending").fetchone()[0]
    assert seen[120] == 1 and seen[240] == 2 and seen[480] == 3        # a 3-cycle rule now fires


def test_gate_constants_never_read_last_seen_at_and_setters_are_ordered():
    ev = _load_alert_evaluator()
    for expr in (ev._CYCLE_DUE_SQL, ev._GATED_STREAK_SQL, ev._GATED_CYCLE_AT_SQL, ev._PENDING_CYCLE_DUE_SQL):
        assert "last_seen_at" not in expr
    src = open("app/collector/alert_evaluator.py").read()
    for chunk in src.split("healthy_streak = {_GATED_STREAK_SQL}")[1:]:
        assert chunk.index("cycle_at") < chunk.index("last_seen_at")
    # a breach and a brand-new alert both start the clock
    assert '"cycle_at = UTC_TIMESTAMP()"' in src
    assert "triggered_at, last_seen_at, cycle_at," in src
