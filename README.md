# Smart Beehive Monitoring System

IoT system for remote monitoring of (simulated) beehives: MQTT (Mosquitto),
a Python hive simulator, Node-RED and InfluxDB, orchestrated with Docker Compose.

## Setup

```bash
cp .env.example .env    # then edit the values (never commit .env)
docker compose up -d --build
```

Startup order: `influxdb` (healthy) → `backfill` (writes one year of history and
exits) → `simulator` (starts only when the backfill has completed successfully).
The hives are defined once, in `x-hive-ids` at the top of `docker-compose.yml`, and
shared by `simulator` and `backfill`.

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
| backfill  | —                       | one-shot: writes a year of history, then exits |
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

## Step 9 verification

Node-RED (tab **Anomalies**) evaluates three rules per hive on every reading and
publishes an alert on `apiary/{hive_id}/alerts` (QoS 1, not retained: alerts are
events) only when a rule changes state:

| Rule          | Severity | Anomalous reading                                              | Raised after | Resolved after |
|---------------|----------|----------------------------------------------------------------|--------------|----------------|
| `weight_drop` | critical | max weight of the last 60 s − current weight > 1.5 kg           | 1 reading    | 1 reading      |
| `inactivity`  | critical | flights ≤ 8 while the weather is favourable (15–30 °C, RH ≤ 80) | 24 readings  | 3 readings     |
| `humidity`    | warning  | internal humidity > 75 %                                       | 24 readings  | 3 readings     |

"After N readings" means N **consecutive** readings (24 readings = 2 minutes):
this debounce gives exactly one `raised` and one `resolved` per episode. With
unfavourable weather `inactivity` cannot be evaluated and its counters are
frozen, so an active alert is not resolved just because it is night. The
thresholds are the `anomalies` section of `nodered/data/config/thresholds.json`
(per-hive overrides work as for the other sections). The rule state is stored in
Node-RED's `file` context store (`nodered/data/context/`, git-ignored), so it
survives a Node-RED restart.
The `weight_drop` window is a time window (`window_seconds`, measured on the payload
`ts`), not a number of readings: readings saved by a previous session (after a
simulator restart or a change of profile) are older than the window and are never
compared with the new ones.

Watch the alerts:

```bash
docker compose exec mosquitto mosquitto_sub -v -t 'apiary/+/alerts'
```

Provoke each anomaly from the console (http://localhost:1880/dashboard), after
selecting a hive:

| Click                 | Expected alert                                                              |
|-----------------------|-----------------------------------------------------------------------------|
| "Sciamatura"          | `weight_drop` raised within 2 readings, resolved by itself about a minute later |
| "Colonia debole"      | `inactivity` raised after 2 minutes of favourable weather (it waits for the simulated day); "Normale" resolves it |
| "Umidità persistente" | fan ON after a few seconds, `humidity` raised after 2 minutes; "Normale" resolves it once humidity is back under 75 % |

The "Anomalie attive" column of the console table shows the active alerts of
each hive.

In Grafana (http://localhost:3000) every anomaly is a coloured band on the time
series, from `raised` to `resolved` or up to now while it is still active (red =
critical, orange = warning; hover the band for hive, rule and message). The
**Anomalie** row lists the alert events of the selected time range, newest
first. Alerts are stored in InfluxDB as measurement `alert` (tags `hive_id`,
`rule`, `severity`; fields `state`, `message`, `value`, plus `raised_at` on
resolved events):

```bash
set -a; . ./.env; set +a
docker compose exec influxdb influx query --org beehive --token "$INFLUXDB_TOKEN" \
 'from(bucket:"beehive") |> range(start:-1h)
  |> filter(fn:(r)=>r._measurement=="alert" and r._field=="state")'
```

## Step 10 verification

The one-shot `backfill` service (`backfill/`) runs at every stack start, before the
simulator, and fills InfluxDB with one year (`BACKFILL_DAYS=365`) of **hourly**
synthetic telemetry per hive, same schema as the live data (`telemetry`, tags
`hive_id` + `sensor`, field `value`, timestamps in seconds). It is deterministic:
`BACKFILL_SEED=42` always gives the same values for the same date.

What it does, in order:

1. Looks for a live session: telemetry younger than 8 s. If there is one, the history
   ends where the session starts and the session is never touched; otherwise it ends now.
2. Generates the year (about 1.7 s) and anchors every hive's weight so that the last
   historical value equals the weight the live simulator starts from (first live
   reading, or `tare_kg + reserves_kg` of the hive profile).
3. Deletes all `telemetry` before the end (old test sessions at 5 s resolution would
   skew the averages; `actuator` and `alert` are not touched), then writes the points
   and checks the counts. Exit code 0 only if everything matches, so the simulator
   never starts after a failed backfill.

Regenerate the history (this also resets the live session):

```bash
docker compose down && docker compose up -d
docker compose logs backfill     # interval, points per hive, duration, anchors
```

`docker compose up -d` on a stack that is already running re-runs the backfill: the
session in progress is detected and kept, only the history before it is rewritten and
the simulator is not restarted.

**Profiles.** `simulator/profiles.json` is the single source of truth for the
simulated hives: it is mounted read-only in both the `simulator` and the `backfill`
(`PROFILES_PATH=/config/profiles.json`). It defines one profile per hive (plus
`default` for hives not listed): `tare_kg`, `reserves_kg`, `strength`, `flow_scale`,
`consumption_scale`, `honey_harvest_kg` / `harvest_day` and `autumn_feeding_kg`. The
tare is the weight of the empty hive plus bees (a weaker colony has fewer bees, so a
lower tare); `reserves_kg` are the CURRENT stores, when the live simulation starts.
**Initial weight = tare_kg + reserves_kg**, and **reserves = weight - tare_kg**, which
the forecasting step will use to estimate when the stores run out. `hive-01` is strong
(tare 24.0 + reserves 19.5 = 43.5 kg, harvest ~12 kg on 15 July), `hive-02` is weak
(tare 22.0 + reserves 8.0 = 30.0 kg, harvest ~6 kg on 18 July: at about 1.5 kg/month
its stores would run out around March). Edit the file and restart the stack
(`docker compose down && docker compose up -d`) to change them.

**Model.** `backfill/src/model.py` is organised on the four components of lesson 10:
`trend_component` (forager population, comb), `seasonal_component` (annual
temperature, daylight, nectar flow, brood season, consumption),
`cyclical_component` (Markov chain of good/bad weather days) and
`irregular_component` (noise, honey harvest, autumn feeding).

Checks (expected: 8760 points per series, 12 series):

```bash
set -a; . ./.env; set +a
# points per hive and sensor, history only
docker compose exec influxdb influx query --org beehive --token "$INFLUXDB_TOKEN" \
 'from(bucket:"beehive") |> range(start: -400d, stop: <end of the backfill, see its log>)
  |> filter(fn:(r)=>r._measurement=="telemetry")
  |> group(columns:["hive_id","sensor"]) |> count()'

# monthly mean of the outdoor temperature: coldest January, hottest July
docker compose exec influxdb influx query --org beehive --token "$INFLUXDB_TOKEN" \
 'from(bucket:"beehive") |> range(start: -400d)
  |> filter(fn:(r)=>r.sensor=="ext_temp" and r.hive_id=="hive-01")
  |> aggregateWindow(every: 1mo, fn: mean, createEmpty: false, timeSrc: "_start")
  |> keep(columns:["_time","_value"])'

# largest daily weight drop: the honey harvest (hive-01: -12.2 kg on 15 July)
docker compose exec influxdb influx query --org beehive --token "$INFLUXDB_TOKEN" \
 'from(bucket:"beehive") |> range(start: -400d)
  |> filter(fn:(r)=>r.sensor=="weight" and r.hive_id=="hive-01")
  |> aggregateWindow(every: 1d, fn: last, createEmpty: false, timeSrc: "_start")
  |> difference() |> sort(columns:["_value"]) |> limit(n:1)'
```

In Grafana (http://localhost:3000) the **Storico** row at the bottom of the Apiary
dashboard shows daily weight, outdoor temperature (daily mean, with dashed min/max)
and daily flight activity over the last 12 months, whatever the time picker says;
it follows the `Arnia` selector.
