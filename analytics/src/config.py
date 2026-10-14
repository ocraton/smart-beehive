import json
import logging
import time

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def collect_retained_configs(host, port, wait_seconds=5):
    """Per-hive config, read from the retained messages of apiary/+/config.

    Connects, subscribes, waits a few seconds (the broker delivers the retained messages
    right away) and disconnects. Returns {hive_id: config dict}.
    """
    configs = {}

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe("apiary/+/config", qos=1)

    def on_message(client, userdata, msg):
        parts = msg.topic.split("/")
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except ValueError:
            logger.warning("ignored a config that is not valid JSON on %s", msg.topic)
            return
        if len(parts) == 3 and isinstance(payload, dict):
            configs[parts[1]] = payload

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="analytics-config")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()
    time.sleep(wait_seconds)
    client.loop_stop()
    client.disconnect()
    return configs
