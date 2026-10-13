import logging
import time
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logger = logging.getLogger(__name__)

# A live simulator publishes every 5 s, so a session in progress has a point younger than this.
# It must stay below the time a "docker compose down && up" needs to get here (at least the
# 10 s healthcheck interval of influxdb): otherwise the previous session, stopped seconds ago,
# would count as live and be preserved as 5 s-resolution residue before the new one.
LIVE_MAX_AGE_S = 8
BATCH_SIZE = 5000
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def flux_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, url, org, bucket, token):
        self.org = org
        self.bucket = bucket
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=120_000)
        self.query_api = self.client.query_api()

    def _records(self, flux):
        return [r for table in self.query_api.query(flux) for r in table.records]

    def wait_ready(self, attempts=40, delay=3):
        # On the very first start InfluxDB can answer pings before its setup has finished
        flux = f'from(bucket: "{self.bucket}") |> range(start: -1m) |> limit(n: 1)'
        for attempt in range(1, attempts + 1):
            try:
                self._records(flux)
                return
            except Exception as e:  # noqa: BLE001 - any failure just means "not ready yet"
                logger.info("InfluxDB not ready (%s), attempt %d/%d", type(e).__name__, attempt, attempts)
                time.sleep(delay)
        raise RuntimeError("InfluxDB did not become ready")

    def live_session_start(self):
        """Start of the live session in progress, or None if the simulator is not publishing."""
        weight = (
            f'from(bucket: "{self.bucket}")\n'
            f'  |> range(start: -{LIVE_MAX_AGE_S}s)\n'
            '  |> filter(fn: (r) => r._measurement == "telemetry" and r.sensor == "weight")\n'
            '  |> count()'
        )
        if not self._records(weight):
            return None

        all_weights = (
            f'from(bucket: "{self.bucket}")\n'
            f'  |> range(start: {flux_time(UNIX_EPOCH)})\n'
            '  |> filter(fn: (r) => r._measurement == "telemetry" and r.sensor == "weight")\n'
            '  |> group()\n'
            '  |> sort(columns: ["_time"])\n'
        )
        # The session starts right after the last gap longer than one minute
        after_gap = self._records(
            all_weights + '  |> elapsed(unit: 1s)\n  |> filter(fn: (r) => r.elapsed > 60)\n  |> last()'
        )
        if after_gap:
            return after_gap[0].get_time()
        return self._records(all_weights + "  |> first()")[0].get_time()

    def first_weight_since(self, hive_id, since):
        records = self._records(
            f'from(bucket: "{self.bucket}")\n'
            f'  |> range(start: {flux_time(since)})\n'
            f'  |> filter(fn: (r) => r._measurement == "telemetry" and r.sensor == "weight" and r.hive_id == "{hive_id}")\n'
            '  |> first()'
        )
        return records[0].get_value() if records else None

    def delete_telemetry(self, stop):
        """Delete every telemetry point up to `stop` (never touches actuator or alert)."""
        self.client.delete_api().delete(
            UNIX_EPOCH, stop, '_measurement="telemetry"', bucket=self.bucket, org=self.org
        )

    def write(self, lines):
        write_api = self.client.write_api(write_options=SYNCHRONOUS)
        for i in range(0, len(lines), BATCH_SIZE):
            write_api.write(
                bucket=self.bucket, org=self.org,
                record=lines[i:i + BATCH_SIZE], write_precision=WritePrecision.S,
            )

    def count_telemetry(self, start, stop):
        records = self._records(
            f'from(bucket: "{self.bucket}")\n'
            f'  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})\n'
            '  |> filter(fn: (r) => r._measurement == "telemetry")\n'
            '  |> group(columns: ["hive_id", "sensor"])\n'
            '  |> count()'
        )
        return {(r.values["hive_id"], r.values["sensor"]): r.get_value() for r in records}

    def close(self):
        self.client.close()
