import logging
import time
from datetime import datetime, timezone

import numpy as np
from influxdb_client import InfluxDBClient, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logger = logging.getLogger(__name__)

BATCH_SIZE = 5000


def flux_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _escape(value):
    return str(value).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def line(measurement, tags, fields, ts):
    """One line of line protocol (precision: seconds). Integers are written as `i` fields."""
    tag_part = ",".join(f"{_escape(k)}={_escape(v)}" for k, v in sorted(tags.items()))
    def encode(v):
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, int) and not isinstance(v, bool):
            return f"{v}i"
        return repr(float(v))

    field_part = ",".join(f"{k}={encode(v)}" for k, v in fields.items())
    return f"{measurement},{tag_part} {field_part} {int(ts)}"


class Store:
    def __init__(self, url, org, bucket, token):
        self.org = org
        self.bucket = bucket
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=120_000)
        self.query_api = self.client.query_api()

    def wait_ready(self, attempts=40, delay=3):
        flux = f'from(bucket: "{self.bucket}") |> range(start: -1m) |> limit(n: 1)'
        for attempt in range(1, attempts + 1):
            try:
                self.query_api.query(flux)
                return
            except Exception as e:  # noqa: BLE001 - "not ready yet", whatever the reason
                logger.info("InfluxDB not ready (%s), attempt %d/%d", type(e).__name__, attempt, attempts)
                time.sleep(delay)
        raise RuntimeError("InfluxDB did not become ready")

    def daily_weights(self, since):
        """Daily mean weight per hive since `since`; the aggregation is done by the database.

        Returns {hive_id: (days since 1970-01-01 as ints, mean weight in kg)}.
        """
        flux = (
            f'from(bucket: "{self.bucket}")\n'
            f'  |> range(start: {flux_time(since)})\n'
            '  |> filter(fn: (r) => r._measurement == "telemetry" and r.sensor == "weight")\n'
            '  |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")\n'
            '  |> keep(columns: ["_time", "_value", "hive_id"])'
        )
        per_hive = {}
        for table in self.query_api.query(flux):
            for r in table.records:
                per_hive.setdefault(r.values["hive_id"], []).append((r.get_time(), r.get_value()))
        result = {}
        for hive_id, rows in per_hive.items():
            rows.sort()
            days = np.array([int(t.timestamp() // 86400) for t, _ in rows])
            weights = np.array([float(v) for _, v in rows])
            result[hive_id] = (days, weights)
        return result

    def delete_projection(self, hive_id, method, start, stop):
        """Remove the previous projection of a hive for one method, so no stale tail is left."""
        predicate = f'_measurement="forecast_reserves" AND hive_id="{hive_id}" AND method="{method}"'
        self.client.delete_api().delete(start, stop, predicate, bucket=self.bucket, org=self.org)

    def delete_backtest(self, hive_id, start, stop):
        """Remove the backtest points of a hive in [start, stop): they are rewritten every run."""
        predicate = f'_measurement="backtest" AND hive_id="{hive_id}"'
        self.client.delete_api().delete(start, stop, predicate, bucket=self.bucket, org=self.org)

    def write(self, lines):
        write_api = self.client.write_api(write_options=SYNCHRONOUS)
        for i in range(0, len(lines), BATCH_SIZE):
            write_api.write(
                bucket=self.bucket, org=self.org,
                record=lines[i:i + BATCH_SIZE], write_precision=WritePrecision.S,
            )

    def close(self):
        self.client.close()
