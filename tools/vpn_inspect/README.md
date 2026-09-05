# VPN Inspect

Inspect the route from the local machine to a VPN peer. The report includes
interfaces and MTUs, the source address and interface selected by the kernel,
the operating-system route lookup, ICMP reachability, non-fragmenting ICMP
packet-size checks, and traceroute.

```bash
uv run --package vpn-inspect vpn-inspect operator.vpn.example --json vpn-path.json
```

Capture the inspection traffic when running with sufficient privileges:

```bash
sudo uv run --package vpn-inspect vpn-inspect operator.vpn.example --pcap vpn-path.pcap
```

Useful options:

```bash
# Skip traceroute on networks where it is known to be filtered.
uv run --package vpn-inspect vpn-inspect 10.20.30.40 --no-traceroute

# Test packet sizes suited to the expected IPsec tunnel MTU.
uv run --package vpn-inspect vpn-inspect 10.20.30.40 \
  --packet-sizes 1000,1200,1300,1400

# Emit only JSON to stdout.
uv run --package vpn-inspect vpn-inspect 10.20.30.40 --json -
```

ICMP and traceroute failures are warnings because VPN policies may block them
while allowing application traffic. Use `network-probe` on the intended TCP
and UDP ports for the authoritative reachability result.

Interface detection recognizes common `utun`, `tun`, `tap`, `ppp`, WireGuard,
and IPsec names. A differently named corporate VPN interface can still be
identified by matching its IPv4 address; it will only be described as an
unrecognized VPN interface.

When your current directory is `tools/vpn_inspect`, the shorter
`uv run vpn-inspect` command is sufficient.
