import os
import logging
import asyncio
from signal import SIGINT, SIGTERM
from dotenv import load_dotenv
from livekit import rtc

from common.auth import generate_token
from common.data import now_time_micros, decode_ping

load_dotenv()
# ensure LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are set in your .env file
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
ROOM_NAME = os.environ["ROOM_NAME"]

PING_TOPIC_NAME = "rtt_ping"
ECHO_TOPIC_NAME = "rtt_echo"



async def main(room: rtc.Room):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    @room.on("data_received")
    def on_data_received(data: rtc.DataPacket):
        if data.topic != PING_TOPIC_NAME:
            return
        

        ping = decode_ping(data.data)

        logging.info(
                "Received ping seq=%d (%d bytes)",
                ping["seq"],
                len(data.data),
            )

        latency = (now_time_micros() - ping["sent_at_us"]) / 1000.0

        logging.info("One-way latency: %.3f ms", latency)

        asyncio.create_task(room.local_participant.publish_data(payload=data.data, topic=ECHO_TOPIC_NAME))

    token = generate_token(ROOM_NAME, "subscriber", "Telemetry Subscriber")
    await room.connect(LIVEKIT_URL, token)
    logger.info("connected to room %s", room.name)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
        ],
    )

    loop = asyncio.get_event_loop()
    room = rtc.Room(loop=loop)

    main_task = asyncio.ensure_future(main(room))

    async def cleanup():
        main_task.cancel()
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        await room.disconnect()
        loop.stop()

    for signal in [SIGINT, SIGTERM]:
        loop.add_signal_handler(signal, lambda: asyncio.ensure_future(cleanup()))

    try:
        loop.run_forever()
    finally:
        loop.close()
