from spotify_heart.hr_smoother import HrSmoother
from spotify_heart.serial_reader import parse_line


def feed(sm, values, t0=0.0, dt=0.5):
    t = t0
    for v in values:
        sm.push(v, t)
        t += dt
    return t - dt


def test_rejects_out_of_range():
    sm = HrSmoother()
    assert not sm.push(39.9, 0)
    assert not sm.push(180.1, 0)
    assert sm.push(40, 0)
    assert sm.push(180, 0)
    assert sm.estimate(0).n == 2


def test_median_beats_outlier():
    sm = HrSmoother()
    t = feed(sm, [72, 71, 73, 72, 150, 72, 71])
    est = sm.estimate(t)
    assert est.bpm == 72
    assert est.locked


def test_not_locked_under_min_readings():
    sm = HrSmoother(min_readings=5)
    t = feed(sm, [72, 72, 72, 72])
    est = sm.estimate(t)
    assert est.n == 4
    assert not est.locked
    sm.push(72, t + 0.5)
    assert sm.estimate(t + 0.5).locked


def test_not_locked_when_spread_too_wide():
    sm = HrSmoother(max_spread=8)
    t = feed(sm, [60, 90, 60, 90, 60, 90, 60, 90])
    est = sm.estimate(t)
    assert est.n == 8
    assert est.spread > 8
    assert not est.locked


def test_unlocks_after_window_empties():
    sm = HrSmoother(window_s=30)
    t = feed(sm, [72] * 10)
    assert sm.estimate(t).locked
    est = sm.estimate(t + 31)
    assert not est.locked
    assert est.n == 0
    assert est.age_s == float("inf")


def test_age_reports_time_since_last_reading():
    sm = HrSmoother()
    t = feed(sm, [72] * 6)
    assert sm.estimate(t + 4).age_s == 4


def test_parse_line():
    assert parse_line("heart_rate: 90.00\r\n") == 90.0
    assert parse_line("heart_rate:89") == 89.0
    assert parse_line("breath_rate: 12.00") is None
    assert parse_line("garbage") is None
    assert parse_line("heart_rate: .") is None
