import os
import signal
import logging
import time
from .hive import Hive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    # Configuration from environment
    mqtt_host = os.getenv("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    hive_ids_str = os.getenv("HIVE_IDS", "hive-01,hive-02")
    tick_seconds = int(os.getenv("TICK_SECONDS", "5"))
    sim_minutes_per_tick = int(os.getenv("SIM_MINUTES_PER_TICK", "15"))

    hive_ids = [hid.strip() for hid in hive_ids_str.split(",")]

    logger.info(
        f"Starting simulator with {len(hive_ids)} hives: {hive_ids}"
    )
    logger.info(
        f"MQTT: {mqtt_host}:{mqtt_port}, "
        f"tick={tick_seconds}s, sim_time={sim_minutes_per_tick}min/tick"
    )

    # Initialize hives
    hives = []
    for hive_id in hive_ids:
        try:
            hive = Hive(hive_id, mqtt_host=mqtt_host, mqtt_port=mqtt_port)
            hives.append(hive)
            time.sleep(0.5)  # Stagger connections
        except Exception as e:
            logger.error(f"Failed to initialize hive {hive_id}: {e}")
            return 1

    # Graceful shutdown handler
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal, cleaning up...")
        for hive in hives:
            try:
                hive.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down {hive.hive_id}: {e}")
        logger.info("Simulator shutdown complete")
        exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Main simulation loop
    try:
        logger.info("Starting simulation loop")
        while True:
            for hive in hives:
                try:
                    hive.tick(sim_minutes_per_tick)
                except Exception as e:
                    logger.error(f"Error during tick for {hive.hive_id}: {e}")

            time.sleep(tick_seconds)
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    exit(main())
