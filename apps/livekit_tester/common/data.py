import time

import msgspec


def monotonic_us() -> int:
    return time.monotonic_ns() // 1_000

def now_time_micros() -> int:
    return int(time.time() * 1_000_000)


def encode_ping(seq: int, sent_at_us: int) -> bytes:
    return msgspec.json.encode({"seq": seq, "sent_at_us": sent_at_us})


def decode_ping(payload: bytes) -> dict[str, int]:
    message = msgspec.json.decode(payload)
    return {
        "seq": int(message["seq"]),
        "sent_at_us": int(message["sent_at_us"]),
    }