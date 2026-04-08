import os
import logging
import asyncio
import json
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

PING_TRACK_NAME = "rtt_ping"
ECHO_TRACK_NAME = "rtt_echo"


def decode_ping(payload: bytes) -> dict[str, int]:
    message = msgspec.json.decode(payload)
    return {
        "seq": int(message["seq"]),
        "sent_at_us": int(message["sent_at_us"]),
    }


async def subscribe(
    track: rtc.RemoteDataTrack,
    echo_track_ready: asyncio.Event,
    echo_track_holder: dict[str, rtc.LocalDataTrack | None],
):
    logging.info(
        "Subscribing to '%s' published by '%s'",
        track.info.name,
        track.publisher_identity,
    )
    try:
        await echo_track_ready.wait()
        echo_track = echo_track_holder["track"]
        if echo_track is None:
            logging.error("Echo track was not available for subscription")
            return

        async for frame in track.subscribe():
            try:
                ping = decode_ping(frame.payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logging.warning("Ignoring malformed ping frame: %s", exc)
                continue

            logging.info(
                "Received ping seq=%d (%d bytes)",
                ping["seq"],
                len(frame.payload),
            )

            if frame.user_timestamp is not None:
                latency = (
                    int(time.time() * 1_000_000) - frame.user_timestamp
                ) / 1_000_000.0
                logging.info("One-way latency: %.3f ms", latency * 1000.0)

            try:
                echo_track.try_push(
                    rtc.DataTrackFrame(
                        payload=frame.payload,
                        user_timestamp=int(time.time() * 1_000_000),
                    )
                )
                logging.info("Echoed ping seq=%d", ping["seq"])
            except rtc.PushFrameError as e:
                logging.error("Failed to echo ping seq=%d: %s", ping["seq"], e)
    except rtc.SubscribeDataTrackError as e:
        logging.error("Failed to subscribe to '%s': %s", track.info.name, e.message)


async def main(room: rtc.Room):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    active_tasks = []
    echo_track_holder: dict[str, rtc.LocalDataTrack | None] = {"track": None}
    echo_track_ready = asyncio.Event()

    def remove_task(done: asyncio.Task):
        if done in active_tasks:
            active_tasks.remove(done)

    @room.on("data_track_published")
    def on_data_track_published(track: rtc.RemoteDataTrack):
        if track.info.name != PING_TRACK_NAME:
            return

        task = asyncio.create_task(
            subscribe(track, echo_track_ready, echo_track_holder)
        )
        active_tasks.append(task)
        task.add_done_callback(remove_task)

    try:
        token = generate_token(ROOM_NAME, "subscriber", "Telemetry Subscriber")
        await room.connect(LIVEKIT_URL, token)
        logger.info("connected to room %s", room.name)
        echo_track_holder["track"] = await room.local_participant.publish_data_track(
            name=ECHO_TRACK_NAME
        )
        echo_track_ready.set()
        logger.info("published echo track '%s'", ECHO_TRACK_NAME)
        await asyncio.Future()
    finally:
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            # logging.FileHandler("subscriber.log"),
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
