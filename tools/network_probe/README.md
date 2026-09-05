# Network Probe

Authenticated TCP and UDP reachability checks between two machines on the same
VPN. The tool uses ordinary IPv4 sockets and does not require public addresses,
STUN, or a coordination service.

## Usage

Choose a shared token and start the listener on the operator workstation:

```bash
export NETWORK_PROBE_TOKEN="replace-with-a-random-shared-token"
uv run --package network-probe network-probe serve \
  --bind 0.0.0.0 \
  --tcp-port 45000 \
  --udp-port 45001
```

Run the test from the AV using the operator's VPN hostname or IP address:

```bash
export NETWORK_PROBE_TOKEN="replace-with-a-random-shared-token"
uv run --package network-probe network-probe test operator.vpn.example \
  --tcp-port 45000 \
  --udp-port 45001 \
  --count 10 \
  --json av-to-operator.json
```

Run the listener on the AV and repeat the test from the operator to detect
asymmetric VPN or host-firewall policies.

If `serve` receives no token, it generates and prints one. Test mode always
requires `--token` or `NETWORK_PROBE_TOKEN`. Invalid probe packets receive no
reply.

## Results

The test reports the resolved peer address, selected local source address,
reachability, replies, packet loss, round-trip latency, and RTT variation for
TCP and UDP independently. A successful result means at least one valid reply
was received for both protocols. Partial loss is visible but does not make the
command fail.

Exit status is `0` when both protocols respond and `1` otherwise. Use
`--json -` for JSON on stdout.

Run `uv run --package network-probe network-probe --help` for all options. When
your current directory is `tools/network_probe`, `uv run network-probe` is
sufficient.
