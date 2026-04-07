# Canopy Traffic Tool (Standalone)

Standalone monitor for observing Canopy runtime activity and endpoint health without modifying the Canopy instance.

## What it tracks

The tool polls a target Canopy instance and stores rolling telemetry samples:

- API probe health/latency/response size (`health`, `p2p_status`, `system_info`, `relay_status`)
- Database activity deltas (messages/feed posts) when API key allows `/api/v1/info`
- P2P connectivity trends (connected peers, discovered peers, relay routes)
- Composite `traffic_score` signal for quick anomaly scanning

## Run

From repository root:

```bash
python traffic_tool/app.py --target http://127.0.0.1:7770 --port 9095
```

Optional API key (recommended for richer data):

```bash
python traffic_tool/app.py \
  --target http://127.0.0.1:7770 \
  --api-key YOUR_CANOPY_API_KEY \
  --poll-seconds 5 \
  --retention-minutes 1440 \
  --port 9095
```

Open:

- `http://127.0.0.1:9095`

## Environment variables

- `CANOPY_MONITOR_TARGET` (default `http://127.0.0.1:7770`)
- `CANOPY_MONITOR_API_KEY` (optional)
- `CANOPY_MONITOR_POLL_SECONDS` (default `5`)
- `CANOPY_MONITOR_RETENTION_MINUTES` (default `1440`)
- `CANOPY_MONITOR_HOST` (default `127.0.0.1`)
- `CANOPY_MONITOR_PORT` (default `9095`)

## JSON endpoints

- `GET /api/metrics/config`
- `GET /api/metrics/latest`
- `GET /api/metrics/samples?window_minutes=60&limit=200`
- `GET /api/metrics/timeseries?metric=traffic_score&window_minutes=60&bucket_seconds=60`
- `GET /api/metrics/breakdown?window_minutes=60`
- `GET /api/metrics/dashboard?metric=traffic_score&window_minutes=60&bucket_seconds=60`
- `POST /api/metrics/refresh`

## Notes / limitations

- This is a standalone sidecar monitor, not an embedded Canopy module.
- It observes activity by polling exposed Canopy endpoints; it does not packet-sniff or hook internal request middleware.
- Without an API key, key-protected probes are skipped and marked in the dashboard.
