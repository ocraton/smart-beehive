"""Synthetic year of hourly telemetry for an apiary in central Italy.

The generator is organised on the four components of a time series (lesson 10):

  trend_component      slow, non-oscillating development of each colony (forager
                       population, wax drift), scaled by the hive profile
  seasonal_component   calendar-driven patterns: annual temperature, daylight, nectar
                       flow, brood season, baseline consumption
  cyclical_component   non-periodic multi-day weather episodes (Markov chain of good /
                       bad days, autocorrelated temperature anomaly)
  irregular_component  noise and one-off events (honey harvest, autumn feeding)

Determinism: every random draw is keyed by (seed, kind, absolute date/hour, hive), never
drawn sequentially from the start of the series. The same date therefore always gets the
same value, whatever the run time or BACKFILL_DAYS are.
"""

import math
import random
from datetime import date, datetime, timedelta, timezone

SENSORS = ("brood_temp", "humidity", "weight", "flights", "ext_temp", "ext_hum")

# The weather chain always starts here, so a given date has one weather in every run.
WEATHER_EPOCH = date(2020, 1, 1)
# Picks one of the many equally valid weather realizations for a given seed.
WEATHER_VARIANT = 22

# Central Italy (~12.5 E): solar noon is 0.83 h earlier than 12:00 UTC.
SOLAR_OFFSET_H = 0.83
SOLAR_NOON_UTC = 12.0 - SOLAR_OFFSET_H

TEMP_PEAK_DOY = 203  # end of July: annual maximum (minimum is mid January)
BAD_DAY_RAIN_PROB = 0.65
Q_REF = 6.0  # weather-activity hours that make a "good" foraging day
FORAGER_DIP_KG = 0.35  # weight of the foragers out of the hive at peak activity
WEIGHT_NOISE_KG = 0.02

# (day of year, value) key points, linearly interpolated
NECTAR_KG_DAY = (
    (1, 0.0), (60, 0.0), (79, 0.15), (100, 0.35), (120, 0.6), (130, 1.5), (145, 1.4),
    (161, 1.2), (181, 0.8), (196, 0.3), (212, 0.05), (243, 0.02), (258, 0.05),
    (278, 0.12), (304, 0.02), (319, 0.0), (366, 0.0),
)
FORAGER_FRACTION = (
    (1, 0.02), (45, 0.05), (75, 0.25), (105, 0.6), (130, 1.0), (170, 1.0), (200, 0.7),
    (230, 0.5), (260, 0.4), (290, 0.25), (310, 0.1), (335, 0.03), (366, 0.02),
)
CONSUMPTION_KG_DAY = (
    (1, 0.055), (45, 0.065), (60, 0.12), (90, 0.14), (120, 0.16), (150, 0.20),
    (190, 0.20), (215, 0.12), (250, 0.09), (280, 0.075), (305, 0.06), (366, 0.055),
)


def _interp(x, points):
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _smoothstep(x):
    x = _clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _doy(d):
    return min(d.timetuple().tm_yday, 365)


def _season_index(doy):
    """1 at the annual temperature maximum, 0 at the minimum."""
    return (1 + math.cos(2 * math.pi * (doy - TEMP_PEAK_DOY) / 365.0)) / 2


class Keyed:
    """Deterministic random draws keyed by what they describe, not by call order."""

    def __init__(self, seed):
        self.seed = seed

    def _rng(self, key):
        return random.Random("|".join(str(k) for k in (self.seed,) + key))

    def gauss(self, *key):
        return self._rng(key).gauss(0.0, 1.0)

    def uniform(self, *key):
        return self._rng(key).random()


# ---------------------------------------------------------------- seasonal component

def seasonal_component(doy):
    """Everything that repeats with the calendar, independent of the weather of the day."""
    s = _season_index(doy)
    day_len = 12.15 + 3.15 * math.cos(2 * math.pi * (doy - 172) / 365.0)
    brood = _smoothstep((doy - 25) / 20.0) * (1 - _smoothstep((doy - 295) / 30.0))
    return {
        "temp_mean": 14.0 + 9.0 * (2 * s - 1),
        "temp_amp": 5.5 + 2.0 * (2 * s - 1),
        "winter_index": 1 - s,
        "day_len": day_len,
        "nectar_kg_day": _interp(doy, NECTAR_KG_DAY),
        "consumption_kg_day": _interp(doy, CONSUMPTION_KG_DAY),
        "brood_presence": brood,
    }


def daylight(doy, hour_utc):
    """Bell-shaped sunlight, 0 at night, 1 at solar noon."""
    day_len = seasonal_component(doy)["day_len"]
    x = (hour_utc - (SOLAR_NOON_UTC - day_len / 2)) / day_len
    return math.sin(math.pi * x) if 0 < x < 1 else 0.0


# -------------------------------------------------------------------- trend component

def trend_component(doy, profile):
    """Slow development of the colony: foragers ramp up to the June peak, then decline."""
    return {
        "strength": profile["strength"],
        "forager_fraction": _interp(doy, FORAGER_FRACTION),
        # Comb built over the summer: a small permanent weight gain
        "comb_kg_day": 0.004 * profile["strength"] if 120 <= doy <= 230 else 0.0,
    }


# ----------------------------------------------------------------- cyclical component

class WeatherChain:
    """Markov chain of good/bad days with an autocorrelated temperature anomaly.

    Bad episodes last longer in winter and are rarer in summer, so the cycle is not
    periodic: that is what separates it from the seasonal component.
    """

    def __init__(self, keyed):
        self.keyed = keyed
        self._days = [(False, 0.0)]  # state of WEATHER_EPOCH - 1 day, discarded

    def state(self, d):
        index = (d - WEATHER_EPOCH).days
        while len(self._days) <= index + 1:
            i = len(self._days) - 1
            day = WEATHER_EPOCH + timedelta(days=i)
            s = _season_index(_doy(day))
            bad_prev, anomaly_prev = self._days[-1]
            p_good_to_bad = 0.28 - 0.16 * s
            p_bad_to_good = 0.30 + 0.25 * s
            u = self.keyed.uniform("markov", WEATHER_VARIANT, i)
            bad = (u < p_good_to_bad) if not bad_prev else (u >= p_bad_to_good)
            anomaly = 0.5 * anomaly_prev + 0.9 * self.keyed.gauss("anomaly", WEATHER_VARIANT, i)
            self._days.append((bad, anomaly))
        return self._days[index + 1]


def cyclical_component(chain, d):
    bad, anomaly = chain.state(d)
    return {"bad": bad, "temp_anomaly": anomaly}


# ---------------------------------------------------------------- irregular component

def irregular_component(profile, d, hour):
    """One-off events (honey harvest, autumn feeding), as kg added (+) or removed (-).

    The other irregular part, the measurement noise, comes from Keyed draws in _hive_day().
    """
    delta = 0.0
    if f"{d.month:02d}-{d.day:02d}" == profile["harvest_day"] and hour in (8, 9, 10):
        delta -= profile["honey_harvest_kg"] * {8: 0.3, 9: 0.4, 10: 0.3}[hour]
    if d.month == 9 and 15 <= d.day <= 18 and hour in (17, 18):
        delta += profile["autumn_feeding_kg"] / 4 * 0.5
    return delta


# ------------------------------------------------------------------------- weather

def _temp_factor(t):
    return _clamp((t - 12.0) / 8.0, 0.0, 1.0) * (1 - _clamp((t - 30.0) / 8.0, 0.0, 0.6))


def _hum_factor(rh):
    return max(0.2, 1 - (rh - 55.0) / 60.0)


def day_weather(keyed, chain, d):
    """24 hourly weather samples (shared by every hive of the apiary) for the date d."""
    doy = _doy(d)
    seasonal = seasonal_component(doy)
    cyc = cyclical_component(chain, d)
    bad = cyc["bad"]
    t_mean = seasonal["temp_mean"] + cyc["temp_anomaly"] + (-2.2 if bad else 0.5)
    amp = seasonal["temp_amp"] * (0.6 if bad else 1.0)
    ordinal = d.toordinal()
    hours = []
    for h in range(24):
        solar_h = h + SOLAR_OFFSET_H
        t = t_mean + amp * math.cos(2 * math.pi * (solar_h - 14.5) / 24.0)
        t += 0.5 * keyed.gauss("t", ordinal, h)
        rh = 78.0 - 1.6 * (t - 10.0) + (14.0 if bad else 0.0) + 3.0 * keyed.gauss("rh", ordinal, h)
        rh = _clamp(rh, 30.0, 95.0)
        rain = bad and keyed.uniform("rain", ordinal, h) < BAD_DAY_RAIN_PROB
        sky = 0.0 if rain else (0.4 if bad else 1.0)
        activity = daylight(doy, h) * _temp_factor(t) * _hum_factor(rh) * sky
        hours.append({"t": t, "rh": rh, "bad": bad, "activity": activity})
    quality = _clamp(sum(x["activity"] for x in hours) / Q_REF, 0.0, 1.0)
    return {"hours": hours, "quality": quality, "t_mean": t_mean}


# ------------------------------------------------------------------------- series

def _hive_day(keyed, hive_id, profile, d, wx):
    """One day of one hive: per-hour values for every sensor, plus the raw weight level."""
    doy = _doy(d)
    ordinal = d.toordinal()
    seasonal = seasonal_component(doy)
    trend = trend_component(doy, profile)
    strength = trend["strength"]
    acts = [x["activity"] for x in wx["hours"]]

    gain = (
        seasonal["nectar_kg_day"] * strength * profile["flow_scale"] * wx["quality"] ** 2
        * (1 + 0.12 * keyed.gauss("gain", hive_id, ordinal))
    )
    gain = max(gain, 0.0)
    # Nectar comes home about two hours after the foragers left
    shifted = [acts[h - 2] if h >= 2 else 0.0 for h in range(24)]
    shifted_total = sum(shifted)
    cold = 1 + 0.02 * max(0.0, 8.0 - wx["t_mean"])
    consumption = seasonal["consumption_kg_day"] * profile["consumption_scale"] * cold

    out = []
    for h in range(24):
        w = wx["hours"][h]
        increment = -consumption / 24 + trend["comb_kg_day"] / 24
        if shifted_total > 0:
            increment += gain * shifted[h] / shifted_total
        increment += irregular_component(profile, d, h)

        dip = FORAGER_DIP_KG * strength * trend["forager_fraction"] * w["activity"]

        t_ext = w["t"] + 0.15 * keyed.gauss("ext_t", hive_id, ordinal, h)
        rh_ext = _clamp(w["rh"] + 0.8 * keyed.gauss("ext_rh", hive_id, ordinal, h), 30.0, 95.0)

        flights = 0.0
        if w["activity"] > 0:
            flights = 100.0 * w["activity"] * trend["forager_fraction"] * strength
            flights *= math.exp(0.15 * keyed.gauss("flights", hive_id, ordinal, h))
            flights = float(round(_clamp(flights, 0.0, 110.0)))

        b = seasonal["brood_presence"]
        noise_scale = 1 + 0.5 * (1 - strength)
        solar_h = h + SOLAR_OFFSET_H
        core = (
            34.5 + 0.1 * math.sin(2 * math.pi * (solar_h - 6) / 24.0)
            + 0.25 * noise_scale * keyed.gauss("brood", hive_id, ordinal, h)
        )
        cluster = 22.0 + 0.5 * (w["t"] - 5.0) + 1.8 * keyed.gauss("cluster", hive_id, ordinal, h)
        brood_temp = _clamp(b * core + (1 - b) * cluster, 14.0, 36.0)

        humidity = (
            62.0 + 4.0 * seasonal["winter_index"] + 0.10 * (w["rh"] - 65.0)
            + (1.5 if w["bad"] else 0.0) + 0.9 * keyed.gauss("hum", hive_id, ordinal, h)
        )
        humidity = _clamp(humidity, 55.0, 72.0)

        out.append({
            "increment": increment,
            "dip": dip,
            "noise": WEIGHT_NOISE_KG * keyed.gauss("w", hive_id, ordinal, h),
            "brood_temp": brood_temp,
            "humidity": humidity,
            "flights": flights,
            "ext_temp": t_ext,
            "ext_hum": rh_ext,
        })
    return out


def resolve_profile(profiles, hive_id):
    """Profile of a hive: the "default" one, overridden by its own entry if it has one."""
    return {**profiles["default"], **profiles["hives"].get(hive_id, {})}


def initial_weight(profile):
    """Weight the live simulator starts from: empty hive plus bees, plus current reserves."""
    return profile["tare_kg"] + profile["reserves_kg"]


def generate(profiles, hive_ids, first_hour, last_hour, seed):
    """Hourly series for every hive: {hive_id: {sensor: [(epoch_s, value), ...]}}.

    The weight is returned UNANCHORED (arbitrary constant offset): see anchor_weight().
    """
    keyed = Keyed(seed)
    chain = WeatherChain(keyed)
    resolved = {hive_id: resolve_profile(profiles, hive_id) for hive_id in hive_ids}
    result = {h: {sensor: [] for sensor in SENSORS} for h in hive_ids}
    levels = {h: 0.0 for h in hive_ids}

    d = first_hour.date()
    while d <= last_hour.date():
        wx = day_weather(keyed, chain, d)  # one weather for the whole apiary
        for hive_id in hive_ids:
            series = result[hive_id]
            for h, x in enumerate(_hive_day(keyed, hive_id, resolved[hive_id], d, wx)):
                ts = datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)
                if ts < first_hour or ts > last_hour:
                    continue
                levels[hive_id] += x["increment"]
                epoch = int(ts.timestamp())
                series["weight"].append((epoch, levels[hive_id] - x["dip"] + x["noise"]))
                for sensor in ("brood_temp", "humidity", "flights", "ext_temp", "ext_hum"):
                    series[sensor].append((epoch, x[sensor]))
        d += timedelta(days=1)
    return result


def anchor_weight(points, anchor_kg):
    """Translate the weight curve by a constant so that its last value equals anchor_kg."""
    shift = anchor_kg - points[-1][1]
    return [(t, v + shift) for t, v in points]
