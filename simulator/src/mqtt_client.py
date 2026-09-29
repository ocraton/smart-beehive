import json
import logging
from paho.mqtt.client import Client, CallbackAPIVersion

logger = logging.getLogger(__name__)


class MqttClient:
    def __init__(self, client_id, host="mosquitto", port=1883):
        self.client_id = client_id
        self.host = host
        self.port = port
        self.client = Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        self.on_message_callback = None
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def set_message_callback(self, callback):
        self.on_message_callback = callback

    def set_will(self, topic, payload, qos=1):
        self.client.will_set(topic, payload=payload, qos=qos, retain=True)

    def connect(self):
        try:
            self.client.connect(self.host, self.port, keepalive=60)
            self.client.loop_start()
            logger.info(f"Client {self.client_id} connecting to {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            raise

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()
        logger.info(f"Client {self.client_id} disconnected")

    def subscribe(self, topic, qos=1):
        self.client.subscribe(topic, qos=qos)
        logger.debug(f"Client {self.client_id} subscribed to {topic}")

    def publish(self, topic, payload, qos=0, retain=False):
        result = self.client.publish(topic, payload=payload, qos=qos, retain=retain)
        if result.rc != 0:
            logger.warning(f"Failed to publish to {topic}: {result.rc}")

    def publish_json(self, topic, data, qos=0, retain=False):
        payload = json.dumps(data)
        self.publish(topic, payload, qos=qos, retain=retain)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            logger.info(f"Client {self.client_id} connected successfully")
        else:
            logger.error(f"Client {self.client_id} connection failed with code {reason_code}")

    def _on_message(self, client, userdata, msg):
        if self.on_message_callback:
            try:
                payload = msg.payload.decode("utf-8")
                self.on_message_callback(msg.topic, payload)
            except Exception as e:
                logger.error(f"Error handling message on {msg.topic}: {e}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.info(f"Client {self.client_id} disconnected with code {reason_code}")
