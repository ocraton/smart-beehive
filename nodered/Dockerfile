FROM nodered/node-red:5

# package.json replaces the base image's one, so it must keep node-red as a dependency
COPY package.json .
RUN npm install --unsafe-perm --no-update-notifier --no-fund --only=production
