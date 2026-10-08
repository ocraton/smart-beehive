import json
import math
import logging
import random
import zlib
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
        self.brood_temp = 34.3
        self.humidity = 65.0
        # Different per hive but stable across restarts (hash() is salted per process)
        self.weight = 40.0 + (zlib.crc32(hive_id.encode()) % 1500) / 100.0
        self.ext_temp = 20.0
        self.ext_hum = 60.0
        self.flights = 0

        # Actuator state
        self.heater_on = False
        self.fan_on = False

        # Perturbations of the simulated ENVIRONMENT, driven over sim/{hive_id}/...
        # They change causes (weather, moisture, colony activity), never the readings.
        self.perturbation = {
            "ext_temp_offset": 0.0,
            "humidity_offset": 0.0,
            "flight_factor": 1.0,
        }
        self.swarm_ticks_left = 0

        # Clock of the accelerated physics (day/night cycle), not of the published "ts"
        self.sim_time_minutes = 0

        # Physics constants
        self.temp_inercia = 0.08
        self.humidity_inercia = 0.10
        self.weight_daily_growth = 0.03
        self.weight_daily_decline = 0.02
        # The colony thermoregulates: the brood holds its setpoint while the outside
        # temperature stays in the comfort band, and drifts proportionally outside it.
        self.brood_setpoint = 34.3
        self.brood_comfort_min = 10.0
        self.brood_comfort_max = 28.0
        self.brood_cold_gain = 1.5
        self.brood_heat_gain = 1.0
        self.brood_noise = 0.1
        self.brood_target_heat = 35.0
        self.brood_target_cool = 30.0
        self.humidity_baseline = 65.0
        self.humidity_fan_drop = 13.0
        self.humidity_noise = 0.5
        self.swarm_weight_loss = 2.5
        self.swarm_ticks = 3

        base = f"apiary/{hive_id}/actuators"
        self._handlers = {
            f"{base}/heater/cmd": lambda payload: self._on_actuator_cmd("heater", payload),
            f"{base}/fan/cmd": lambda payload: self._on_actuator_cmd("fan", payload),
            f"sim/{hive_id}/perturbation/set": self._on_perturbation_set,
            f"sim/{hive_id}/event": self._on_sim_event,
        }

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

        # Simulator-only channel, outside apiary/: a real hive would not have it
        self.mqtt.subscribe(f"sim/{self.hive_id}/perturbation/set", qos=1)
        self.mqtt.subscribe(f"sim/{self.hive_id}/event", qos=1)

        # Publish online status
        self.mqtt.publish(will_topic, "online", qos=1, retain=True)

        # Publish the real initial actuator state, so subscribers never see a stale retained one
        self._publish_actuator_state("heater")
        self._publish_actuator_state("fan")

        # Retained defaults: a simulator restart also resets what the console shows
        self._publish_perturbation_state()
        logger.info(f"Hive {self.hive_id} initialized and online")

    def _publish_actuator_state(self, name):
        is_on = self.heater_on if name == "heater" else self.fan_on
        self.mqtt.publish(
            f"apiary/{self.hive_id}/actuators/{name}/state",
            "ON" if is_on else "OFF",
            qos=1,
            retain=True,
        )

    def _on_mqtt_message(self, topic, payload):
        handler = self._handlers.get(topic)
        if handler:
            handler(payload)

    def _on_actuator_cmd(self, name, payload):
        is_on = payload.upper() == "ON"
        if name == "heater":
            self.heater_on = is_on
        else:
            self.fan_on = is_on
        self._publish_actuator_state(name)
        logger.info(f"Hive {self.hive_id} {name} set to {is_on}")

    PERTURBATION_LIMITS = {
        "ext_temp_offset": (-20.0, 20.0),
        "humidity_offset": (0.0, 30.0),
        "flight_factor": (0.0, 1.0),
    }

    def _on_perturbation_set(self, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            logger.warning(f"Hive {self.hive_id} ignored invalid perturbation payload: {payload!r}")
            return

        # Partial update: only the known fields present in the payload are merged
        for field, value in data.items():
            limits = self.PERTURBATION_LIMITS.get(field)
            valid = (
                limits is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            if not valid:
                logger.warning(f"Hive {self.hive_id} ignored perturbation field {field}={value!r}")
                continue
            self.perturbation[field] = max(limits[0], min(limits[1], float(value)))
        self._publish_perturbation_state()
        logger.info(f"Hive {self.hive_id} perturbation is now {self.perturbation}")

    def _publish_perturbation_state(self):
        self.mqtt.publish_json(
            f"sim/{self.hive_id}/perturbation/state", self.perturbation, qos=1, retain=True
        )

    def _on_sim_event(self, payload):
        try:
            event_type = json.loads(payload).get("type")
        except (ValueError, AttributeError):
            event_type = None
        if event_type == "swarm":
            self.swarm_ticks_left = self.swarm_ticks
            logger.info(f"Hive {self.hive_id} is swarming")
        else:
            logger.warning(f"Hive {self.hive_id} ignored sim event: {payload!r}")

    def tick(self, sim_minutes_per_tick):
        self.sim_time_minutes += sim_minutes_per_tick
        self._update_external_conditions()
        self._update_brood_temperature()
        self._update_humidity()
        self._update_weight(sim_minutes_per_tick)
        self._update_flights()
        self._publish_telemetry()

    def _update_external_conditions(self):
        # 24-hour cycle: 24 * 60 = 1440 minutes
        cycle_position = (self.sim_time_minutes % 1440) / 1440.0
        # Base 18°C, amplitude 8°C, peak in early afternoon
        angle = 2 * math.pi * (cycle_position - 0.25)
        ext_temp_base = 18.0 + 8.0 * math.sin(angle)
        # Small noise
        ext_temp_noise = random.gauss(0, 0.5)
        self.ext_temp = ext_temp_base + ext_temp_noise + self.perturbation["ext_temp_offset"]

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
            # Thermoregulation holds the setpoint inside the comfort band; outside it
            # the colony cannot compensate and the brood follows the weather.
            target = self.brood_setpoint
            if self.ext_temp < self.brood_comfort_min:
                target -= self.brood_cold_gain * (self.brood_comfort_min - self.ext_temp)
            elif self.ext_temp > self.brood_comfort_max:
                target += self.brood_heat_gain * (self.ext_temp - self.brood_comfort_max)

        noise = random.gauss(0, self.brood_noise)
        self.brood_temp += (target - self.brood_temp) * self.temp_inercia + noise
        self.brood_temp = max(20, min(40, self.brood_temp))

    def _update_humidity(self):
        target_hum = (
            self.humidity_baseline
            + 0.2 * (self.ext_hum - self.humidity_baseline)
            + self.perturbation["humidity_offset"]
        )
        if self.fan_on:
            # The fan removes a fixed share of moisture: with a strong source it is not enough
            target_hum -= self.humidity_fan_drop

        noise = random.gauss(0, self.humidity_noise)
        self.humidity += (target_hum - self.humidity) * self.humidity_inercia + noise
        self.humidity = max(30, min(90, self.humidity))

    def _update_weight(self, sim_minutes_per_tick):
        ticks_per_sim_day = 1440 / sim_minutes_per_tick
        cycle_position = (self.sim_time_minutes % 1440) / 1440.0
        # Day is roughly 6:00 to 18:00 (0.25 to 0.75 of cycle)
        is_daytime = 0.25 < cycle_position < 0.75

        if is_daytime and self.ext_temp > 15:
            # Small daily gain during foraging hours
            self.weight += self.weight_daily_growth / ticks_per_sim_day
        else:
            # Small nightly decline (consumption)
            self.weight -= self.weight_daily_decline / ticks_per_sim_day

        if self.swarm_ticks_left > 0:
            # A swarm leaves with part of the colony: one-off loss spread over a few ticks
            self.weight -= self.swarm_weight_loss / self.swarm_ticks
            self.swarm_ticks_left -= 1

        self.weight = max(10, min(60, self.weight))

    def _update_flights(self):
        # Flights depend on external conditions
        if self.ext_temp < 12 or self.ext_hum > 85:
            self.flights = 0
        else:
                # Scale with temperature (better at 20-25°C) and inverse of humidity
            temp_factor = max(0, 1.0 - abs(self.ext_temp - 22.5) / 15.0)
            hum_factor = max(0, 1.0 - (self.ext_hum - 50.0) / 40.0)
            base_flights = 100 * temp_factor * hum_factor
            flights = base_flights + random.gauss(0, 10)
            self.flights = max(0, int(flights * self.perturbation["flight_factor"]))

    def _publish_telemetry(self):
        timestamp = self._get_timestamp()
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

    def _get_timestamp(self):
        # Wall-clock time, like a real sensor: sim_time_minutes only drives the
        # accelerated physics, while "ts" must stay comparable with live data and backfill.
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def shutdown(self):
        # Publish offline status explicitly (before LWT takes over)
        self.mqtt.publish(
            f"apiary/{self.hive_id}/status", "offline", qos=1, retain=True
        )
        self.mqtt.disconnect()
        logger.info(f"Hive {self.hive_id} shutdown complete")
