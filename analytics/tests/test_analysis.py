from datetime import date

import numpy as np
import pytest

from src import analysis
from src.forecasting import moving_average


def days_from(n, start=date(2026, 1, 1)):
    first = analysis.day_number(start)
    return np.arange(first, first + n)


# ------------------------------------------------------------------ depletion

def test_depletion_days_to_critical():
    # reserves 20 kg falling 0.5 kg/day, critical 5 kg -> (20 - 5) / 0.5 = 30 days
    days = days_from(40)
    reserves = 40.0 - 0.5 * np.arange(40)
    dep = analysis.analyse_depletion(days, reserves, 5.0)
    assert dep["status"] == "depleting"
    assert dep["consumption_kg_day"] == pytest.approx(0.5)
    assert dep["reserves_kg"] == pytest.approx(20.5)
    assert dep["days_to_critical"] == pytest.approx((20.5 - 5.0) / 0.5)
    assert dep["r2"] == pytest.approx(1.0)


def test_depletion_stable_or_growing_has_no_date():
    days = days_from(40)
    assert analysis.analyse_depletion(days, np.full(40, 12.0), 5.0)["status"] == "stable"
    growing = analysis.analyse_depletion(days, 12.0 + 0.1 * np.arange(40), 5.0)
    assert growing["status"] == "stable" and growing["days_to_critical"] is None


def test_depletion_already_under_the_threshold_is_now():
    days = days_from(40)
    dep = analysis.analyse_depletion(days, 4.0 - 0.05 * np.arange(40), 5.0)
    assert dep["status"] == "now" and dep["days_to_critical"] == 0.0


def test_depletion_uses_only_the_last_30_days():
    days = days_from(60)
    reserves = np.concatenate([np.full(30, 30.0), 30.0 - 0.4 * np.arange(1, 31)])
    dep = analysis.analyse_depletion(days, reserves, 5.0)
    assert dep["consumption_kg_day"] == pytest.approx(0.4)


def test_projection_ends_30_days_after_the_critical_date_and_is_capped():
    days = days_from(40)
    dep = analysis.analyse_depletion(days, 40.0 - 0.5 * np.arange(40), 5.0)
    points = analysis.projection(dep)
    assert len(points) == 31 + 30  # ceil(30.99) + 30
    assert points[0][0] == dep["last_day"] + 1
    assert points[0][1] == pytest.approx(dep["reserves_kg"] - 0.5)
    slow = analysis.analyse_depletion(days, 100.0 - 0.1 * np.arange(40), 5.0)
    assert len(analysis.projection(slow)) == analysis.PROJECTION_MAX_DAYS


# -------------------------------------------------------------------- harvest

def build_season():
    """Hand-made year: winter decline, strong flow, weak tail, harvest of 10 kg on day 120."""
    gains = np.concatenate([
        np.full(40, -0.05),    # winter
        np.full(30, 0.6),      # nectar flow ...
        np.full(30, 0.1),      # ... fading
        np.full(20, 0.0),      # flow over: gain < 0.05 for long
        [-6.0, -4.0],          # harvest over two days (the daily mean spreads it)
        np.full(30, -0.05),
    ])
    weights = 40.0 + np.concatenate([[0.0], np.cumsum(gains)])
    return days_from(len(weights)), weights, gains


def test_find_harvest_sums_the_consecutive_days_of_the_drop():
    days, weights, gains = build_season()
    index, kg = analysis.find_harvest(weights)
    assert index == 40 + 30 + 30 + 20 + 1  # +1: weights has one more point than gains
    assert kg == pytest.approx(10.0)


def test_optimal_harvest_is_after_the_flow_peak_and_before_the_real_harvest():
    days, weights, _ = build_season()
    result = analysis.analyse_harvest(days, weights, tare_kg=24.0, winter_reserves_kg=15.0)
    assert result["status"] == "ok"
    assert result["optimal_day"] < result["actual_day"]
    assert result["delta_days"] == result["optimal_day"] - result["actual_day"] < 0
    assert result["actual_kg"] == pytest.approx(10.0)
    index = int(np.searchsorted(days, result["optimal_day"]))
    gain = analysis.smoothed_gain(weights)
    # after the strong flow (gains of +0.6 up to index 70) and with 3 quiet days from there
    assert index > 70
    assert (gain[index:index + 3] < analysis.FLOW_END_GAIN_KG_DAY).all()
    expected = max(0.0, weights[index] - 24.0 - 15.0)
    assert result["harvestable_kg"] == pytest.approx(expected)


def test_harvestable_kg_is_never_negative():
    days, weights, _ = build_season()
    result = analysis.analyse_harvest(days, weights, tare_kg=24.0, winter_reserves_kg=200.0)
    assert result["harvestable_kg"] == 0.0


def test_season_in_progress_is_reported_instead_of_inventing_a_date():
    gains = np.concatenate([np.full(40, -0.05), np.full(30, 0.6)])  # flow never ends, no harvest
    weights = 40.0 + np.concatenate([[0.0], np.cumsum(gains)])
    result = analysis.analyse_harvest(days_from(len(weights)), weights, 24.0, 15.0)
    assert result["status"] == "in_progress"
    assert result["optimal_day"] is None and result["actual_day"] is None


def test_model_does_not_see_the_harvest_drop():
    days, weights, _ = build_season()
    cut = analysis.find_harvest(weights)[0]
    with_drop = analysis.analyse_harvest(days, weights, 24.0, 15.0)["optimal_day"]
    without = analysis.analyse_harvest(days[:cut], weights[:cut], 24.0, 15.0)["optimal_day"]
    assert with_drop == without


# ------------------------------------------------------------------- backtest

def test_backtest_origins_cover_nov_1_to_feb_1_weekly():
    origins = analysis.backtest_origins(date(2026, 10, 14))
    assert origins[0] == date(2025, 11, 1) and origins[-1] <= date(2026, 2, 1)
    assert all((b - a).days == 7 for a, b in zip(origins, origins[1:]))
    # in February 2026 the winter 2025-26 is not fully known yet (+90 days): the one before is used
    assert analysis.backtest_origins(date(2026, 2, 10))[0] == date(2024, 11, 1)


def test_backtest_linear_is_exact_on_a_straight_line_and_naive_is_biased():
    days = days_from(400, start=date(2025, 10, 1))
    reserves = 30.0 - 0.05 * np.arange(400)  # a perfectly linear decline
    smoothed = moving_average(reserves, analysis.SMOOTHING_DAYS)
    results = analysis.backtest(days, smoothed, analysis.backtest_origins(date(2026, 10, 14)))
    for h in analysis.BACKTEST_HORIZONS:
        assert results[("linear", h)]["mae"] == pytest.approx(0.0, abs=1e-9)
        # naive keeps the origin value, so it is too optimistic by 0.05 * h kg
        assert results[("naive", h)]["bias"] == pytest.approx(0.05 * h)
        assert results[("naive", h)]["mae"] == pytest.approx(0.05 * h)


def test_backtest_linear_is_optimistic_when_consumption_accelerates():
    days = days_from(400, start=date(2025, 10, 1))
    slow, fast = np.arange(400), np.maximum(np.arange(400) - 150, 0)
    reserves = 30.0 - 0.02 * slow - 0.1 * fast  # consumption speeds up after day 150
    smoothed = moving_average(reserves, analysis.SMOOTHING_DAYS)
    results = analysis.backtest(days, smoothed, analysis.backtest_origins(date(2026, 10, 14)))
    assert results[("linear", 90)]["bias"] > 0


# ---------------------------------------------------- feeding events and statuses

def test_depletion_fit_skips_a_feeding_event_inside_the_window():
    gains = np.full(70, -0.05)
    gains[40:44] = 0.5  # autumn feeding: +2 kg over four days
    weights = 30.0 + np.concatenate([[0.0], np.cumsum(gains)])[:70]
    days = days_from(70)
    smoothed = moving_average(weights - 22.0, analysis.SMOOTHING_DAYS)
    naive_fit = analysis.analyse_depletion(days, smoothed, 5.0)
    with_events = analysis.analyse_depletion(days, smoothed, 5.0, analysis.daily_gain(weights))
    assert with_events["consumption_kg_day"] == pytest.approx(0.05, abs=1e-6)
    assert with_events["fit_days"] < 30
    assert naive_fit["consumption_kg_day"] < with_events["consumption_kg_day"]


def test_depletion_fit_keeps_the_whole_window_if_too_few_days_follow_the_event():
    gains = np.full(60, -0.05)
    gains[50:54] = 0.5  # the event is too recent: fewer than 14 clean days remain
    weights = 30.0 + np.concatenate([[0.0], np.cumsum(gains)])[:60]
    smoothed = moving_average(weights - 22.0, analysis.SMOOTHING_DAYS)
    dep = analysis.analyse_depletion(days_from(60), smoothed, 5.0, analysis.daily_gain(weights))
    assert dep["fit_days"] == analysis.REGRESSION_DAYS


def test_harvest_status_when_the_flow_is_still_on_at_the_real_harvest():
    gains = np.concatenate([np.full(40, 0.6), [-6.0, -4.0], np.full(10, -0.05)])
    weights = 40.0 + np.concatenate([[0.0], np.cumsum(gains)])
    result = analysis.analyse_harvest(days_from(len(weights)), weights, 24.0, 15.0)
    assert result["status"] == "flow_active_at_harvest"
    assert result["optimal_day"] is None and result["actual_kg"] == pytest.approx(10.0)


def test_harvest_status_without_any_nectar_flow():
    weights = 40.0 - 0.05 * np.arange(120)
    assert analysis.analyse_harvest(days_from(120), weights, 24.0, 15.0)["status"] == "no_flow"
