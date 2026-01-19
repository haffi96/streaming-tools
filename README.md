Python tools for streaming and parsing H.264 streams with SEI timestamp metadata.

Gstreamer stream:

Usage:
    # File output (for testing with parse_h264_sei.py)
    uv run python gstreamer_source_sei.py --output file --path test.h264 --duration 5

    # TCP output (for live streaming tests)
    uv run python gstreamer_source_sei.py --output tcp --port 5004

    # Camera source instead of test pattern
    uv run python gstreamer_source_sei.py --camera --output file --path camera.h264



Parse H.264 stream:

Usage:
    # File mode
    uv run python parse_h264_sei.py samples/annux-d-sample_timestamp_sei.h264

    # TCP mode (connect to streaming source)
    uv run python parse_h264_sei.py --tcp --host localhost --port 5004


View the stream or file:
    # Using ffplay
    ffplay -i ./samples/generated_sei.h264


Pre-requisites:
- GStreamer
- Gstream plugins
- Python 3.10+
- [uv (optional)](https://docs.astral.sh/uv/getting-started/installation/), can also just run with pip and a venv





