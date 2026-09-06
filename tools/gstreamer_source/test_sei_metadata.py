import struct
import unittest

from gstreamer_source.parse import parse_packet_trailer, parse_user_data
from gstreamer_source.sei import SEI_UUID, append_packet_trailer, create_sei_nalu


class SeiMetadataTests(unittest.TestCase):
    def test_append_packet_trailer_round_trip(self):
        timestamp_us = 1_746_000_000_123_456
        frame_id = 123
        trailer = append_packet_trailer(timestamp_us, frame_id)
        metadata = parse_packet_trailer(trailer)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["timestamp_us"], timestamp_us)
        self.assertEqual(metadata["frame_id"], frame_id)

    def test_create_sei_nalu_annex_b_contains_lkts_trailer(self):
        timestamp_us = 42
        sei = create_sei_nalu(timestamp_us=timestamp_us, stream_format="byte-stream")
        self.assertTrue(sei.startswith(b"\x00\x00\x00\x01"))
        payload = sei[5:]
        user_data = payload[2:-1]
        parsed = parse_user_data(user_data[:16], user_data[16:])
        self.assertEqual(parsed, (timestamp_us, "lkts"))

    def test_create_sei_nalu_annex_b_contains_frame_id(self):
        timestamp_us = 42
        frame_id = 7
        sei = create_sei_nalu(
            timestamp_us=timestamp_us,
            frame_id=frame_id,
            stream_format="byte-stream",
        )
        payload = sei[5:]
        user_data = payload[2:-1]
        metadata = parse_packet_trailer(user_data[16:])
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["timestamp_us"], timestamp_us)
        self.assertEqual(metadata["frame_id"], frame_id)

    def test_legacy_payload_still_parses(self):
        timestamp_us = 99
        legacy_payload = struct.pack(">Q", timestamp_us)
        parsed = parse_user_data(SEI_UUID, legacy_payload)
        self.assertEqual(parsed, (timestamp_us, "legacy"))


if __name__ == "__main__":
    unittest.main()


class TimestampOverlayTests(unittest.TestCase):
    def test_format_clock_ms(self):
        from gstreamer_source.overlay import format_clock_ms

        self.assertEqual(format_clock_ms(0), "0:00:00.000")
        self.assertEqual(format_clock_ms(1_522), "0:00:01.522")
        self.assertEqual(format_clock_ms(61_005), "0:01:01.005")
        self.assertEqual(format_clock_ms(3_661_999), "1:01:01.999")
