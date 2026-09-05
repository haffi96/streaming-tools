import secrets
import socket
import statistics
import struct
import time
from itertools import pairwise
from typing import Any

from .protocol import MAX_MESSAGE_SIZE, ProtocolError, decode_message, encode_message


def run_probe(
    host: str,
    tcp_port: int,
    udp_port: int,
    token: str,
    count: int = 5,
    interval: float = 0.2,
    timeout: float = 2.0,
    packet_size: int = 256,
) -> dict[str, Any]:
    started = time.time()
    report: dict[str, Any] = {
        "target": host,
        "resolved_address": None,
        "source_address": None,
        "tcp": _empty_result(tcp_port),
        "udp": _empty_result(udp_port),
        "started_at": _iso_time(started),
    }
    try:
        address = socket.gethostbyname(host)
        report["resolved_address"] = address
        report["source_address"] = select_source_address(address, udp_port)
    except OSError as error:
        message = f"name or route resolution failed: {error}"
        report["tcp"]["error"] = message
        report["udp"]["error"] = message
        return _finish(report, started)

    report["tcp"] = _probe_tcp(
        address, tcp_port, token, count, interval, timeout, packet_size
    )
    report["udp"] = _probe_udp(
        address, udp_port, token, count, interval, timeout, packet_size
    )
    return _finish(report, started)


def select_source_address(address: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((address, port))
        return sock.getsockname()[0]


def _probe_tcp(
    address: str,
    port: int,
    token: str,
    count: int,
    interval: float,
    timeout: float,
    packet_size: int,
) -> dict[str, Any]:
    result = _empty_result(port)
    session = secrets.token_hex(8)
    latencies: list[float] = []
    try:
        with socket.create_connection((address, port), timeout=timeout) as sock:
            result["source_address"] = sock.getsockname()[0]
            for seq in range(count):
                started = time.perf_counter_ns()
                request = _request(session, seq)
                encoded = encode_message(request, token, packet_size)
                sock.sendall(struct.pack("!I", len(encoded)) + encoded)
                length = struct.unpack("!I", _receive_exact(sock, 4))[0]
                if length > MAX_MESSAGE_SIZE:
                    raise ProtocolError("reply is too large")
                reply = decode_message(_receive_exact(sock, length), token)
                _validate_reply(reply, session, seq)
                latencies.append((time.perf_counter_ns() - started) / 1_000_000)
                if seq + 1 < count:
                    time.sleep(interval)
    except (OSError, ProtocolError, struct.error) as error:
        result["error"] = str(error)
    return _complete_result(result, count, latencies)


def _probe_udp(
    address: str,
    port: int,
    token: str,
    count: int,
    interval: float,
    timeout: float,
    packet_size: int,
) -> dict[str, Any]:
    result = _empty_result(port)
    session = secrets.token_hex(8)
    latencies: list[float] = []
    errors: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((address, port))
            result["source_address"] = sock.getsockname()[0]
        except OSError as error:
            result["error"] = str(error)
            return _complete_result(result, count, latencies)

        for seq in range(count):
            try:
                started = time.perf_counter_ns()
                sock.send(encode_message(_request(session, seq), token, packet_size))
                reply = decode_message(sock.recv(MAX_MESSAGE_SIZE), token)
                _validate_reply(reply, session, seq)
                latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            except (OSError, ProtocolError) as error:
                errors.append(str(error))
            if seq + 1 < count:
                time.sleep(interval)
    if errors:
        result["error"] = errors[-1]
    return _complete_result(result, count, latencies)


def _request(session: str, seq: int) -> dict[str, Any]:
    return {
        "kind": "probe",
        "session": session,
        "seq": seq,
        "sent_ns": time.time_ns(),
    }


def _validate_reply(reply: dict[str, Any], session: str, seq: int) -> None:
    if (
        reply.get("kind") != "reply"
        or reply.get("session") != session
        or reply.get("seq") != seq
    ):
        raise ProtocolError("reply does not match request")


def _receive_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise ProtocolError("connection closed before complete reply")
        chunks.extend(chunk)
    return bytes(chunks)


def _empty_result(port: int) -> dict[str, Any]:
    return {
        "port": port,
        "reachable": False,
        "sent": 0,
        "received": 0,
        "loss_percent": 100.0,
        "rtt_ms": None,
        "jitter_ms": None,
        "source_address": None,
        "error": None,
    }


def _complete_result(
    result: dict[str, Any], sent: int, latencies: list[float]
) -> dict[str, Any]:
    result["sent"] = sent
    result["received"] = len(latencies)
    result["reachable"] = bool(latencies)
    result["loss_percent"] = round((sent - len(latencies)) / sent * 100, 2)
    if latencies:
        result["rtt_ms"] = {
            "min": round(min(latencies), 3),
            "average": round(statistics.fmean(latencies), 3),
            "max": round(max(latencies), 3),
        }
        differences = [abs(b - a) for a, b in pairwise(latencies)]
        result["jitter_ms"] = (
            round(statistics.fmean(differences), 3) if differences else 0.0
        )
        if len(latencies) == sent:
            result["error"] = None
    return result


def _finish(report: dict[str, Any], started: float) -> dict[str, Any]:
    report["duration_seconds"] = round(time.time() - started, 3)
    report["success"] = report["tcp"]["reachable"] and report["udp"]["reachable"]
    return report


def _iso_time(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
