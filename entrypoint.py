import json
import os
import re
import sys
import socket
import logging
import time

import backoff


class ExpectMismatchError(Exception):
    """Raised when the response does not match the expected pattern."""

    def __init__(self, pattern, response):
        self.pattern = pattern
        self.response = response
        super().__init__(
            f"Response did not match expected pattern /{pattern}/. "
            f"Got: {response!r}"
        )


def parse_positive_int(value, name, allow_zero=False):
    try:
        n = int(value)
    except (ValueError, TypeError):
        print(f"::error::{name} must be an integer, got '{value}'")
        sys.exit(1)
    lower = 0 if allow_zero else 1
    if n < lower:
        print(f"::error::{name} must be >= {lower}, got {n}")
        sys.exit(1)
    return n


def decode_send_payload(raw):
    """Process escape sequences like \\r\\n in the send payload so users can
    write literal ``\\r\\n`` in YAML and have it arrive as CR+LF on the wire."""
    return raw.encode("utf-8").decode("unicode_escape").encode("latin-1")


def validate_expect_pattern(pattern, context="expect"):
    try:
        re.compile(pattern)
    except re.error as e:
        print(f"::error::{context} is not a valid regex: {e}")
        sys.exit(1)


def parse_target_line(line, lineno):
    """Parse a single targets line into a target dict.

    Supported formats:
        host:port
        host:port | send=payload | expect=pattern

    Returns dict with keys: host, port, send (str|None), expect (str|None).
    """
    parts = [p.strip() for p in line.split("|")]

    addr = parts[0]
    if ":" not in addr:
        print(f"::error::targets line {lineno}: expected host:port, got '{addr}'")
        sys.exit(1)

    h, _, p = addr.rpartition(":")
    port = parse_positive_int(p, f"targets line {lineno} port")
    if not 1 <= port <= 65535:
        print(f"::error::targets line {lineno}: port must be 1-65535, got {port}")
        sys.exit(1)
    if not h:
        print(f"::error::targets line {lineno}: host must not be empty")
        sys.exit(1)

    target = {"host": h, "port": port, "send": None, "expect": None}

    for part in parts[1:]:
        if part.startswith("send="):
            target["send"] = part[5:]
        elif part.startswith("expect="):
            value = part[7:]
            validate_expect_pattern(value, f"targets line {lineno} expect")
            target["expect"] = value
        elif part:
            print(
                f"::error::targets line {lineno}: unknown option '{part}'. "
                f"Supported: send=..., expect=..."
            )
            sys.exit(1)

    return target


def parse_targets(targets_str, host, port_str):
    """Build a list of target dicts from either the targets input or the single
    remotehost/remoteport pair.

    Each target dict has: host, port, send (str|None), expect (str|None).
    """
    targets = []

    if targets_str:
        for lineno, line in enumerate(targets_str.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            targets.append(parse_target_line(line, lineno))
    else:
        if not host:
            print("::error::Either remotehost/remoteport or targets must be provided")
            sys.exit(1)
        port = parse_positive_int(port_str, "remoteport")
        if not 1 <= port <= 65535:
            print(f"::error::remoteport must be 1-65535, got {port}")
            sys.exit(1)
        targets.append({"host": host, "port": port, "send": None, "expect": None})

    return targets


def get_config():
    maxtime = parse_positive_int(
        os.environ.get("INPUT_MAXTIME", "60"), "maxtime"
    )
    connect_timeout = parse_positive_int(
        os.environ.get("INPUT_CONNECT_TIMEOUT", "10"), "connect_timeout"
    )
    max_retries = parse_positive_int(
        os.environ.get("INPUT_MAX_RETRIES", "0"), "max_retries", allow_zero=True
    )
    retry_delay = parse_positive_int(
        os.environ.get("INPUT_RETRY_DELAY", "1"), "retry_delay"
    )
    expect_timeout = parse_positive_int(
        os.environ.get("INPUT_EXPECT_TIMEOUT", "5"), "expect_timeout"
    )

    send_raw = os.environ.get("INPUT_SEND", "")
    global_send = send_raw if send_raw else None

    global_expect = os.environ.get("INPUT_EXPECT", "") or None
    if global_expect:
        validate_expect_pattern(global_expect)

    return (
        maxtime, connect_timeout, max_retries, retry_delay,
        global_send, global_expect, expect_timeout,
    )


def resolve_send_expect(target, global_send, global_expect):
    """Resolve the effective send payload and expect pattern for a target.
    Per-target values override globals; explicit per-target empty string disables
    the global."""
    raw_send = target["send"] if target["send"] is not None else global_send
    send_payload = decode_send_payload(raw_send) if raw_send else None

    expect_pattern = target["expect"] if target["expect"] is not None else global_expect

    return send_payload, expect_pattern


def connect_target(host, port, maxtime, connect_timeout, max_retries,
                   retry_delay, send_payload, expect_pattern, expect_timeout):
    """Attempt a TCP connection with optional send/expect verification.
    Returns (success, latency_ms, response_text, error_msg)."""

    max_tries = max_retries if max_retries > 0 else None

    @backoff.on_exception(
        backoff.expo,
        (ConnectionRefusedError, socket.timeout, TimeoutError, OSError),
        max_time=maxtime,
        max_tries=max_tries,
        base=retry_delay,
        jitter=backoff.full_jitter,
    )
    def _connect():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(connect_timeout)
            s.connect((host, port))

            if send_payload:
                s.sendall(send_payload)

            if send_payload or expect_pattern:
                s.settimeout(expect_timeout)
                chunks = []
                try:
                    while True:
                        data = s.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                except socket.timeout:
                    pass

                response = b"".join(chunks).decode("utf-8", errors="replace")

                if expect_pattern and not re.search(expect_pattern, response):
                    raise ExpectMismatchError(expect_pattern, response)

                return response
            else:
                s.shutdown(socket.SHUT_WR)
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                return None

    start = time.monotonic()
    try:
        response = _connect()
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return True, elapsed_ms, response, None
    except ExpectMismatchError as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return False, elapsed_ms, e.response, str(e)
    except socket.gaierror as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution failed: {e}"
    except (ConnectionRefusedError, TimeoutError, socket.timeout) as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"Connection failed: {e}"
    except OSError as e:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"Network error: {e}"


def set_output(name, value):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            if "\n" in str(value):
                import uuid
                delimiter = f"ghadelimiter_{uuid.uuid4()}"
                f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{name}={value}\n")
    else:
        print(f"::set-output name={name}::{value}")


def describe_mode(send_payload, expect_pattern):
    if send_payload and expect_pattern:
        return "send/expect"
    if send_payload:
        return "send"
    if expect_pattern:
        return "expect (banner)"
    return "connect"


def main():
    logging.getLogger("backoff").addHandler(logging.StreamHandler())

    targets = parse_targets(
        os.environ.get("INPUT_TARGETS", ""),
        os.environ.get("INPUT_REMOTEHOST", ""),
        os.environ.get("INPUT_REMOTEPORT", ""),
    )

    (maxtime, connect_timeout, max_retries, retry_delay,
     global_send, global_expect, expect_timeout) = get_config()

    details = []
    total_latency_ms = 0
    any_failed = False

    for target in targets:
        host = target["host"]
        port = target["port"]
        send_payload, expect_pattern = resolve_send_expect(
            target, global_send, global_expect
        )
        mode = describe_mode(send_payload, expect_pattern)

        print(f"::group::{mode} → {host}:{port}")
        success, latency_ms, response, error = connect_target(
            host, port, maxtime, connect_timeout, max_retries, retry_delay,
            send_payload, expect_pattern, expect_timeout,
        )
        total_latency_ms += latency_ms

        entry = {
            "host": host,
            "port": port,
            "status": "success" if success else "failure",
            "latency_ms": latency_ms,
        }

        if response is not None:
            preview = response[:500]
            entry["response"] = preview
            print(f"Response ({len(response)} bytes):\n{preview}")

        if success:
            print(f"::notice::Connected to {host}:{port} in {latency_ms}ms")
        else:
            any_failed = True
            entry["error"] = error
            print(f"::error::Failed {host}:{port} — {error}")

        details.append(entry)
        print("::endgroup::")

    passed = sum(1 for d in details if d["status"] == "success")
    total = len(details)

    set_output("details", json.dumps(details))
    set_output("latency_ms", str(total_latency_ms))

    if any_failed:
        summary = f"{passed}/{total} targets reachable"
        set_output("result", summary)
        print(f"\n::error::{summary}")
        sys.exit(1)

    summary = f"All {total} target(s) reachable in {total_latency_ms}ms"
    set_output("result", summary)
    print(f"\n::notice::{summary}")


if __name__ == "__main__":
    main()
