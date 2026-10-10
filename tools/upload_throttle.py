#!/usr/bin/env python3
"""Adaptive upstream pacing for resumable YouTube uploads.

Problem: an unpaced upload fills the uplink and its router queue (bufferbloat),
so every other flow sees huge latency. Fix: pace the bytes written to the
socket, and steer the pace with a measured queueing-delay signal instead of a
hard-coded bandwidth.

  1. A probe thread measures TCP-connect RTT to a few hosts about twice a
     second. Per host, baseline = minimum RTT over a sliding window, so
     queue delay = RTT - baseline. The baseline is learned while the uploader
     is still idle (wait_ready), so it starts uncontaminated.
  2. A controller (slow start, then delay-based AIMD, LEDBAT-like) sets the
     send rate. Queue delay above the target means we are the bottleneck:
     back off. Well below the target: probe upward. Other traffic that raises
     the delay therefore makes the uploader yield.
  3. A pacer sleeps between small socket writes to hold the send rate.
     Chunk size follows the rate so a chunk is a few seconds long.

Nothing here knows the link speed. Optional env overrides (all rates kbit/s):
  YT_UPLOAD_THROTTLE=off          disable pacing entirely
  YT_UPLOAD_TARGET_DELAY_MS=30    tolerated queue delay above baseline
  YT_UPLOAD_MIN_KBPS=128          floor so progress never stops
  YT_UPLOAD_MAX_KBPS=0            optional cap (0 = none)
  YT_UPLOAD_START_KBPS=256        slow-start initial rate
  YT_UPLOAD_PROBE_HOSTS=h:p,...   probe targets

Measure latency alone (A/B comparison, uploader stopped vs running):
  python3 tools/upload_throttle.py --measure 30
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import socket
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httplib2

CHUNK_UNIT = 256 * 1024  # YouTube: chunk sizes must be multiples of 256 KiB.
MAX_CHUNK_UNITS = 32     # 8 MiB


@dataclass
class ThrottleConfig:
    target_delay_s: float = 0.030
    start_bps: float = 32_000        # bytes/s (256 kbit/s)
    min_bps: float = 16_000          # bytes/s (128 kbit/s)
    max_bps: float = float("inf")
    tick_s: float = 0.5
    probe_timeout_s: float = 1.0
    probe_targets: tuple = ("www.googleapis.com:443", "1.1.1.1:443", "8.8.8.8:443")
    baseline_window_s: float = 600.0
    filter_rounds: int = 8           # control delay = 2nd-lowest of the last N rounds (~4 s)
    fail_delay_s: float = 0.5        # a round this slow (timeouts) counts as a failure round
    fail_rounds_min: int = 2         # >= this many failure rounds in the window -> control >= fail_delay_s
    warmup_rounds: int = 6
    startup_exit_frac: float = 0.3   # leave slow start at this x target
    grow_frac: float = 0.6           # only grow below this x target
    util_min: float = 0.5            # only grow if the sender used the rate
    startup_growth: float = 1.2      # per tick
    fast_growth: float = 0.08        # per tick, far below the known ceiling
    slow_growth: float = 0.01        # per tick, near/above the ceiling
    decrease_cooldown_s: float = 1.0
    chunk_seconds: float = 4.0
    log_every_s: float = 15.0
    csv_every_s: float = 2.0
    csv_dir: Path = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cfg = cls()

        def num(name):
            try:
                value = float(env.get(name, ""))
            except ValueError:
                return None
            return value if value >= 0 else None

        value = num("YT_UPLOAD_TARGET_DELAY_MS")
        if value:
            cfg.target_delay_s = value / 1000.0
        for name, attr in (("YT_UPLOAD_MIN_KBPS", "min_bps"), ("YT_UPLOAD_START_KBPS", "start_bps")):
            value = num(name)
            if value:
                setattr(cfg, attr, value * 125.0)
        value = num("YT_UPLOAD_MAX_KBPS")
        if value:
            cfg.max_bps = value * 125.0
        hosts = [h.strip() for h in env.get("YT_UPLOAD_PROBE_HOSTS", "").split(",") if h.strip()]
        if hosts:
            cfg.probe_targets = tuple(hosts)
        cfg.start_bps = min(max(cfg.start_bps, cfg.min_bps), cfg.max_bps)
        return cfg


# ============================================================
# DELAY ESTIMATION
# ============================================================

class _WindowMin:
    """Sliding-window minimum (monotonic deque)."""

    def __init__(self, window):
        self.window = window
        self.items = collections.deque()

    def add(self, now, value):
        while self.items and self.items[-1][1] >= value:
            self.items.pop()
        self.items.append((now, value))
        while self.items[0][0] < now - self.window:
            self.items.popleft()

    def value(self):
        return self.items[0][1] if self.items else None


class DelayEstimator:
    """Turns per-host RTT samples into one queueing-delay figure."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bases = {}
        self.recent = collections.deque(maxlen=max(1, cfg.filter_rounds))
        self.rounds = 0
        self.latest = None
        self.host_fails = 0   # failed probes in the latest round
        self.fail_total = 0   # failed probes since start (the CSV reports deltas)

    def add_round(self, now, rtts):
        """rtts: {label: seconds, or None for a failed/timed-out probe}."""
        self.rounds += 1
        self.host_fails = sum(1 for rtt in rtts.values() if rtt is None)
        self.fail_total += self.host_fails
        delays = []
        for label, rtt in rtts.items():
            base = self.bases.get(label)
            if rtt is None:
                # A host that normally answers but now times out is saturation.
                if base is not None:
                    delays.append(max(0.0, self.cfg.probe_timeout_s - base.value()))
                continue
            if base is None:
                base = self.bases[label] = _WindowMin(self.cfg.baseline_window_s)
            base.add(now, rtt)
            delays.append(max(0.0, rtt - base.value()))
        if not delays:
            self.latest = None
            self.recent.clear()
            return None
        self.latest = statistics.median(delays)
        self.recent.append(self.latest)
        return self.latest

    @property
    def filtered(self):
        """Control delay: 2nd-lowest of the window (one lucky low round no longer hides
        a bursty queue; one lone spike still cannot cut), forced >= fail_delay_s when
        repeated probe-timeout rounds occur."""
        if not self.recent:
            return None
        ordered = sorted(self.recent)
        value = ordered[1] if len(ordered) > 1 else ordered[0]
        if sum(1 for d in self.recent if d >= self.cfg.fail_delay_s) >= self.cfg.fail_rounds_min:
            value = max(value, self.cfg.fail_delay_s)
        return value

    def window_ms(self):
        return "|".join(f"{d * 1000:.0f}" for d in self.recent)

    @property
    def baseline(self):
        values = [b.value() for b in self.bases.values() if b.value() is not None]
        return statistics.median(values) if values else None

    @property
    def ready(self):
        return self.rounds >= self.cfg.warmup_rounds and self.baseline is not None


# ============================================================
# RATE CONTROL
# ============================================================

class RateController:
    """Slow start, then delay-based AIMD. Pure logic; the caller owns the clock."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rate = cfg.start_bps
        self.phase = "startup"
        self.ceiling = None  # rate at the last congestion event
        self._last_decrease = float("-inf")

    def update(self, now, filtered, latest, utilization):
        cfg, target = self.cfg, self.cfg.target_delay_s
        delay = filtered  # persistent (min over recent rounds) in every phase

        if delay > target:
            if now - self._last_decrease >= cfg.decrease_cooldown_s:
                factor = min(0.9, max(0.5, target / delay))
                if self.phase == "startup":
                    factor = min(factor, 0.75)
                self.ceiling = self.rate
                self.rate *= factor
                self._last_decrease = now
                self.phase = "avoid"
        elif self.phase == "startup" and latest > cfg.startup_exit_frac * target:
            self.phase = "avoid"  # queue is starting to build: stop doubling
        elif delay < cfg.grow_frac * target and utilization >= cfg.util_min:
            if self.phase == "startup":
                self.rate *= cfg.startup_growth
            elif self.ceiling is None or self.rate < 0.85 * self.ceiling:
                self.rate *= 1.0 + cfg.fast_growth
            else:
                self.rate *= 1.0 + cfg.slow_growth

        self.rate = min(max(self.rate, cfg.min_bps), cfg.max_bps)
        return self.rate


class Governor:
    """One control tick: probe results + bytes sent in, send rate out."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.estimator = DelayEstimator(cfg)
        self.controller = RateController(cfg)
        self.rate = cfg.start_bps
        self.utilization = 0.0
        self.blind = False
        self._last_t = None
        self._last_bytes = 0

    def step(self, now, rtts, total_bytes):
        latest = self.estimator.add_round(now, rtts)
        if self._last_t is not None and now > self._last_t:
            self.utilization = (total_bytes - self._last_bytes) / (self.rate * (now - self._last_t))
        self._last_t, self._last_bytes = now, total_bytes
        self.blind = self.estimator.ready and latest is None
        if self.estimator.ready and latest is not None:
            self.rate = self.controller.update(now, self.estimator.filtered, latest, self.utilization)
        return self.rate  # warm-up or blind: hold the current rate


# ============================================================
# PACING
# ============================================================

class Pacer:
    """Holds the average write rate by sleeping before each small write."""

    MAX_CREDIT_S = 0.05

    def __init__(self, rate_bps, sleep=time.sleep, clock=time.monotonic):
        self.rate = float(rate_bps)
        self.sent = 0
        self._next = 0.0
        self._sleep = sleep
        self._clock = clock

    def slice_size(self):
        return int(min(max(self.rate * 0.01, 2920), 16384))

    def acquire(self, nbytes):
        now = self._clock()
        self._next = max(self._next, now - self.MAX_CREDIT_S)
        wait = self._next - now
        if wait > 0:
            self._sleep(wait)
        self._next += nbytes / self.rate
        self.sent += nbytes


class PacedSocket:
    """Socket proxy that paces sends; everything else is delegated."""

    def __init__(self, sock, pacer):
        self._sock = sock
        self._pacer = pacer

    def sendall(self, data, *args):
        view = memoryview(data).cast("B")
        while len(view):
            piece = view[:self._pacer.slice_size()]
            self._pacer.acquire(len(piece))
            self._sock.sendall(piece)
            view = view[len(piece):]

    def send(self, data, *args):
        piece = memoryview(data).cast("B")[:self._pacer.slice_size()]
        self._pacer.acquire(len(piece))
        return self._sock.send(piece)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def make_paced_connection_class(pacer):
    class PacedHTTPSConnection(httplib2.HTTPSConnectionWithTimeout):
        def connect(self):
            super().connect()
            if not isinstance(self.sock, PacedSocket):
                self.sock = PacedSocket(self.sock, pacer)
    return PacedHTTPSConnection


class PacedHttp(httplib2.Http):
    """httplib2.Http whose HTTPS sends go through a Pacer."""

    def __init__(self, pacer, **kwargs):
        super().__init__(**kwargs)
        self._paced_connection = make_paced_connection_class(pacer)
        # Same as googleapiclient.http.build_http: 308 means "resume incomplete".
        self.redirect_codes = self.redirect_codes - {308}

    def request(self, uri, method="GET", body=None, headers=None, redirections=5, connection_type=None):
        if connection_type is None and str(uri).lower().startswith("https:"):
            connection_type = self._paced_connection
        return super().request(uri, method, body, headers, redirections, connection_type)


# ============================================================
# PROBING
# ============================================================

def resolve_target(spec):
    host, _, port = spec.rpartition(":")
    if not host:
        host, port = spec, "443"
    infos = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_STREAM)
    return infos[0][4]


def probe_tcp(addr, timeout):
    """Seconds for SYN -> SYN/ACK, or None on failure/timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        start = time.perf_counter()
        sock.connect(addr)
        return time.perf_counter() - start
    except OSError:
        return None
    finally:
        sock.close()


def probe_round(pool, targets, timeout):
    futures = {label: pool.submit(probe_tcp, addr, timeout) for label, addr in targets.items()}
    results = {}
    for label, future in futures.items():
        try:
            results[label] = future.result(timeout=timeout + 0.5)
        except Exception:
            results[label] = None
    return results


# ============================================================
# FACADE
# ============================================================

class AdaptiveThrottle:
    def __init__(self, cfg=None, log=None, probe=None):
        self.cfg = cfg or ThrottleConfig.from_env()
        self.log = log or (lambda msg, level="INFO": print(f"[{level}] {msg}", flush=True))
        self.governor = Governor(self.cfg)
        self.pacer = Pacer(self.cfg.start_bps)
        self._probe = probe  # test hook: callable() -> {label: seconds|None}
        self._stop = threading.Event()
        self._thread = None
        self._csv = None
        self._csv_fails = 0

    # -- lifecycle ------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="upload-throttle", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._csv:
            self._csv[0].close()
            self._csv = None

    def wait_ready(self, timeout=6.0):
        """Block until the idle baseline is learned. False = running blind."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.governor.estimator.ready:
                return True
            time.sleep(0.1)
        return self.governor.estimator.ready

    # -- consumers ------------------------------------------------
    def make_http(self, timeout=60):
        return PacedHttp(self.pacer, timeout=socket.getdefaulttimeout() or timeout)

    def chunk_bytes(self):
        units = int(self.pacer.rate * self.cfg.chunk_seconds // CHUNK_UNIT)
        return max(1, min(MAX_CHUNK_UNITS, units)) * CHUNK_UNIT

    def status(self):
        g, e = self.governor, self.governor.estimator
        delay = "n/a" if e.latest is None else f"{e.latest * 1000:.0f}ms"
        base = "n/a" if e.baseline is None else f"{e.baseline * 1000:.0f}ms"
        return (f"rate={self.pacer.rate / 1000:.0f}KB/s ({self.pacer.rate * 8 / 1e6:.2f}Mbit/s) "
                f"queue_delay={delay} baseline_rtt={base} phase={g.controller.phase} "
                f"util={g.utilization:.2f}" + (" PROBES_BLIND" if g.blind else ""))

    # -- control loop ---------------------------------------------
    def _targets(self):
        targets = {}
        for spec in self.cfg.probe_targets:
            try:
                targets[spec] = resolve_target(spec)
            except (OSError, ValueError) as exc:
                self.log(f"THROTTLE probe target {spec} unavailable: {exc}", "WARN")
        return targets

    def _run(self):
        targets = {} if self._probe else self._targets()
        pool = None if self._probe else concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets)))
        if not self._probe and not targets:
            self.log("THROTTLE no probe targets resolvable; holding the start rate", "WARN")
        next_tick = time.monotonic()
        last_log = last_csv = 0.0
        was_ready = was_blind = False
        while not self._stop.is_set():
            rtts = self._probe() if self._probe else probe_round(pool, targets, self.cfg.probe_timeout_s) if targets else {}
            now = time.monotonic()
            self.pacer.rate = self.governor.step(now, rtts, self.pacer.sent)
            if self.governor.estimator.ready and not was_ready:
                was_ready = True
                self.log("THROTTLE baseline learned: " + self.status())
            if self.governor.blind != was_blind:
                was_blind = self.governor.blind
                self.log("THROTTLE probes " + ("all failing; holding rate" if was_blind else "recovered"),
                         "WARN" if was_blind else "INFO")
            if now - last_log >= self.cfg.log_every_s and self.pacer.sent:
                last_log = now
                self.log("THROTTLE " + self.status())
            if now - last_csv >= self.cfg.csv_every_s:
                last_csv = now
                self._write_csv(now)
            next_tick += self.cfg.tick_s
            self._stop.wait(max(0.0, next_tick - time.monotonic()))
        if pool:
            pool.shutdown(wait=False)

    def _write_csv(self, now):
        if not self.cfg.csv_dir or not self.pacer.sent:
            return
        try:
            if self._csv is None:
                path = Path(self.cfg.csv_dir) / f"upload_throttle_{datetime.now():%Y%m}.csv"
                new = not path.exists()
                handle = path.open("a", newline="")
                writer = csv.writer(handle)
                header = ["time", "rate_kBps", "queue_delay_ms", "baseline_rtt_ms",
                          "utilization", "phase", "sent_bytes", "control_delay_ms",
                          "probe_fails", "window_ms"]
                if not new:
                    with path.open(newline="") as old:
                        first = next(csv.reader(old), [])
                    if first != header:  # keep the old-format file intact, start a new one
                        handle.close()
                        path.rename(path.with_name(path.stem + f"_oldfmt_{datetime.now():%H%M%S}.csv"))
                        handle, new = path.open("a", newline=""), True
                        writer = csv.writer(handle)
                if new:
                    writer.writerow(header)
                self._csv = (handle, writer)
            e, g = self.governor.estimator, self.governor
            self._csv[1].writerow([
                datetime.now().isoformat(timespec="seconds"), f"{self.pacer.rate / 1000:.1f}",
                "" if e.latest is None else f"{e.latest * 1000:.0f}",
                "" if e.baseline is None else f"{e.baseline * 1000:.0f}",
                f"{g.utilization:.2f}", g.controller.phase, self.pacer.sent,
                "" if e.filtered is None else f"{e.filtered * 1000:.0f}",
                e.fail_total - self._csv_fails, e.window_ms()])
            self._csv_fails = e.fail_total
            self._csv[0].flush()
        except OSError:
            self.cfg.csv_dir = None  # metrics are optional; never disturb the upload


def from_env(log=None, csv_dir=None):
    """Return a started AdaptiveThrottle, or None when YT_UPLOAD_THROTTLE=off."""
    if os.environ.get("YT_UPLOAD_THROTTLE", "adaptive").strip().lower() in ("off", "0", "false", "no"):
        return None
    cfg = ThrottleConfig.from_env()
    cfg.csv_dir = csv_dir
    throttle = AdaptiveThrottle(cfg, log=log)
    throttle.start()
    return throttle


# ============================================================
# CLI: latency measurement for A/B comparison
# ============================================================

def measure(seconds, targets, interval=0.5, timeout=1.0):
    resolved = {}
    for spec in targets:
        try:
            resolved[spec] = resolve_target(spec)
        except (OSError, ValueError) as exc:
            print(f"skip {spec}: {exc}")
    samples = {label: [] for label in resolved}
    failures = {label: 0 for label in resolved}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(resolved))) as pool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tick = time.monotonic()
            for label, rtt in probe_round(pool, resolved, timeout).items():
                (samples[label].append(rtt * 1000) if rtt is not None else failures.__setitem__(label, failures[label] + 1))
            time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    print(f"{'target':26} {'n':>4} {'min':>6} {'median':>7} {'p95':>6} {'max':>6}  fail   (ms, TCP connect RTT)")
    for label, values in samples.items():
        if not values:
            print(f"{label:26} no successful probes ({failures[label]} failures)")
            continue
        values.sort()
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(f"{label:26} {len(values):4d} {values[0]:6.0f} {statistics.median(values):7.0f} "
              f"{p95:6.0f} {values[-1]:6.0f}  {failures[label]:4d}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--measure", type=float, metavar="SECONDS", help="print latency stats for SECONDS")
    args = parser.parse_args()
    if args.measure:
        measure(args.measure, ThrottleConfig.from_env().probe_targets)
    else:
        parser.print_help()


if __name__ == "__main__":
    sys.exit(main())
