import asyncio
import unittest

from network_probe.client import run_probe
from network_probe.protocol import ProtocolError, decode_message, encode_message
from network_probe.server import ProbeServer


class ProtocolTests(unittest.TestCase):
    def test_round_trip_and_requested_size(self) -> None:
        encoded = encode_message({"kind": "probe", "seq": 1}, "secret", 256)

        self.assertEqual(len(encoded), 256)
        self.assertEqual(decode_message(encoded, "secret")["seq"], 1)

    def test_rejects_wrong_token(self) -> None:
        encoded = encode_message({"kind": "probe", "seq": 1}, "secret")

        with self.assertRaises(ProtocolError):
            decode_message(encoded, "wrong")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = ProbeServer("127.0.0.1", 0, 0, "test-token")
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.close()

    async def test_tcp_and_udp_probe(self) -> None:
        report = await asyncio.to_thread(
            run_probe,
            "127.0.0.1",
            self.server.tcp_port,
            self.server.udp_port,
            "test-token",
            3,
            0,
            1,
            256,
        )

        self.assertTrue(report["success"])
        self.assertEqual(report["tcp"]["received"], 3)
        self.assertEqual(report["udp"]["received"], 3)
        self.assertEqual(report["tcp"]["loss_percent"], 0)
        self.assertEqual(report["udp"]["loss_percent"], 0)
        self.assertEqual(report["source_address"], "127.0.0.1")
