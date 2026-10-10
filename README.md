# Network Device Monitor

A small full-stack monitoring app. A Flask REST API checks whether network devices and services are reachable over TCP, stores every result in SQLite, and a dashboard shows live status, latency and uptime.

I built it to practise what a NOC does every day (watching reachability, spotting failures and their cause) as a backend, API and deployment project.

**Live demo:** _add your Render URL here after deploying_

## Features

- Add or remove devices (name, host, port) from the dashboard or the API
- Background checker probes every device on an interval (default 30 s) and keeps up to 500 results per device
- Dashboard: Up / Down summary, latency, uptime over the last 100 checks, time since last check, failure reason (DNS failure, timeout, connection refused)
- JSON REST API with input validation and clear error codes (400, 403, 404, 409, 429)
- Read-only mode for public demos
- Unit tests, Dockerfile and GitHub Actions CI

## Architecture

```
Browser (static HTML/CSS/JS)
        |  fetch() every 10 s
        v
Flask REST API  ----------------->  SQLite (devices, checks)
        ^
        |  every CHECK_INTERVAL seconds
Background checker thread  --->  TCP connect to each device (thread pool)
```

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/api/config` | Read-only flag, check interval, device limit |
| GET | `/api/devices` | All devices with status, latency, uptime, last error |
| POST | `/api/devices` | Add a device: `{"name": "...", "host": "...", "port": 443}` |
| DELETE | `/api/devices/<id>` | Remove a device |
| GET | `/api/devices/<id>/history?limit=50` | Recent check results |
| GET | `/api/summary` | Counts of total / up / down / unknown |
| POST | `/api/check` | Run all checks now (10 s cooldown) |

Example:

```bash
curl -X POST http://127.0.0.1:5000/api/devices \
  -H "Content-Type: application/json" \
  -d '{"name": "GitHub", "host": "github.com", "port": 443}'
```

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                    # http://127.0.0.1:5000
```

Run the tests (they use a local TCP server, no internet needed):

```bash
python -m unittest discover -s tests -v
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CHECK_INTERVAL` | 30 | Seconds between automatic checks |
| `CHECK_TIMEOUT` | 3 | TCP connect timeout in seconds |
| `MAX_DEVICES` | 20 | Maximum monitored devices |
| `READ_ONLY` | off | `1` disables add/remove (use for public demos) |
| `ALLOW_PRIVATE` | off | `1` allows private/loopback targets (for monitoring a home or lab network) |
| `DB_PATH` | monitor.db | SQLite file location |

## Deploy

**Render (free tier):** push to GitHub, then create a new Web Service from the repo. `render.yaml` already sets the build and start commands and `READ_ONLY=1`. The free tier has an ephemeral disk, so history resets on redeploy and the demo devices are re-seeded.

**Docker:**

```bash
docker build -t network-device-monitor .
docker run -p 8000:8000 network-device-monitor
```

## Design decisions

- **TCP connect checks instead of ICMP ping.** ICMP needs raw-socket privileges that most hosting platforms and containers do not give. A TCP connect works anywhere and tests the actual service port, not just the host.
- **Failure reasons are kept separate.** DNS failure, timeout and connection refused point to different problems (resolver, filtering or path, service down), so the dashboard shows which one it was.
- **SSRF protection.** A public monitor that probes whatever host users type could be used to scan internal networks. Hosts are resolved on every check and private, loopback and other non-global addresses are refused unless `ALLOW_PRIVATE=1`.
- **Abuse limits.** Device cap, strict host and port validation, a cooldown on manual checks, and `READ_ONLY` mode for the public demo.
- **Browser hardening.** The dashboard uses no inline scripts or styles, sets a strict Content-Security-Policy, and renders data with `textContent` (no HTML injection).
- **One gunicorn worker.** The checker runs as a thread inside the app, so a single worker with several threads avoids duplicate checkers.
- **SQLite.** Enough for a small tool and keeps setup simple.

## Limitations and next steps

- IPv4 only; TCP checks only (no HTTP status or TLS certificate checks yet)
- No user login; write access is controlled by `READ_ONLY`
- Possible additions: alerts on status change (email or Telegram), HTTP and TLS-expiry checks, response-time charts, PostgreSQL for persistent history
