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

# --- Seasonal method
SEASONAL_HORIZON_DAYS = 240
SEASONAL_K_MIN = 0.5             # the scale factor of the consumption is kept in [0.5, 2.0]
SEASONAL_K_MAX = 2.0
SEASONAL_MIN_PREV_CONSUMPTION = 0.005   # kg/day: under this the previous year did not consume
SHRINK_WEIGHT = 0.5              # shrunk variant: k = 1 + 0.5 * (estimated k - 1)
# Variants of the seasonal method: they only differ in the k applied to the consumption.
#   k       the estimated k (ratio of two small, noisy autumn consumptions), kept in [0.5, 2]
#   k1      k = 1: "like last year"
#   shrunk  the estimate pulled half-way towards 1
SEASONAL_VARIANTS = ("k", "k1", "shrunk")
SELECTION_HORIZONS = (60, 90)    # the variant is chosen on the MAE at these horizons ...
SELECTION_TIE_KG = 0.1           # ... and if two differ by less than this, the more prudent wins
# Variant used by the service, chosen once with `choose_variant` on the backtest of all the
# origins (1 Oct - 1 Feb of the last complete winter, 18 weekly origins, both hives, mean of
# the MAE and bias at +60 and +90 days). Result on the data of 15/10/2026:
#   k       MAE 0.72 kg, bias -0.56 kg   (too pessimistic in October: -2.2 / -3.3 kg at +60/+90 d)
#   shrunk  MAE 0.39 kg, bias -0.26 kg
#   k1      MAE 0.05 kg, bias +0.03 kg   <- lowest MAE by far (no tie within 0.1 kg)
# On synthetic data, where every year has the same consumption, k = 1 is the ideal case:
# see the note in CLAUDE.md before reading the numbers as a property of real hives.
SEASONAL_VARIANT = "k1"

# --- Analysis B: harvest timing
GAIN_ALPHA = 0.3                 # exponential smoothing of the daily net gain
HARVEST_DROP_KG = 3.0            # a daily drop bigger than this is a real harvest
HARVEST_EXTENT_KG = 0.5          # the harvest spreads over the consecutive days dropping more than this
FLOW_GAIN_KG_DAY = 0.15          # smoothed gain above this: nectar flow ...
FLOW_MIN_DAYS = 7                # ... if it lasts at least this many days
FLOW_END_GAIN_KG_DAY = 0.05      # the flow is over when the smoothed gain stays below this ...
FLOW_END_DAYS = 3                # ... for this many consecutive days

# --- Backtest
BACKTEST_METHODS = ("naive", "linear") + tuple("seasonal_" + v for v in SEASONAL_VARIANTS)
BACKTEST_HORIZONS = (30, 60, 90)
BACKTEST_MIN_POINTS = 14         # smoothed days needed at an origin to fit the line
BACKTEST_WEEK = 7
BACKTEST_PERIODS = ("all", "october", "nov_feb")   # origins of October / of November-February

SEASON_DAYS = 365                # the harvest analysis looks at the last season only

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


# ------------------------------------------------------------------ Seasonal method

def previous_year_day(day):
    """The same calendar day of the previous year (as a day number).

    The 29th of February has no counterpart: it is mapped to the 28th of February, so the
    previous-year profile simply does not change that day (a documented simplification).
    """
    d = EPOCH + timedelta(days=int(day))
    try:
        previous = d.replace(year=d.year - 1)
    except ValueError:
        previous = date(d.year - 1, 2, 28)
    return day_number(previous)


def seasonal_profile(index_of, smoothed, last_day, horizon=SEASONAL_HORIZON_DAYS):
    """Daily changes of the smoothed reserves of the previous year, from today's calendar day.

    Element k-1 is how much the reserves changed, a year ago, between the day that
    corresponds to today+k-1 and the one that corresponds to today+k. None if the previous
    year does not cover all those days.
    """
    values = []
    for k in range(horizon + 1):
        i = index_of.get(previous_year_day(last_day + k))
        if i is None or not np.isfinite(smoothed[i]):
            return None
        values.append(float(smoothed[i]))
    return np.diff(values)


def seasonal_scale(days, smoothed, gain, index_of):
    """Scale factor k: consumption seen now / consumption seen a year ago on the same days.

    "Now" is the window of the line (the last 30 days, starting after a feeding event);
    "a year ago" is the same calendar days of the previous year. In both cases the
    consumption is minus the slope of a least-squares line. k is kept in [0.5, 2.0].
    Returns (k, warning) or None if the previous year does not cover the window; the warning
    is True when the previous year did not consume in that window (then k = 1).
    """
    fit = _fit_recent(days, smoothed, gain)
    if fit is None:
        return None
    slope, _, _, last_day, window = fit
    previous_x, previous_y = [], []
    for day in range(last_day - window + 1, last_day + 1):
        previous = previous_year_day(day)
        i = index_of.get(previous)
        if i is not None and np.isfinite(smoothed[i]):
            previous_x.append(previous)
            previous_y.append(float(smoothed[i]))
    if len(previous_x) < FIT_MIN_DAYS:
        return None
    previous_slope, _, _ = linear_regression(previous_x, previous_y)
    previous_consumption = -previous_slope
    if previous_consumption < SEASONAL_MIN_PREV_CONSUMPTION:
        return 1.0, True
    return float(min(SEASONAL_K_MAX, max(SEASONAL_K_MIN, -slope / previous_consumption))), False


def variant_k(estimated, variant):
    """The k applied to the consumption by a variant of the seasonal method."""
    if variant == "k":
        return estimated
    if variant == "k1":
        return 1.0
    if variant == "shrunk":
        return 1.0 + SHRINK_WEIGHT * (estimated - 1.0)
    raise ValueError(f"unknown seasonal variant: {variant}")


def analyse_seasonal(days, smoothed, gain, critical_kg, horizon=SEASONAL_HORIZON_DAYS, variant=None):
    """Previsione stagionale delle riserve (spiegata in modo semplice).

    L'idea: l'inverno si ripete. Un anno fa, a partire da oggi sul calendario, le riserve
    hanno fatto un certo percorso: scendevano d'inverno, risalivano con il nettare di
    primavera. Si prende quel percorso (giorno per giorno, sulle riserve già lisciate)
    e lo si applica alle riserve di oggi, con una correzione sola: se quest'anno l'arnia
    consuma più o meno di un anno fa negli stessi giorni, i consumi vengono moltiplicati
    per k = consumo di adesso / consumo dell'anno scorso (tenuto fra 0,5 e 2). Le entrate
    (nettare, nutrizione) restano quelle dell'anno scorso, senza correzione.

    Risultato: la data in cui le riserve scendono sotto la soglia critica (se succede), il
    minimo previsto con la sua data, e k. Se l'anno precedente non copre i giorni che
    servono il metodo non è disponibile (status "no_previous_year"), senza errori.

    `variant` (default SEASONAL_VARIANT) decides which k is applied: the estimated one, 1, or
    the estimate pulled half-way towards 1 (see SEASONAL_VARIANTS).

    Returns a dict: status ("depleting" | "no_depletion" | "now" | "no_previous_year"),
    reserves_kg (now), scale_k (the one used), estimated_k, variant, k_warning, days_to_critical, min_reserves_kg, min_day,
    last_day and projection [(day, reserves_kg), ...] over `horizon` days.
    """
    days = np.asarray(days)
    smoothed = np.asarray(smoothed, dtype=float)
    finite = np.nonzero(np.isfinite(smoothed))[0]
    if len(finite) == 0:
        return {"status": "no_previous_year"}
    index_of = {int(d): i for i, d in enumerate(days)}
    last_day = int(days[finite[-1]])
    now = float(smoothed[finite[-1]])
    profile = seasonal_profile(index_of, smoothed, last_day, horizon)
    scale = seasonal_scale(days, smoothed, gain, index_of)
    if profile is None or scale is None:
        return {"status": "no_previous_year"}
    variant = variant or SEASONAL_VARIANT
    estimated, warning = scale
    k = variant_k(estimated, variant)
    steps = np.where(profile < 0, profile * k, profile)   # only the consumption is scaled
    path = now + np.cumsum(steps)
    below = np.nonzero(path < critical_kg)[0]
    low = int(np.argmin(path))
    result = {
        "status": "depleting" if len(below) else "no_depletion", "reserves_kg": now, "scale_k": k,
        "estimated_k": estimated, "variant": variant, "k_warning": warning, "days_to_critical": None, "min_reserves_kg": float(path[low]),
        "min_day": last_day + low + 1, "last_day": last_day,
        "projection": [(last_day + j + 1, float(v)) for j, v in enumerate(path)],
    }
    if now < critical_kg:
        result.update(status="now", days_to_critical=0.0)
    elif len(below):
        result["days_to_critical"] = float(below[0] + 1)
    return result


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
    """Weekly origins from Oct 1 to Feb 1 of the most recent winter fully in the past.

    "Fully" means that the longest horizon from the last origin is already known.
    """
    for year in (today.year, today.year - 1, today.year - 2):
        if date(year + 1, 2, 1) + timedelta(days=max(BACKTEST_HORIZONS)) <= today:
            origins = []
            d = date(year, 10, 1)
            while d <= date(year + 1, 2, 1):
                origins.append(d)
                d += timedelta(days=BACKTEST_WEEK)
            return origins
    return []


def in_period(origin, period):
    """Whether a backtest origin belongs to the period: "all", "october" or "nov_feb"."""
    if period == "all":
        return True
    return (origin.month == 10) == (period == "october")


def backtest(days, smoothed, origins, gain=None):
    """Forecast the smoothed reserves at +30/+60/+90 days from every origin, then compare.

    At each origin only the data up to that day is used. Methods:
      naive             the reserves stay what they are at the origin (baseline);
      linear            the line of analysis A, extended: fitted reserves + slope * horizon;
      seasonal_<variant> the seasonal method (profile of the year before the origin) with
                        the k of each variant (not available without a previous year).
    Error = predicted - actual (bias > 0: the forecast is optimistic, more reserves than
    there really were). Returns {period: {(method, horizon): {"mae", "bias", "n"}}} for the
    periods "all" origins, "october" origins and "nov_feb" origins.
    """
    days = np.asarray(days)
    smoothed = np.asarray(smoothed, dtype=float)
    gain = None if gain is None else np.asarray(gain, dtype=float)
    index_of = {int(d): i for i, d in enumerate(days)}
    errors = []    # (origin, method, horizon, error)
    for origin in origins:
        day = day_number(origin)
        i = index_of.get(day)
        if i is None:
            continue
        history = smoothed[:i + 1]
        if int(np.isfinite(history).sum()) < BACKTEST_MIN_POINTS:
            continue
        origin_gain = None if gain is None else gain[:i + 1]
        slope, estimate, _, _, _ = _fit_recent(days[:i + 1], history, origin_gain)
        naive = float(history[np.isfinite(history)][-1])
        seasonal = {
            v: analyse_seasonal(days[:i + 1], history, origin_gain, critical_kg=0.0,
                                horizon=max(BACKTEST_HORIZONS), variant=v)
            for v in SEASONAL_VARIANTS
        }
        for h in BACKTEST_HORIZONS:
            j = index_of.get(day + h)
            if j is None or not np.isfinite(smoothed[j]):
                continue
            errors.append((origin, "naive", h, naive - smoothed[j]))
            errors.append((origin, "linear", h, estimate + slope * h - smoothed[j]))
            for v, result in seasonal.items():
                if result["status"] != "no_previous_year":
                    errors.append((origin, "seasonal_" + v, h, result["projection"][h - 1][1] - smoothed[j]))
    out = {}
    for period in BACKTEST_PERIODS:
        grouped = {}
        for origin, method, h, e in errors:
            if in_period(origin, period):
                grouped.setdefault((method, h), []).append(e)
        out[period] = {
            key: {"mae": float(np.mean(np.abs(e))), "bias": float(np.mean(e)), "n": len(e)}
            for key, e in grouped.items()
        }
    return out


def choose_variant(results_by_hive):
    """The seasonal variant with the lowest MAE at +60 and +90 days over all the origins.

    `results_by_hive` is a list of the "all" results of `backtest`, one per hive. The score
    of a variant is the mean of its MAE (and of its bias) at SELECTION_HORIZONS over the hives.
    Variants whose MAE is within SELECTION_TIE_KG of the best one are tied, and the tie goes
    to the lowest bias: for the stores, overestimating is more dangerous than underestimating
    (an optimistic forecast delays the feeding of a colony that is running out of food).
    Returns (variant, scores {variant: {"mae", "bias"}}); (None, {}) without data.
    """
    scores = {}
    for v in SEASONAL_VARIANTS:
        cells = [r[("seasonal_" + v, h)] for r in results_by_hive for h in SELECTION_HORIZONS
                 if ("seasonal_" + v, h) in r]
        if cells:
            scores[v] = {"mae": float(np.mean([c["mae"] for c in cells])),
                         "bias": float(np.mean([c["bias"] for c in cells]))}
    if not scores:
        return None, {}
    best = min(s["mae"] for s in scores.values())
    tied = [v for v, s in scores.items() if s["mae"] - best < SELECTION_TIE_KG]
    return min(tied, key=lambda v: (scores[v]["bias"], scores[v]["mae"])), scores
