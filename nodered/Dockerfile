FROM nodered/node-red:5

# package.json replaces the base image's one, so it must keep node-red as a dependency
COPY package.json .
RUN npm install --unsafe-perm --no-update-notifier --no-fund --only=production

# Generates flows_cred.json from INFLUXDB_TOKEN, then starts Node-RED (see the script)
COPY --chmod=755 docker-entrypoint.sh /usr/src/node-red/docker-entrypoint.sh
ENTRYPOINT ["./docker-entrypoint.sh"]
