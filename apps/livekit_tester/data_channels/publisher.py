import os
import logging
import asyncio
import time
from signal import SIGINT, SIGTERM
from dotenv import load_dotenv
from livekit import rtc
import msgspec

from common.auth import generate_token

load_dotenv()
# ensure LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are set in your .env file

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
ROOM_NAME = os.environ["ROOM_NAME"]

PING_TOPIC_NAME = "rtt_ping"
ECHO_TOPIC_NAME = "rtt_echo"
PING_INTERVAL_SECONDS = 0.5


def monotonic_us() -> int:
    return time.monotonic_ns() // 1_000


def encode_ping(seq: int, sent_at_us: int) -> bytes:
    return msgspec.json.encode({"seq": seq, "sent_at_us": sent_at_us})


def decode_ping(payload: bytes) -> dict[str, int]:
    message = msgspec.json.decode(payload)
    return {
        "seq": int(message["seq"]),
        "sent_at_us": int(message["sent_at_us"]),
    }


async def main(room: rtc.Room):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    active_tasks = []

    def remove_task(done: asyncio.Task):
        if done in active_tasks:
            active_tasks.remove(done)

    async def _send_telemetry() -> None:
        seq = 0
        while True:
            sent_at_us = monotonic_us()

            try:
                await room.local_participant.publish_data(
                    payload=encode_ping(seq, sent_at_us),
                    topic=PING_TOPIC_NAME,
                    reliable=False,
                )
                logging.info("Sent ping seq=%d", seq)
            except Exception:
                logging.error("failed to send telemetry", exc_info=True)
                continue
            finally:
                seq += 1
                await asyncio.sleep(0.5)
            

    @room.on("data_received")
    def on_data_received(data: rtc.DataPacket):
        if data.topic != ECHO_TOPIC_NAME:
            return
        

        ping = decode_ping(data.data)

        rtt_ms = (monotonic_us() - ping["sent_at_us"]) / 1000.0

        logging.info("RTT seq=%d: %.3f ms", ping["seq"], rtt_ms)

    try:
        token = generate_token(ROOM_NAME, "publisher", "TelemetryPublisher")
        await room.connect(LIVEKIT_URL, token)
        logger.info("connected to room %s", room.name)

        # Start publishing task
        publish_task = asyncio.create_task(_send_telemetry())
        active_tasks.append(publish_task)
        publish_task.add_done_callback(remove_task)
        await asyncio.Future() 
    finally:
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)


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
