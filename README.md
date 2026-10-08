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
| grafana   | http://localhost:3000   | Apiary dashboard, no login (anonymous viewer) |

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

## Step 6 verification

The **Apiary** dashboard is provisioned as code
(`grafana/provisioning/dashboards/apiary.json`, loaded by the provider in
`dashboards.yaml` into the "Smart Beehive" folder). It is also the home
dashboard, and anonymous access is enabled with the read-only `Viewer` role, so
no login is needed. This is meant for the local demo only: remove the
`GF_AUTH_ANONYMOUS_*` variables in a real deployment.

```bash
docker compose up -d
# anonymous access: expect HTTP 200, no credentials
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/api/dashboards/uid/apiary
```

Open http://localhost:3000: the Apiary dashboard opens by itself, with the last
30 minutes of data refreshed every 5 seconds. Each panel shows one series per
hive.

- **Hive selector**: the `Arnia` drop-down at the top (variable `hive_id`) is
  filled from the `hive_id` tag values stored in InfluxDB. Pick one hive,
  several, or `All`; a new hive appears after reloading the page.
- **Editing the dashboard**: changes made in the UI cannot be saved
  (`allowUiUpdates: false`). Edit `apiary.json` instead: Grafana reloads it
  within 30 seconds, no restart needed.

## Step 7 verification

Node-RED runs the heater/fan control loop. The thresholds live in
`nodered/data/config/thresholds.json` (`defaults` plus optional per-hive
overrides under `hives`, merged per section and field by field).

- Tab **Config**: when a hive reports `online`, its merged thresholds are
  published as a retained message on `apiary/{hive_id}/config`.
- Tab **Control**: for every `brood_temp` / `humidity` reading it applies the
  thresholds with hysteresis and sends `ON` / `OFF` on
  `apiary/{hive_id}/actuators/{heater|fan}/cmd` (QoS 1, not retained) only when
  the desired state differs from the one the hive confirmed on `.../state`.
  The heater has priority: the fan never runs while the heater is on.
- Tab **Ingestion**: every confirmed state is also written to InfluxDB
  (measurement `actuator`, tags `hive_id` + `actuator`, field `state` 1/0) and
  shown in the **Attuatori** row of the Grafana dashboard.

```bash
docker compose up -d --build

# thresholds in use: one retained message per online hive
docker compose exec mosquitto mosquitto_sub -v -t 'apiary/+/config'

# commands, confirmed states and the temperature that drives them
docker compose exec mosquitto mosquitto_sub -v \
  -t 'apiary/+/actuators/+/cmd' -t 'apiary/+/actuators/+/state' \
  -t 'apiary/+/sensors/brood_temp'
```

Expected: the heater gets `ON` when `brood_temp` drops below `heater.on_below`
and `OFF` when it rises above `heater.off_above`; in between nothing is sent.
Each `cmd` is followed by a matching `state`, and no command is sent while the
confirmed state already matches. `hive-02` switches at 31.5 / 33.0 instead of
32.0 / 33.5 because of its override.

**Changing the thresholds:** edit `nodered/data/config/thresholds.json`, then

```bash
docker compose restart nodered
```

Node-RED reads the file again and republishes the config of every online hive.
A config whose thresholds are inconsistent (for example `on_below` not lower
than `off_above`) is not published and a warning appears in
`docker compose logs nodered`. To try other thresholds without touching the
file, publish a retained config by hand; the loop adapts on the next reading
(it lasts until the next Node-RED restart):

```bash
docker compose exec mosquitto mosquitto_pub -q 1 -r -t apiary/hive-01/config -m \
  '{"heater":{"on_below":29.5,"off_above":31.0},"fan_temp":{"on_above":36.0,"off_below":34.5},"fan_humidity":{"on_above":75.0,"off_below":65.0}}'
```

## Step 8 verification

The simulator accepts **perturbations of the simulated environment** (weather,
moisture sources, colony activity). They are causes, never measured values: the
readings change only because the simulated physics reacts. The channel lives
under `sim/`, outside `apiary/`, so ingestion, control loop and Grafana do not
see it; with real hives it would not exist.

| Field             | Range      | Effect                                                        |
|-------------------|------------|---------------------------------------------------------------|
| `ext_temp_offset` | -20 .. +20 | added to the outside temperature (also moves `ext_hum`, flights) |
| `humidity_offset` | 0 .. +30   | added to the target of the internal humidity                  |
| `flight_factor`   | 0 .. 1     | multiplies the flight count                                   |

**Console:** http://localhost:1880/dashboard (Node-RED Dashboard 2.0, tab
"Scenarios"). Pick a hive, then move a slider (sends only that field when
released) or click a preset (sends the full object, so it replaces the previous
one). The sliders always show the state reported by the hive, and the table at
the bottom shows perturbations, actuators and last readings of every online hive.

| Preset              | ext_temp_offset | humidity_offset | flight_factor | What to expect                              |
|---------------------|-----------------|-----------------|---------------|---------------------------------------------|
| Normale             | 0               | 0               | 1             | no actuator commands                        |
| Ondata di freddo    | -15             | 0               | 1             | brood cools down, the heater cycles         |
| Ondata di caldo     | +15             | 0               | 1             | brood overheats, the fan cycles             |
| Umidità persistente | 0               | +30             | 1             | humidity stays above 75 % despite the fan   |
| Colonia debole      | 0               | 0               | 0.05          | flights close to zero in good weather       |
| Sciamatura          | —               | —               | —             | one-off event: weight drops ~2.5 kg in 3 ticks |

Heater and fan react only while the perturbed outside temperature is outside
the colony's comfort band (10 – 28 °C), so around the warmest (cold wave) or
coldest (heat wave) hours of the simulated day they pause: depending on the
simulated time, the first command arrives after a few seconds or up to ~3 minutes.

The same without the console:

```bash
# current perturbations (retained, one per hive)
docker compose exec mosquitto mosquitto_sub -v -t 'sim/+/perturbation/state'

# partial update: only the given fields change, out-of-range values are clamped
docker compose exec mosquitto mosquitto_pub -q 1 -t sim/hive-01/perturbation/set \
  -m '{"ext_temp_offset": -15}'

# back to normal
docker compose exec mosquitto mosquitto_pub -q 1 -t sim/hive-01/perturbation/set \
  -m '{"ext_temp_offset": 0, "humidity_offset": 0, "flight_factor": 1}'

# swarm event
docker compose exec mosquitto mosquitto_pub -q 1 -t sim/hive-01/event -m '{"type": "swarm"}'

# watch the control loop react
docker compose exec mosquitto mosquitto_sub -v \
  -t 'apiary/+/actuators/+/cmd' -t 'apiary/+/sensors/brood_temp'
```

With no perturbation the colony keeps the brood at about 34.3 °C by itself and
no actuator command is sent. Restarting the simulator
(`docker compose restart simulator`) resets every perturbation to its default.
