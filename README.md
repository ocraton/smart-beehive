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
