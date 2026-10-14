"""Analyses on the daily weight of a hive: stores depletion, harvest timing, backtest.

Pure functions on numpy arrays. `days` are integers: days since 1970-01-01 (UTC).
"""

from datetime import date, timedelta

import numpy as np

from .forecasting import exponential_smoothing, linear_regression, moving_average

# --- Analysis A: depletion of the stores
SMOOTHING_DAYS = 7               # moving average of the daily reserves
REGRESSION_DAYS = 30             # the line is fitted on the last 30 days of smoothed reserves
FEEDING_GAIN_KG_DAY = 0.3        # a day gaining more than this is a feeding / nectar event, not consumption
FIT_MIN_DAYS = 14                # days needed after such an event to fit the line
STABLE_SLOPE_KG_DAY = -0.01      # a slope above this is "stable or growing": no depletion date
PROJECTION_EXTRA_DAYS = 30       # the projection goes on this long after the critical date
PROJECTION_MAX_DAYS = 240
PROJECTION_NO_DEPLETION_DAYS = 90

# --- Analysis B: harvest timing
GAIN_ALPHA = 0.3                 # exponential smoothing of the daily net gain
HARVEST_DROP_KG = 3.0            # a daily drop bigger than this is a real harvest
HARVEST_EXTENT_KG = 0.5          # the harvest spreads over the consecutive days dropping more than this
FLOW_GAIN_KG_DAY = 0.15          # smoothed gain above this: nectar flow ...
FLOW_MIN_DAYS = 7                # ... if it lasts at least this many days
FLOW_END_GAIN_KG_DAY = 0.05      # the flow is over when the smoothed gain stays below this ...
FLOW_END_DAYS = 3                # ... for this many consecutive days

# --- Backtest
BACKTEST_HORIZONS = (30, 60, 90)
BACKTEST_MIN_POINTS = 14         # smoothed days needed at an origin to fit the line
BACKTEST_WEEK = 7

EPOCH = date(1970, 1, 1)


def day_number(d):
    return (d - EPOCH).days


def smooth_reserves(reserves):
    return moving_average(reserves, SMOOTHING_DAYS)


def daily_gain(weights):
    """Daily net gain (kg/day) of the daily mean weight; index 0 has no previous day (NaN)."""
    gain = np.full(len(weights), np.nan)
    gain[1:] = np.diff(np.asarray(weights, dtype=float))
    return gain


def _fit_recent(days, smoothed, gain=None):
    """Least-squares line on the last REGRESSION_DAYS finite smoothed values.

    If `gain` (the raw daily gain) shows a feeding or nectar event inside that window, the
    fit starts once the event has left the moving average (event day + 6), provided at
    least FIT_MIN_DAYS remain: the reserves are then followed while they only fall.

    Returns (slope kg/day, fitted reserves on the last day, R^2, last day, days used), or None.
    """
    days = np.asarray(days, dtype=float)
    smoothed = np.asarray(smoothed, dtype=float)
    finite = np.isfinite(smoothed)
    x, y = days[finite][-REGRESSION_DAYS:], smoothed[finite][-REGRESSION_DAYS:]
    if gain is not None:
        g = np.asarray(gain, dtype=float)[finite][-REGRESSION_DAYS:]
        events = np.nonzero(g > FEEDING_GAIN_KG_DAY)[0]
        if len(events):
            start = int(events[-1]) + SMOOTHING_DAYS - 1
            if len(x) - start >= FIT_MIN_DAYS:
                x, y = x[start:], y[start:]
    if len(x) < 2:
        return None
    slope, intercept, r2 = linear_regression(x, y)
    return slope, intercept + slope * x[-1], r2, int(x[-1]), len(x)


# ----------------------------------------------------------------------- Analysis A

def analyse_depletion(days, smoothed, critical_kg, gain=None):
    """When do the reserves reach the critical threshold, if the last 30 days continue.

    status: "now" (already under the threshold), "stable" (slope above -0.01 kg/day:
    no date), "depleting" (days = (reserves - threshold) / consumption).
    """
    fit = _fit_recent(days, smoothed, gain)
    if fit is None:
        return None
    slope, estimate, r2, last_day, fit_days = fit
    consumption = -slope
    result = {
        "reserves_kg": estimate, "consumption_kg_day": consumption, "r2": r2,
        "slope": slope, "last_day": last_day, "fit_days": fit_days, "days_to_critical": None,
    }
    if estimate < critical_kg:
        result.update(status="now", days_to_critical=0.0)
    elif slope > STABLE_SLOPE_KG_DAY:
        result.update(status="stable")
    else:
        result.update(status="depleting", days_to_critical=(estimate - critical_kg) / consumption)
    return result


def projection(depletion):
    """Daily linear projection of the reserves after the last day: [(day, reserves_kg), ...].

    It runs up to the critical date plus 30 days (at most 240 days); without a date, 90 days.
    """
    if depletion["days_to_critical"] is None:
        horizon = PROJECTION_NO_DEPLETION_DAYS
    else:
        horizon = min(PROJECTION_MAX_DAYS, int(np.ceil(depletion["days_to_critical"])) + PROJECTION_EXTRA_DAYS)
    return [
        (depletion["last_day"] + k, depletion["reserves_kg"] + depletion["slope"] * k)
        for k in range(1, horizon + 1)
    ]


# ----------------------------------------------------------------------- Analysis B

def find_harvest(weights):
    """The real harvest: index of its first day and kg removed, or None.

    A harvest is a run of consecutive days dropping more than HARVEST_EXTENT_KG, with at
    least one day dropping more than HARVEST_DROP_KG (the daily mean spreads the removal
    over two days). With several, the biggest one.
    """
    gain = np.full(len(weights), np.nan)
    gain[1:] = np.diff(weights)
    best = None
    i = 1
    while i < len(weights):
        if gain[i] < -HARVEST_EXTENT_KG:
            j = i
            while j < len(weights) and gain[j] < -HARVEST_EXTENT_KG:
                j += 1
            run = gain[i:j]
            if run.min() < -HARVEST_DROP_KG and (best is None or -run.sum() > best[1]):
                best = (i, float(-run.sum()))
            i = j
        else:
            i += 1
    return best


def smoothed_gain(weights):
    """Exponentially smoothed daily net gain (kg/day); index 0 has no previous day (NaN).

    The smoothing only uses past days, so a slice of it never "sees" later data.
    """
    out = np.full(len(weights), np.nan)
    if len(weights) > 1:
        out[1:] = exponential_smoothing(np.diff(weights), GAIN_ALPHA)
    return out


def find_optimal_harvest(gain, end_index):
    """Index of the optimal harvest day among the days before `end_index`, and why not.

    The nectar flow is a run of at least 7 days with smoothed gain above +0.15 kg/day
    (the strongest run is used). The optimal day is the first day after its peak from
    which the smoothed gain stays below +0.05 kg/day for 3 consecutive days.
    """
    g = gain[:end_index]
    runs = []
    i = 1
    while i < len(g):
        if g[i] > FLOW_GAIN_KG_DAY:
            j = i
            while j < len(g) and g[j] > FLOW_GAIN_KG_DAY:
                j += 1
            if j - i >= FLOW_MIN_DAYS:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return None, "no_flow"
    start, stop = max(runs, key=lambda r: g[r[0]:r[1]].max())
    peak = start + int(np.argmax(g[start:stop]))
    for k in range(peak + 1, len(g) - FLOW_END_DAYS + 1):
        if np.all(g[k:k + FLOW_END_DAYS] < FLOW_END_GAIN_KG_DAY):
            return k, "found"
    return None, "flow_not_ended"


def analyse_harvest(days, weights, tare_kg, winter_reserves_kg):
    """Optimal harvest moment of the past season against the real harvest.

    Only the days BEFORE the real harvest are used to find the optimal one, so the model
    does not see the drop. If there is no real harvest and the flow is not over, the
    season is reported as in progress instead of inventing a date.
    """
    weights = np.asarray(weights, dtype=float)
    harvest = find_harvest(weights)
    end = harvest[0] if harvest else len(weights)
    optimal, reason = find_optimal_harvest(smoothed_gain(weights), end)

    result = {"status": "ok", "optimal_day": None, "harvestable_kg": None,
              "actual_day": None, "actual_kg": None, "delta_days": None}
    if optimal is not None:
        result["optimal_day"] = int(days[optimal])
        result["harvestable_kg"] = max(0.0, float(weights[optimal]) - tare_kg - winter_reserves_kg)
    if harvest:
        result["actual_day"] = int(days[harvest[0]])
        result["actual_kg"] = harvest[1]
    if optimal is None:
        # With a real harvest the flow was still on the day before it: no optimal moment seen
        if reason == "flow_not_ended":
            result["status"] = "flow_active_at_harvest" if harvest else "in_progress"
        else:
            result["status"] = reason
    elif not harvest:
        result["status"] = "no_harvest_yet"
    if optimal is not None and harvest:
        result["delta_days"] = result["optimal_day"] - result["actual_day"]
    return result


# ------------------------------------------------------------------------ Backtest

def backtest_origins(today):
    """Weekly origins from Nov 1 to Feb 1 of the most recent winter fully in the past.

    "Fully" means that the longest horizon from the last origin is already known.
    """
    for year in (today.year, today.year - 1, today.year - 2):
        if date(year + 1, 2, 1) + timedelta(days=max(BACKTEST_HORIZONS)) <= today:
            origins = []
            d = date(year, 11, 1)
            while d <= date(year + 1, 2, 1):
                origins.append(d)
                d += timedelta(days=BACKTEST_WEEK)
            return origins
    return []


def backtest(days, smoothed, origins, gain=None):
    """Forecast the smoothed reserves at +30/+60/+90 days from every origin, then compare.

    At each origin only the data up to that day is used. Methods:
      naive  the reserves stay what they are at the origin (baseline);
      linear the line of analysis A, extended: fitted reserves + slope * horizon.
    Error = predicted - actual; returns {(method, horizon): {"mae", "bias", "n"}}
    (bias > 0 means the forecast is optimistic: more reserves than there really were).
    """
    days = np.asarray(days)
    smoothed = np.asarray(smoothed, dtype=float)
    index_of = {int(d): i for i, d in enumerate(days)}
    errors = {(m, h): [] for m in ("naive", "linear") for h in BACKTEST_HORIZONS}
    for origin in origins:
        day = day_number(origin)
        i = index_of.get(day)
        if i is None:
            continue
        history = smoothed[:i + 1]
        if int(np.isfinite(history).sum()) < BACKTEST_MIN_POINTS:
            continue
        slope, estimate, _, _, _ = _fit_recent(
            days[:i + 1], history, None if gain is None else np.asarray(gain)[:i + 1])
        naive = float(history[np.isfinite(history)][-1])
        for h in BACKTEST_HORIZONS:
            j = index_of.get(day + h)
            if j is None or not np.isfinite(smoothed[j]):
                continue
            errors[("naive", h)].append(naive - smoothed[j])
            errors[("linear", h)].append(estimate + slope * h - smoothed[j])
    return {
        key: {"mae": float(np.mean(np.abs(e))), "bias": float(np.mean(e)), "n": len(e)}
        for key, e in errors.items() if e
    }
