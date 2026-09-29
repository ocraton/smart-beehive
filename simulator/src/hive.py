import math
import logging
from datetime import datetime, timezone
from .mqtt_client import MqttClient

logger = logging.getLogger(__name__)


class Hive:
    def __init__(self, hive_id, mqtt_host="mosquitto", mqtt_port=1883):
        self.hive_id = hive_id
        self.mqtt = MqttClient(
            client_id=hive_id, host=mqtt_host, port=mqtt_port
        )

        # Physical state
        self.brood_temp = 32.0
        self.humidity = 65.0
        self.weight = 50.0
        self.ext_temp = 20.0
        self.ext_hum = 60.0
        self.flights = 0

        # Actuator state
        self.heater_on = False
        self.fan_on = False

        # Simulation time (in minutes, since start)
        self.sim_time_minutes = 0

        # Physics constants
        self.temp_inercia = 0.08
        self.humidity_inercia = 0.10
        self.weight_daily_growth = 0.03
        self.weight_daily_decline = 0.02
        self.brood_baseline = 33.0
        self.brood_target_heat = 35.0
        self.brood_target_cool = 30.0
        self.humidity_baseline = 65.0
        self.humidity_with_fan = 55.0

        # Setup MQTT
        self._setup_mqtt()

    def _setup_mqtt(self):
        will_topic = f"apiary/{self.hive_id}/status"
        self.mqtt.set_will(will_topic, "offline", qos=1)
        self.mqtt.set_message_callback(self._on_mqtt_message)
        self.mqtt.connect()

        # Subscribe to actuator commands
        cmd_topic = f"apiary/{self.hive_id}/actuators/+/cmd"
        self.mqtt.subscribe(cmd_topic, qos=1)

        # Publish online status
        self.mqtt.publish(will_topic, "online", qos=1, retain=True)
        logger.info(f"Hive {self.hive_id} initialized and online")

    def _on_mqtt_message(self, topic, payload):
        if "heater/cmd" in topic:
            self.heater_on = payload.upper() == "ON"
            self.mqtt.publish(
                f"apiary/{self.hive_id}/actuators/heater/state",
                "ON" if self.heater_on else "OFF",
                qos=1,
                retain=True,
            )
            logger.info(f"Hive {self.hive_id} heater set to {self.heater_on}")
        elif "fan/cmd" in topic:
            self.fan_on = payload.upper() == "ON"
            self.mqtt.publish(
                f"apiary/{self.hive_id}/actuators/fan/state",
                "ON" if self.fan_on else "OFF",
                qos=1,
                retain=True,
            )
            logger.info(f"Hive {self.hive_id} fan set to {self.fan_on}")

    def tick(self, sim_minutes_per_tick):
        self.sim_time_minutes += sim_minutes_per_tick
        self._update_external_conditions()
        self._update_brood_temperature()
        self._update_humidity()
        self._update_weight()
        self._update_flights()
        self._publish_telemetry()

    def _update_external_conditions(self):
        # 24-hour cycle: 24 * 60 = 1440 minutes
        cycle_position = (self.sim_time_minutes % 1440) / 1440.0
        # Base 18°C, amplitude 8°C, peak in early afternoon
        angle = 2 * math.pi * (cycle_position - 0.25)
        ext_temp_base = 18.0 + 8.0 * math.sin(angle)
        # Small noise
        import random
        ext_temp_noise = random.gauss(0, 0.5)
        self.ext_temp = ext_temp_base + ext_temp_noise

        # Humidity inversely related to temperature
        hum_base = 80.0 - (self.ext_temp - 10.0) * 2.0
        hum_noise = random.gauss(0, 2.0)
        self.ext_hum = max(30, min(90, hum_base + hum_noise))

    def _update_brood_temperature(self):
        # Target depends on actuators
        if self.heater_on:
            target = self.brood_target_heat
        elif self.fan_on:
            target = self.brood_target_cool
        else:
            # Between baseline and external, favor baseline
            target = self.brood_baseline + 0.3 * (self.ext_temp - self.brood_baseline)

        import random
        noise = random.gauss(0, 0.3)
        self.brood_temp += (target - self.brood_temp) * self.temp_inercia + noise
        self.brood_temp = max(20, min(40, self.brood_temp))

    def _update_humidity(self):
        if self.fan_on:
            target_hum = self.humidity_with_fan
        else:
            target_hum = self.humidity_baseline + 0.2 * (self.ext_hum - self.humidity_baseline)

        import random
        noise = random.gauss(0, 1.0)
        self.humidity += (target_hum - self.humidity) * self.humidity_inercia + noise
        self.humidity = max(30, min(90, self.humidity))

    def _update_weight(self):
        # Diurnal cycle: grow during day, decline at night
        cycle_position = (self.sim_time_minutes % 1440) / 1440.0
        # Day is roughly 6:00 to 18:00 (0.25 to 0.75 of cycle)
        is_daytime = 0.25 < cycle_position < 0.75

        import random
        if is_daytime and self.ext_temp > 15:
            # Small daily gain during foraging hours
            self.weight += self.weight_daily_growth / 288  # 288 ticks per 24h @ 5sec
        else:
            # Small nightly decline (consumption)
            self.weight -= self.weight_daily_decline / 288

        self.weight = max(10, min(60, self.weight))

    def _update_flights(self):
        # Flights depend on external conditions
        if self.ext_temp < 12 or self.ext_hum > 85:
            self.flights = 0
        else:
            import random
            # Scale with temperature (better at 20-25°C) and inverse of humidity
            temp_factor = max(0, 1.0 - abs(self.ext_temp - 22.5) / 15.0)
            hum_factor = max(0, 1.0 - (self.ext_hum - 50.0) / 40.0)
            base_flights = 100 * temp_factor * hum_factor
            self.flights = int(base_flights + random.gauss(0, 10))
            self.flights = max(0, self.flights)

    def _publish_telemetry(self):
        timestamp = self._get_sim_timestamp()
        sensors = {
            "brood_temp": {
                "value": round(self.brood_temp, 2),
                "unit": "C",
                "ts": timestamp,
            },
            "humidity": {
                "value": round(self.humidity, 2),
                "unit": "%",
                "ts": timestamp,
            },
            "weight": {
                "value": round(self.weight, 2),
                "unit": "kg",
                "ts": timestamp,
            },
            "flights": {
                "value": self.flights,
                "unit": "count",
                "ts": timestamp,
            },
            "ext_temp": {
                "value": round(self.ext_temp, 2),
                "unit": "C",
                "ts": timestamp,
            },
            "ext_hum": {
                "value": round(self.ext_hum, 2),
                "unit": "%",
                "ts": timestamp,
            },
        }

        for sensor_name, data in sensors.items():
            topic = f"apiary/{self.hive_id}/sensors/{sensor_name}"
            self.mqtt.publish_json(topic, data, qos=0, retain=False)

    def _get_sim_timestamp(self):
        # Convert sim_time_minutes to ISO 8601 UTC timestamp
        # Start from 2026-09-28 00:00:00 UTC
        from datetime import timedelta
        start_time = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
        sim_time = start_time + timedelta(minutes=self.sim_time_minutes)
        return sim_time.isoformat().replace("+00:00", "Z")

    def shutdown(self):
        # Publish offline status explicitly (before LWT takes over)
        self.mqtt.publish(
            f"apiary/{self.hive_id}/status", "offline", qos=1, retain=True
        )
        self.mqtt.disconnect()
        logger.info(f"Hive {self.hive_id} shutdown complete")
