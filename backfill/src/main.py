import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import model
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s - backfill - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(token):
    url = os.environ["INFLUXDB_URL"]
    org = os.environ["INFLUXDB_ORG"]
    bucket = os.environ["INFLUXDB_BUCKET"]
    hive_ids = [h.strip() for h in os.environ["HIVE_IDS"].split(",") if h.strip()]
    days = int(os.environ.get("BACKFILL_DAYS", "365"))
    seed = int(os.environ.get("BACKFILL_SEED", "42"))
    profiles = json.loads(Path(os.environ["PROFILES_PATH"]).read_text())

    started = time.monotonic()
    store = Store(url, org, bucket, token)
    store.wait_ready()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    session_start = store.live_session_start()
    end = session_start or now
    start = end - timedelta(days=days)
    first_hour = floor_hour(start)
    if first_hour < start:
        first_hour += timedelta(hours=1)
    last_hour = floor_hour(end - timedelta(seconds=1))
    hours = int((last_hour - first_hour).total_seconds() // 3600) + 1

    if session_start:
        logger.info("Live session in progress since %s: history ends there, the session is left untouched", iso(end))
    else:
        logger.info("No live session in progress: history ends now (%s)", iso(end))
    logger.info("Interval %s -> %s, %d hourly points per series, seed %d", iso(first_hour), iso(last_hour), hours, seed)

    # Weight anchors: where the live simulator starts (or started) from
    anchors = {}
    for hive_id in hive_ids:
        value = store.first_weight_since(hive_id, session_start) if session_start else None
        if value is None:
            value = model.initial_weight(model.resolve_profile(profiles, hive_id))
            origin = "profile: tare + reserves"
        else:
            origin = "first live reading"
        anchors[hive_id] = value
        logger.info("%s: weight anchor %.2f kg (%s)", hive_id, value, origin)

    # Generate before deleting: a model error must never leave the bucket emptied
    series = model.generate(profiles, hive_ids, first_hour, last_hour, seed)
    lines = []
    for hive_id in hive_ids:
        series[hive_id]["weight"] = model.anchor_weight(series[hive_id]["weight"], anchors[hive_id])
        for sensor, points in series[hive_id].items():
            lines.extend(
                f"telemetry,hive_id={hive_id},sensor={sensor} value={value:.4f} {epoch}"
                for epoch, value in points
            )

    # Everything before `end`: old test sessions at 5 s resolution would skew the averages,
    # and anything older than `start` (e.g. from a previous run) would not be aligned.
    store.delete_telemetry(end - timedelta(seconds=1))
    logger.info("Deleted previous telemetry before %s", iso(end))
    store.write(lines)

    counts = store.count_telemetry(first_hour, end)
    ok = True
    for hive_id in hive_ids:
        per_sensor = {s: counts.get((hive_id, s), 0) for s in model.SENSORS}
        if any(n != hours for n in per_sensor.values()):
            ok = False
        last_weight = series[hive_id]["weight"][-1][1]
        logger.info(
            "%s: %d points written (%s); last weight %.2f kg",
            hive_id, sum(per_sensor.values()), ", ".join(f"{s}={n}" for s, n in per_sensor.items()), last_weight,
        )
    logger.info("Done in %.1f s", time.monotonic() - started)
    store.close()
    return ok


def main():
    token = os.environ.get("INFLUXDB_TOKEN", "")
    try:
        ok = run(token)
    except Exception as e:  # noqa: BLE001 - report, then fail so the simulator does not start
        message = f"{type(e).__name__}: {e}"
        logger.error("Backfill failed: %s", message.replace(token, "***") if token else message)
        return 1
    if not ok:
        logger.error("Backfill failed: written point counts differ from the expected ones")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
