import asyncio
import contextlib
import socket
import struct
import time
from typing import Any

from .protocol import MAX_MESSAGE_SIZE, ProtocolError, decode_message, encode_message


class UdpProbeProtocol(asyncio.DatagramProtocol):
    def __init__(self, token: str) -> None:
        self.token = token
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        try:
            request = decode_message(data, self.token)
            response = _response_for(request)
            encoded = encode_message(response, self.token)
        except ProtocolError:
            return
        if self.transport is not None:
            self.transport.sendto(encoded, address)


class ProbeServer:
    def __init__(self, bind: str, tcp_port: int, udp_port: int, token: str) -> None:
        self.bind = bind
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.token = token
        self._tcp_server: asyncio.Server | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        self._tcp_server = await asyncio.start_server(
            self._handle_tcp, self.bind, self.tcp_port, family=socket.AF_INET
        )
        tcp_socket = self._tcp_server.sockets[0]
        self.tcp_port = tcp_socket.getsockname()[1]

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: UdpProbeProtocol(self.token),
            local_addr=(self.bind, self.udp_port),
            family=socket.AF_INET,
        )
        self._udp_transport = transport
        self.udp_port = transport.get_extra_info("sockname")[1]

    async def serve_forever(self) -> None:
        if self._tcp_server is None:
            raise RuntimeError("server has not been started")
        async with self._tcp_server:
            await self._tcp_server.serve_forever()

    async def close(self) -> None:
        if self._udp_transport is not None:
            self._udp_transport.close()
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(4)
                length = struct.unpack("!I", header)[0]
                if length > MAX_MESSAGE_SIZE:
                    raise ProtocolError("message is too large")
                request = decode_message(await reader.readexactly(length), self.token)
                response = encode_message(_response_for(request), self.token)
                writer.write(struct.pack("!I", len(response)) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


def _response_for(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("kind") != "probe":
        raise ProtocolError("unexpected message kind")
    if not isinstance(request.get("session"), str) or not isinstance(
        request.get("seq"), int
    ):
        raise ProtocolError("invalid probe fields")
    return {
        "kind": "reply",
        "session": request["session"],
        "seq": request["seq"],
        "sent_ns": request.get("sent_ns"),
        "received_ns": time.time_ns(),
    }
