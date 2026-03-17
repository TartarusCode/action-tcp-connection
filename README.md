# action-tcp-connection

GitHub Action to test TCP connectivity to one or more remote endpoints, with optional send/expect verification.

[![Github Action - TCP Test](https://github.com/BGarber42/action-tcp-connection/actions/workflows/main.yml/badge.svg)](https://github.com/BGarber42/action-tcp-connection/actions/workflows/main.yml)

## Features

- Single or multi-target connectivity checks in one step
- **Send/expect verification** — send a payload and assert the response matches a regex
- **Per-target overrides** — different send/expect for each target in a multi-target check
- Configurable retry strategy with exponential backoff
- Per-attempt socket timeout
- Latency measurement and structured JSON output
- GitHub annotations (`::notice::` / `::error::`) for PR-level visibility

## Quick start

```yaml
- name: Wait for database
  uses: BGarber42/action-tcp-connection@v1
  with:
    remotehost: 'localhost'
    remoteport: '5432'
```

## Inputs

| Name              | Required | Default | Description                                                              |
|-------------------|----------|---------|--------------------------------------------------------------------------|
| `remotehost`      | No*      |         | Hostname or IP to connect to                                             |
| `remoteport`      | No*      |         | Port to connect to (1-65535)                                             |
| `targets`         | No*      |         | Multiline target list with optional per-target overrides (see below)     |
| `send`            | No       |         | Default payload to send after connecting (supports `\r\n` escapes)       |
| `expect`          | No       |         | Default regex the response must match                                    |
| `expect_timeout`  | No       | `5`     | Seconds to wait for response data when using send/expect                 |
| `maxtime`         | No       | `60`    | Maximum total retry time in seconds                                      |
| `connect_timeout` | No       | `10`    | Per-attempt socket timeout in seconds                                    |
| `max_retries`     | No       | `0`     | Maximum retry attempts (0 = unlimited, bounded by `maxtime`)             |
| `retry_delay`     | No       | `1`     | Initial backoff delay in seconds                                         |

\* Either `remotehost` + `remoteport` **or** `targets` must be provided.

## Targets format

Each line in `targets` is a `host:port` endpoint. Optionally append `| send=...` and/or `| expect=...` to override the global `send`/`expect` for that specific target:

```
host:port
host:port | send=payload | expect=pattern
```

Lines without overrides inherit the global `send` and `expect` inputs. Lines with overrides use their own values instead.

## Outputs

| Name         | Description                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `result`     | Human-readable summary (e.g. `All 3 target(s) reachable in 142ms`)         |
| `latency_ms` | Total connection time in milliseconds (sum across all targets)             |
| `details`    | JSON array with per-target results (see below)                             |

### `details` schema

```json
[
  {
    "host": "api.example.com",
    "port": 80,
    "status": "success",
    "latency_ms": 42,
    "response": "HTTP/1.1 200 OK\r\n..."
  },
  {
    "host": "redis",
    "port": 6379,
    "status": "success",
    "latency_ms": 3,
    "response": "+PONG\r\n"
  },
  {
    "host": "1.1.1.1",
    "port": 8675,
    "status": "failure",
    "latency_ms": 5003,
    "error": "Connection failed: [Errno 111] Connection refused"
  }
]
```

The `response` field is included when `send` or `expect` is active for a target. It is truncated to 500 characters.

## Examples

### Simple connectivity check

```yaml
- name: Test connectivity
  uses: BGarber42/action-tcp-connection@v1
  id: tcp
  with:
    remotehost: '1.1.1.1'
    remoteport: '80'

- run: echo "Connected in ${{ steps.tcp.outputs.latency_ms }}ms"
```

### Multiple targets (connect only)

```yaml
- name: Verify all services are reachable
  uses: BGarber42/action-tcp-connection@v1
  with:
    targets: |
      db.example.com:5432
      redis:6379
      rabbitmq:5672
    maxtime: '120'
```

### HTTP health check (send/expect)

Send a raw HTTP request and verify the response starts with a 2xx or 3xx status:

```yaml
- name: Verify HTTP endpoint is healthy
  uses: BGarber42/action-tcp-connection@v1
  with:
    remotehost: 'api.example.com'
    remoteport: '80'
    send: 'GET /health HTTP/1.0\r\nHost: api.example.com\r\n\r\n'
    expect: 'HTTP/1\.[01] 200'
```

### Per-target send/expect (mixed protocols)

Check an HTTP API, a Redis cache, and a plain TCP database port in a single step, each with its own protocol verification:

```yaml
- name: Verify all services are healthy
  uses: BGarber42/action-tcp-connection@v1
  with:
    targets: |
      api.example.com:80 | send=GET /health HTTP/1.0\r\nHost: api.example.com\r\n\r\n | expect=HTTP/1\.[01] 200
      redis:6379 | send=PING\r\n | expect=\+PONG
      db.example.com:5432
    maxtime: '60'
```

In this example:
- The API target sends an HTTP GET and expects a `200` response
- The Redis target sends `PING` and expects `+PONG`
- The database target is connect-only (no send/expect)

### Global send/expect with per-target override

Set a default expectation for all targets, then override specific ones:

```yaml
- name: Check multiple HTTP endpoints
  uses: BGarber42/action-tcp-connection@v1
  with:
    send: 'GET / HTTP/1.0\r\nHost: example.com\r\n\r\n'
    expect: 'HTTP/1\.[01] [23]\d\d'
    targets: |
      web1.example.com:80
      web2.example.com:80
      api.example.com:80 | send=GET /health HTTP/1.0\r\nHost: api.example.com\r\n\r\n | expect=HTTP/1\.[01] 200
```

Here `web1` and `web2` use the global send/expect, while `api` overrides with its own.

### SMTP banner grab

Use `expect` without `send` to verify a service's banner:

```yaml
- name: Verify SMTP server
  uses: BGarber42/action-tcp-connection@v1
  with:
    remotehost: 'mail.example.com'
    remoteport: '25'
    expect: '^220 '
```

### MySQL protocol handshake

```yaml
- name: Verify MySQL is serving
  uses: BGarber42/action-tcp-connection@v1
  with:
    remotehost: 'localhost'
    remoteport: '3306'
    expect: 'mysql|MariaDB'
    maxtime: '60'
```

### Custom retry strategy

```yaml
- name: Wait for slow service
  uses: BGarber42/action-tcp-connection@v1
  with:
    remotehost: 'localhost'
    remoteport: '5432'
    maxtime: '120'
    connect_timeout: '5'
    max_retries: '20'
    retry_delay: '2'
```

### Use latency as a quality gate

```yaml
- name: Check API endpoint
  uses: BGarber42/action-tcp-connection@v1
  id: api
  with:
    remotehost: 'api.example.com'
    remoteport: '443'

- name: Fail if too slow
  run: |
    if [ "${{ steps.api.outputs.latency_ms }}" -gt 1000 ]; then
      echo "::error::Connection latency exceeded 1000ms"
      exit 1
    fi
```

## Send/expect behaviour

When `send` is provided (globally or per-target), the payload is transmitted immediately after the TCP connection is established. Escape sequences `\r` and `\n` are converted to real CR/LF bytes on the wire, so `send=GET / HTTP/1.0\r\n\r\n` sends a valid HTTP request.

When `expect` is provided (globally or per-target, with or without `send`), the action reads response data for up to `expect_timeout` seconds and matches it against the pattern as a Python regex (`re.search`). If the pattern is not found, the step fails with an `ExpectMismatchError` showing the actual response.

Using `expect` alone (without `send`) is useful for protocols that send a banner on connect (SMTP, MySQL, SSH, etc.).

### Override precedence

Per-target `| send=...` and `| expect=...` take precedence over the global `send` and `expect` inputs. Targets without overrides inherit the global values. This lets you set a common default and override only where needed.

## Retry behaviour

The action uses exponential backoff (with full jitter) to retry connection failures up to `maxtime` seconds. The initial delay between attempts is controlled by `retry_delay`. If `max_retries` is set to a non-zero value, retries are also capped at that count.

Note: `ExpectMismatchError` does **not** trigger retries — if the service is up but responding incorrectly, the action fails immediately. Only network-level errors (connection refused, timeout, DNS failure) are retried.
