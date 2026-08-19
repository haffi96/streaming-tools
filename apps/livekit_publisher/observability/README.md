# Local observability

Start the Collector, Prometheus, and Grafana:

```bash
docker compose up -d
```

Export publisher metrics to the Collector:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_INSECURE=true
export OTEL_METRIC_EXPORT_INTERVAL=5000
```

Open Grafana at <http://localhost:3000> (`admin` / `admin`) and Prometheus at
<http://localhost:9090>. The **LiveKit Publisher** dashboard is provisioned in
the LiveKit folder.

Stop the stack with `docker compose down`. Add `-v` only when you also want to
delete the local Grafana and Prometheus data volumes.
