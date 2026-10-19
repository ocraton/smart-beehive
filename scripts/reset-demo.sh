#!/usr/bin/env bash
# Resets the demo to a clean state: no old alerts, no actuator history, fresh history and forecasts.
#
#   1. docker compose down -v      stops the stack and deletes the volumes (InfluxDB, Grafana, broker)
#   2. rm nodered/data/context/    deletes the state of the Node-RED rules saved on file
#   3. docker compose up -d --build
#      (the one-shot backfill rewrites the history, the simulator starts after it)
#   4. waits for the first complete run of the analytics (forecasts written to InfluxDB)
#
# It can be launched from any folder of the repository: it moves to the repository root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
    echo "Missing .env: copy .env.example to .env first." >&2
    exit 1
fi

echo "==> Stopping the stack and deleting the volumes (InfluxDB, Grafana, broker)"
docker compose down -v --remove-orphans

echo "==> Removing the saved state of the Node-RED rules"
rm -rf nodered/data/context

echo "==> Building and starting the stack"
docker compose up -d --build

# The analytics service logs "run done" at the end of every run, once the first one has
# written the reserves and the forecasts. Its first run also waits for the config of the hives.
echo "==> Waiting for the first run of the analytics (at most 4 minutes)"
deadline=$((SECONDS + 240))
until docker compose logs analytics 2>/dev/null | grep -q "run done"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "The analytics did not finish its first run in time: check 'docker compose logs analytics'." >&2
        exit 1
    fi
    sleep 3
done

# Node-RED publishes the config, then ingestion fills in the live telemetry
echo "==> Waiting for Node-RED to be healthy"
until [ "$(docker inspect -f '{{.State.Health.Status}}' beehive_nodered 2>/dev/null)" = "healthy" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "Node-RED is not healthy: check 'docker compose logs nodered'." >&2
        exit 1
    fi
    sleep 3
done

cat <<MSG

Demo ready.
  Grafana (dashboards)   http://localhost:3000        (Apiary, Apiary - Previsioni)
  Console scenari        http://localhost:1880/dashboard
  Node-RED (flow)        http://localhost:1880
  InfluxDB               http://localhost:8086
MSG
