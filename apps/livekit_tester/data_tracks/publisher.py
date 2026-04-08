import os
import logging
import asyncio
import time
from signal import SIGINT, SIGTERM
from dotenv import load_dotenv
from livekit import rtc
import msgspec

from common.auth import generate_token
from common.data import encode_ping, decode_ping, now_time_micros

load_dotenv()
# ensure LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are set in your .env file

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
ROOM_NAME = os.environ["ROOM_NAME"]

PING_TRACK_NAME = "rtt_ping"
ECHO_TRACK_NAME = "rtt_echo"
PING_INTERVAL_SECONDS = 0.5


async def push_frames(track: rtc.LocalDataTrack):
    seq = 0
    while True:
        sent_at_us = now_time_micros()
        try:
            frame = rtc.DataTrackFrame(
                payload=encode_ping(seq, sent_at_us),
                user_timestamp=int(time.time() * 1_000_000),
            )
            track.try_push(frame)
            logging.info("Sent ping seq=%d", seq)
        except rtc.PushFrameError as e:
            logging.error("Failed to push frame: %s", e)
        seq += 1
        await asyncio.sleep(PING_INTERVAL_SECONDS)


async def subscribe(track: rtc.RemoteDataTrack):
    logging.info(
        "Subscribing to '%s' published by '%s'",
        track.info.name,
        track.publisher_identity,
    )
    try:
        async for frame in track.subscribe():
            try:
                ping = decode_ping(frame.payload)
            except (KeyError, TypeError, ValueError, msgspec.DecodeError) as exc:
                logging.warning("Ignoring malformed echo frame: %s", exc)
                continue

            rtt_ms = (now_time_micros() - ping["sent_at_us"]) / 1000.0
            logging.info("RTT seq=%d: %.3f ms", ping["seq"], rtt_ms)
    except rtc.SubscribeDataTrackError as e:
        logging.error("Failed to subscribe to '%s': %s", track.info.name, e.message)


async def main(room: rtc.Room):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    active_tasks = []

    def remove_task(done: asyncio.Task):
        if done in active_tasks:
            active_tasks.remove(done)

    @room.on("data_track_published")
    def on_data_track_published(track: rtc.RemoteDataTrack):
        if track.info.name != ECHO_TRACK_NAME:
            return

        task = asyncio.create_task(subscribe(track))
        active_tasks.append(task)
        task.add_done_callback(remove_task)

    try:
        token = generate_token(ROOM_NAME, "publisher", "TelemetryPublisher")
        await room.connect(LIVEKIT_URL, token)
        logger.info("connected to room %s", room.name)

        track = await room.local_participant.publish_data_track(name=PING_TRACK_NAME)
        logger.info("published ping track '%s'", PING_TRACK_NAME)
        await push_frames(track)
    finally:
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            # logging.FileHandler("publisher.log"),
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
