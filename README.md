# Smart Beehive Monitoring System

IoT system for remote monitoring of (simulated) beehives: MQTT (Mosquitto),
a Python hive simulator, Node-RED and InfluxDB, orchestrated with Docker Compose.

## Setup

```bash
cp .env.example .env    # then edit the values (never commit .env)
docker compose up -d --build
```

`.env` holds the InfluxDB credentials and admin token. InfluxDB reads them
only on its **first** start, when the volume is empty: to change them later
you must reset its volumes (`docker compose down` followed by
`docker volume rm smart-beehive_influxdb_data smart-beehive_influxdb_config`,
which deletes all stored data).

### Linux: Node-RED data permissions

Node-RED runs as user `1000` inside the container and writes to the
bind-mounted `nodered/data/`. On Linux, if the container fails with
`EACCES` / permission denied, run:

```bash
sudo chown -R 1000:1000 nodered/data
```

(Not needed on macOS / Docker Desktop.)

## Services

| Service   | URL / port              | Notes                                      |
|-----------|-------------------------|--------------------------------------------|
| mosquitto | `localhost:1883`        | MQTT broker, anonymous access              |
| simulator | —                       | publishes telemetry for `HIVE_IDS`         |
| influxdb  | http://localhost:8086   | login with `INFLUXDB_USERNAME`/`PASSWORD`  |
| nodered   | http://localhost:1880   | flows saved to `nodered/data/flows.json`   |
| grafana   | http://localhost:3000   | login with `GRAFANA_ADMIN_USER`/`PASSWORD` |

## Verification

### Infrastructure

```bash
docker compose up -d --build
docker compose ps          # all services Up, influxdb (healthy)
docker compose logs nodered   # no errors
```

- http://localhost:8086: log in with the `.env` credentials. Under
  *Load Data → Buckets* the `beehive` bucket is listed.
- http://localhost:1880: the Node-RED editor loads, and the palette
  contains the `influxdb in`, `influxdb out` and `influxdb batch` nodes.

### Hive simulator

```bash
docker compose logs -f simulator
docker compose exec mosquitto mosquitto_sub -v -t "apiary/+/sensors/#"
docker compose exec mosquitto mosquitto_sub -v -t "apiary/+/status"
docker compose exec mosquitto mosquitto_pub -q 1 -t "apiary/hive-01/actuators/heater/cmd" -m "ON"
```

After the heater command, `brood_temp` of `hive-01` rises over the next ticks.
The `ts` field of each payload is the real UTC time; the physics (day/night
cycle) runs accelerated at `SIM_MINUTES_PER_TICK` simulated minutes per tick.

## Step 4 verification

The "Ingestion" flow (`nodered/data/flows.json`) writes every sensor reading to
InfluxDB as measurement `telemetry`, tags `hive_id` + `sensor`, field `value`.
About 30 seconds after `docker compose up -d`, this query should return one
series per hive and sensor (2 hives x 6 sensors = 12), each with roughly one
point per 5 seconds (~60 per 5 minutes):

```bash
set -a; . ./.env; set +a
docker compose exec influxdb influx query --org beehive --token "$INFLUXDB_TOKEN" \
 'from(bucket:"beehive") |> range(start:-5m)
  |> filter(fn:(r)=>r._measurement=="telemetry")
  |> group(columns:["hive_id","sensor"]) |> count()'
```

No point should have a 1970 timestamp, and `docker compose logs nodered` should
show no errors.

**After a fresh clone:** `nodered/data/flows_cred.json` is git-ignored, so the
InfluxDB token is not in the repo. Open http://localhost:1880, double-click the
`influxdb batch` node, edit the "InfluxDB beehive" server, paste the value of
`INFLUXDB_TOKEN` from your `.env` in the Token field, then Deploy. This is
needed only once.

## Step 5 verification

Grafana (`grafana/grafana-oss:13.0.2`) starts with the InfluxDB data source
already provisioned from `grafana/provisioning/datasources/influxdb.yaml`
(uid `influxdb-beehive`, query language Flux). The token is injected from `.env`
through environment interpolation and is never stored in the repo. As with
InfluxDB, the admin password is read only on the first start (empty
`grafana_data` volume).

```bash
docker compose up -d
curl http://localhost:3000/api/health

set -a; . ./.env; set +a
# data source health: expect "status":"OK"
curl -s -u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" \
  http://localhost:3000/api/datasources/uid/influxdb-beehive/health
```

Run a Flux query from the UI: open http://localhost:3000, log in, go to
**Explore**, pick the **InfluxDB** data source, paste the query below and click
**Run query**. You should see one series per hive (`hive-01`, `hive-02`).

```
from(bucket: "beehive")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "telemetry" and r.sensor == "brood_temp")
```
