#!/bin/bash
# Writes the credentials of the InfluxDB config node from the environment, then starts
# Node-RED as the base image does. .env stays the only place where the token lives:
# nodered/data/flows_cred.json is generated at every start (and is in .gitignore).
#
# The file is plain JSON because settings.js sets credentialSecret: false. The token is
# read from the environment inside node, so it never appears on a command line or in a log.
set -e

: "${INFLUXDB_TOKEN:?INFLUXDB_TOKEN is not set (copy .env.example to .env)}"

# b3a1f0c2d4e56789 is the id of the "InfluxDB beehive" config node in flows.json
node -e '
const fs = require("fs");
const creds = { b3a1f0c2d4e56789: { token: process.env.INFLUXDB_TOKEN } };
fs.writeFileSync("/data/flows_cred.json", JSON.stringify(creds), { mode: 0o600 });
'
echo "Node-RED: InfluxDB credentials written to /data/flows_cred.json"

exec ./entrypoint.sh "$@"
