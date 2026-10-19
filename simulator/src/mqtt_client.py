import json
import logging
from paho.mqtt.client import Client, CallbackAPIVersion, MQTT_ERR_NO_CONN

logger = logging.getLogger(__name__)


class MqttClient:
    def __init__(self, client_id, host="mosquitto", port=1883):
        self.client_id = client_id
        self.host = host
        self.port = port
        self.client = Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        self.on_message_callback = None
        self.on_connect_callback = None
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        # After a connection loss paho reconnects by itself, waiting 1 s, 2 s, 4 s ... up to 30 s
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def set_message_callback(self, callback):
        self.on_message_callback = callback

    def set_connect_callback(self, callback):
        """Called at EVERY successful connection (first one and reconnections).

        With a clean session the broker forgets the subscriptions when the connection
        drops, so whoever subscribes or publishes retained state must do it here.
        """
        self.on_connect_callback = callback

    def set_will(self, topic, payload, qos=1):
        self.client.will_set(topic, payload=payload, qos=qos, retain=True)

    def connect(self):
        """Start connecting in the background and return at once.

        connect_async + loop_start keep trying (with the reconnect delay) even if the
        broker is not reachable yet, so a simulator started before the broker does not crash.
        """
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()
        logger.info(f"Client {self.client_id} connecting to {self.host}:{self.port}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()
        logger.info(f"Client {self.client_id} disconnected")

    def subscribe(self, topic, qos=1):
        self.client.subscribe(topic, qos=qos)
        logger.debug(f"Client {self.client_id} subscribed to {topic}")

    def publish(self, topic, payload, qos=0, retain=False):
        result = self.client.publish(topic, payload=payload, qos=qos, retain=retain)
        if result.rc == MQTT_ERR_NO_CONN:
            # Broker away: a lost reading is fine (QoS 0); the state is republished on reconnect
            logger.debug(f"Not connected, dropped message for {topic}")
        elif result.rc != 0:
            logger.warning(f"Failed to publish to {topic}: {result.rc}")

    def publish_json(self, topic, data, qos=0, retain=False):
        payload = json.dumps(data)
        self.publish(topic, payload, qos=qos, retain=retain)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            logger.info(f"Client {self.client_id} connected successfully")
            if self.on_connect_callback:
                try:
                    self.on_connect_callback()
                except Exception as e:
                    logger.error(f"Error in the connect callback of {self.client_id}: {e}")
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
