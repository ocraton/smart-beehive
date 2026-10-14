"""Analytics: batch processing read from InfluxDB -> aggregate -> forecast -> write to InfluxDB.

A full analysis runs at startup and then every ANALYTICS_INTERVAL_SECONDS.
"""

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import analysis
from .config import collect_retained_configs
from .store import Store, line

logging.basicConfig(level=logging.INFO, format="%(asctime)s - analytics - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DATA_DAYS = 365
CONFIG_WAIT_SECONDS = 5
# At startup the retained config may still be the old one: Node-RED republishes it when the
# hives come online. The first run waits for it, at most CONFIG_RETRIES x CONFIG_RETRY_SECONDS.
CONFIG_RETRIES = 6
CONFIG_RETRY_SECONDS = 10


def fmt_date(day):
    return (analysis.EPOCH + timedelta(days=int(day))).isoformat()


def load_configs(cfg, data_hives, first_run):
    for attempt in range(CONFIG_RETRIES if first_run else 1):
        configs = collect_retained_configs(cfg["mqtt_host"], cfg["mqtt_port"], CONFIG_WAIT_SECONDS)
        missing = [h for h in data_hives if "stores" not in (configs.get(h) or {})]
        if not missing:
            break
        if attempt < CONFIG_RETRIES - 1 and first_run:
            logger.info("waiting for the config with a stores section of %s", missing)
            time.sleep(CONFIG_RETRY_SECONDS)
    return configs


def analyse_hive(hive_id, days, weights, stores, store, now, today, lines):
    tare = stores["tare_kg"]
    critical = stores["critical_reserves_kg"]
    reserves = weights - tare
    smoothed = analysis.smooth_reserves(reserves)
    gain = analysis.daily_gain(weights)
    run_ts = int(now.timestamp())
    summary = []

    # reserves of the last year (idempotent: midnight UTC timestamps)
    for day, actual, smooth in zip(days, reserves, smoothed):
        fields = {"actual": float(actual)}
        if np.isfinite(smooth):
            fields["smoothed"] = float(smooth)
        lines.append(line("reserves", {"hive_id": hive_id}, fields, int(day) * 86400))

    # Analysis A: depletion of the stores
    dep = analysis.analyse_depletion(days, smoothed, critical, gain)
    store.delete_projection(
        hive_id, datetime.combine(today - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc),
        now + timedelta(days=400),
    )
    if dep is None:
        logger.warning("%s: not enough data for the depletion analysis", hive_id)
    else:
        fields = {
            "reserves_kg": dep["reserves_kg"], "consumption_kg_day": dep["consumption_kg_day"],
            "r2": dep["r2"], "critical_reserves_kg": float(critical),
        }
        if dep["days_to_critical"] is not None:
            fields["days_to_critical"] = dep["days_to_critical"]
            fields["critical_date_ts"] = run_ts + int(round(dep["days_to_critical"] * 86400))
        lines.append(line("forecast", {"hive_id": hive_id, "kind": "depletion"}, fields, run_ts))
        for day, value in analysis.projection(dep):
            lines.append(line("forecast_reserves", {"hive_id": hive_id, "method": "linear"},
                              {"reserves_kg": value}, day * 86400))
        if dep["status"] == "now":
            when = "already under the critical threshold"
        elif dep["status"] == "stable":
            when = "stable or growing, no depletion date"
        else:
            critical_day = analysis.day_number(today) + round(dep["days_to_critical"])
            when = f"critical in {dep['days_to_critical']:.0f} days ({fmt_date(critical_day)})"
        summary.append(f"reserves {dep['reserves_kg']:.1f} kg, consumption {dep['consumption_kg_day']:.3f} kg/day, "
                       f"R2 {dep['r2']:.2f} (fit on {dep['fit_days']} days): {when}")

    # Analysis B: harvest timing of the past season
    harvest = analysis.analyse_harvest(days, weights, tare, stores["winter_reserves_kg"])
    fields = {}
    for key, name, cast in (("optimal_day", "optimal_ts", int), ("harvestable_kg", "harvestable_kg", float),
                            ("actual_day", "actual_harvest_ts", int), ("actual_kg", "actual_harvest_kg", float),
                            ("delta_days", "delta_days", float)):
        if harvest[key] is not None:
            value = harvest[key] * 86400 if key.endswith("_day") else harvest[key]
            fields[name] = cast(value)
    fields["status"] = harvest["status"]
    lines.append(line("forecast", {"hive_id": hive_id, "kind": "harvest"}, fields, run_ts))
    summary.append(
        "harvest: optimal %s, real %s (%s kg), delta %s days, extractable %s kg [%s]" % (
            fmt_date(harvest["optimal_day"]) if harvest["optimal_day"] is not None else "-",
            fmt_date(harvest["actual_day"]) if harvest["actual_day"] is not None else "-",
            "%.1f" % harvest["actual_kg"] if harvest["actual_kg"] is not None else "-",
            harvest["delta_days"] if harvest["delta_days"] is not None else "-",
            "%.1f" % harvest["harvestable_kg"] if harvest["harvestable_kg"] is not None else "-",
            harvest["status"]))

    # Backtest on the past winter
    results = analysis.backtest(days, smoothed, analysis.backtest_origins(today), gain)
    midnight = (today - analysis.EPOCH).days * 86400
    for (method, horizon), r in sorted(results.items()):
        lines.append(line("backtest", {"hive_id": hive_id, "method": method, "horizon_days": horizon},
                          {"mae_kg": r["mae"], "bias_kg": r["bias"], "n": r["n"]}, midnight))
    summary.append("backtest: " + ", ".join(
        f"{m}+{h}d MAE {r['mae']:.2f} bias {r['bias']:+.2f} (n={r['n']})" for (m, h), r in sorted(results.items())))
    return summary


def run_once(cfg, store, first_run):
    started = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = now.date()
    since = datetime.combine(today - timedelta(days=DATA_DAYS), datetime.min.time(), tzinfo=timezone.utc)

    data = store.daily_weights(since)
    configs = load_configs(cfg, sorted(data), first_run)
    lines = []
    analysed = []
    for hive_id in sorted(set(data) | set(configs)):
        stores = (configs.get(hive_id) or {}).get("stores")
        if not stores:
            logger.warning("%s: no stores section in its config, skipped", hive_id)
            continue
        if hive_id not in data:
            logger.warning("%s: no weight data, skipped", hive_id)
            continue
        days, weights = data[hive_id]
        for text in analyse_hive(hive_id, days, weights, stores, store, now, today, lines):
            logger.info("%s: %s", hive_id, text)
        analysed.append(hive_id)
    store.write(lines)
    logger.info("run done: hives %s, %d points written, %.1f s", analysed, len(lines), time.monotonic() - started)


def main():
    token = os.environ.get("INFLUXDB_TOKEN", "")
    cfg = {
        "mqtt_host": os.environ.get("MQTT_HOST", "mosquitto"),
        "mqtt_port": int(os.environ.get("MQTT_PORT", "1883")),
    }
    interval = int(os.environ.get("ANALYTICS_INTERVAL_SECONDS", "600"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    store = Store(os.environ["INFLUXDB_URL"], os.environ["INFLUXDB_ORG"], os.environ["INFLUXDB_BUCKET"], token)
    store.wait_ready()
    logger.info("analytics started, interval %d s", interval)
    first_run = True
    while not stop.is_set():
        try:
            run_once(cfg, store, first_run)
        except Exception as e:  # noqa: BLE001 - keep the service alive, retry at the next interval
            message = f"{type(e).__name__}: {e}"
            logger.error("run failed: %s", message.replace(token, "***") if token else message)
        first_run = False
        stop.wait(interval)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
