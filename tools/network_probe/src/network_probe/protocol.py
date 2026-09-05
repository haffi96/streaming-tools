import hashlib
import hmac
import json
from typing import Any

VERSION = 1
MAX_MESSAGE_SIZE = 65_507


class ProtocolError(ValueError):
    pass


def encode_message(
    fields: dict[str, Any], token: str, size: int | None = None
) -> bytes:
    message = {"v": VERSION, **fields}
    if size is not None:
        message["padding"] = ""
        unsigned = _serialize(message)
        # The MAC field adds 73 bytes with compact JSON encoding.
        padding_size = size - len(unsigned) - 73
        if padding_size < 0:
            raise ProtocolError(f"packet size must be at least {len(unsigned) + 73}")
        message["padding"] = "x" * padding_size

    signature = hmac.new(
        token.encode(), _serialize(message), hashlib.sha256
    ).hexdigest()
    encoded = _serialize({**message, "mac": signature})
    if len(encoded) > MAX_MESSAGE_SIZE:
        raise ProtocolError(f"message exceeds {MAX_MESSAGE_SIZE} bytes")
    return encoded


def decode_message(data: bytes, token: str) -> dict[str, Any]:
    try:
        message = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("message is not valid JSON") from error

    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    signature = message.pop("mac", None)
    if not isinstance(signature, str):
        raise ProtocolError("message has no signature")
    expected = hmac.new(token.encode(), _serialize(message), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ProtocolError("message signature is invalid")
    if message.get("v") != VERSION:
        raise ProtocolError("unsupported protocol version")
    return message


def _serialize(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
