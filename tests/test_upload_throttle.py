import http.server
import random
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import upload_throttle as ut


def simulate(cap_fn, secs=240, base=0.029, jitter=0.006, seed=1, cross=lambda t: 0.0, buf=1.0, cfg=None):
    """Fluid model of a bufferbloated uplink driven by the real Governor."""
    random.seed(seed)
    cfg = cfg or ut.ThrottleConfig()
    gov = ut.Governor(cfg)
    queue = t = sent = 0.0
    rows = []
    while t < secs:
        cap, rate = cap_fn(t), gov.rate
        for _ in range(10):
            dt = cfg.tick_s / 10
            queue = min(buf, max(0.0, queue + ((rate + cross(t) * cap) / cap - 1.0) * dt))
            sent += min(rate, cap) * dt
        rtts = {k: base + queue + random.uniform(0, jitter) for k in "abc"}
        t += cfg.tick_s
        gov.step(t, rtts, sent)
        rows.append((t, rate, cap, queue * 1000))
    return rows


def window(rows, lo, hi):
    sel = [r for r in rows if lo <= r[0] < hi]
    queues = sorted(r[3] for r in sel)
    return statistics.mean(r[1] / r[2] for r in sel), statistics.mean(queues), queues[int(len(queues) * 0.95)]


class ControllerTests(unittest.TestCase):
    def test_converges_without_knowing_capacity(self):
        # Slow DSL-like, the user's ~2.8 Mbit/s, and a fast link: no retuning.
        for cap in (50e3, 350e3, 12.5e6):
            with self.subTest(capacity=cap):
                util, mean_q, p95_q = window(simulate(lambda t: cap), 60, 240)
                self.assertGreater(util, 0.6)  # 2nd-lowest-of-8 control delay is more conservative than min-of-8
                self.assertLess(util, 1.0)
                # 8-round (~4 s) persistence filter trades slower reaction for jitter immunity:
                # the clean-bufferbloat sim now shows ~25-40 ms mean / ~200-260 ms p95 queue (was <20 / <80).
                self.assertLess(mean_q, 50)
                self.assertLess(p95_q, 300)

    def test_tracks_capacity_changes(self):
        down = simulate(lambda t: 350e3 if t < 100 else 120e3)
        self.assertGreater(window(down, 140, 240)[0], 0.6)
        up = simulate(lambda t: 120e3 if t < 100 else 500e3)
        self.assertGreater(window(up, 140, 240)[0], 0.6)

    def test_yields_to_cross_traffic_then_recovers(self):
        rows = simulate(lambda t: 350e3, cross=lambda t: 0.5 if 100 <= t < 160 else 0.0)
        self.assertLess(window(rows, 110, 160)[0], 0.6)    # gave way to the other flow
        self.assertLess(window(rows, 110, 160)[1], 30)     # queue stayed short meanwhile
        self.assertGreater(window(rows, 190, 240)[0], 0.7)  # took the link back

    def test_shared_wifi_jitter_does_not_stall_at_floor(self):
        # Replay of the real 2026-10-02 failure: probe hosts share (common-mode) jitter
        # incl. occasional ~70 ms spikes, on a ~2.8 Mbit/s link. The old controller
        # (2-round filter + single-sample severe trigger) sat at 16-17 KB/s forever.
        def run(cap, secs=900, seed=1, **kw):
            random.seed(seed)
            cfg = ut.ThrottleConfig(start_bps=32_000, **kw)
            gov = ut.Governor(cfg)
            backlog = t = sent = 0.0
            rates = []
            while t < secs:
                backlog = max(0.0, backlog + (gov.rate - cap) * cfg.tick_s)
                common = random.gammavariate(1.5, 0.016) + (0.07 if random.random() < 0.08 else 0.0)
                rtts = {h: 0.017 + common + random.gammavariate(1, 0.004) + backlog / cap for h in "abc"}
                sent += min(gov.rate, cap) * cfg.tick_s
                t += cfg.tick_s
                gov.step(t, rtts, sent)
                rates.append(gov.rate)
            return statistics.mean(rates[-600:])
        self.assertGreater(run(350e3), 150e3)  # escapes the floor and uses the link
        self.assertGreater(run(30e3), 20e3)    # slow link still works
        # Documents the old failure: reinstating the old filter pins the rate at the floor.
        self.assertLess(run(350e3, filter_rounds=2), 30e3)

    def test_control_delay_is_second_lowest_with_failure_rule(self):
        cfg = ut.ThrottleConfig()
        est = ut.DelayEstimator(cfg)
        for i in range(8):
            est.add_round(i * 0.5, {k: 0.020 for k in "abc"})   # baseline 20 ms
        # one lucky low round among high ones does NOT hide the queue (min-of-8 would say ~0)
        for i, rtt in enumerate([0.120, 0.150, 0.020, 0.140, 0.130, 0.160, 0.150, 0.120]):
            est.add_round(10 + i * 0.5, {k: rtt for k in "abc"})
        self.assertGreaterEqual(est.filtered, 0.095)
        # a single spike among low rounds still does not count
        est2 = ut.DelayEstimator(cfg)
        for i in range(8):
            est2.add_round(i * 0.5, {k: 0.020 for k in "abc"})
        est2.add_round(5.0, {k: 0.400 for k in "abc"})
        self.assertLess(est2.filtered, 0.01)
        # two timeout rounds (>=500 ms) force >=500 ms even when the other rounds are idle-low
        est2.add_round(5.5, {"a": None, "b": None, "c": 0.02})
        est2.add_round(6.0, {"a": None, "b": None, "c": None})
        self.assertGreaterEqual(est2.filtered, 0.5)
        self.assertEqual(est2.host_fails, 3)
        self.assertIn("|", est2.window_ms())

    def test_bursty_saturated_queue_backs_off_in_time(self):
        # Replay of the 19:15-19:19 failure: link saturated, queue drains for ~0.7 s every 6 s
        # (so lows exist), plus shared Wi-Fi jitter. min-of-8 hid the queue (p95 ~160 ms, rate pinned
        # near capacity); the 2nd-lowest rule must keep the queue short.
        class MinEst(ut.DelayEstimator):
            @property
            def filtered(self):
                return min(self.recent) if self.recent else None

        def run(est_cls, cap=280e3, secs=900, seed=1):
            random.seed(seed)
            cfg = ut.ThrottleConfig(start_bps=32_000)
            gov = ut.Governor(cfg)
            gov.estimator = est_cls(cfg)
            backlog = sent = t = 0.0
            rows = []
            while t < secs:
                t += cfg.tick_s
                pause = (t % 6.0) < 0.7
                send = 0.0 if pause else gov.rate / (1 - 0.7 / 6.0)
                backlog = min(1.2 * cap, max(0.0, backlog + (send - cap) * cfg.tick_s))
                qd = backlog / cap
                common = random.gammavariate(1.5, 0.016) + (0.07 if random.random() < 0.08 else 0.0)
                rtts = {h: (None if (v := 0.017 + common + random.gammavariate(1, 0.004) + qd) > 1.0 else v) for h in "abc"}
                sent += min(send, cap) * cfg.tick_s
                gov.step(t, rtts, sent)
                rows.append((gov.rate, qd * 1000))
            tail = rows[-1200:]
            qs = sorted(q for _, q in tail)
            return statistics.mean(r for r, _ in tail), statistics.mean(qs), qs[int(len(qs) * 0.95)]

        rate_new, q_new, p95_new = run(ut.DelayEstimator)
        rate_old, q_old, p95_old = run(MinEst)
        self.assertLess(p95_new, 100)            # queue kept short
        self.assertGreater(p95_old, p95_new * 2)  # the min filter really did hide it
        self.assertGreater(rate_new, 100e3)      # and it still uses the link (no floor-stall)

    def test_sustained_delay_still_backs_off_and_csv_has_control_delay(self):
        cfg = ut.ThrottleConfig()
        gov = ut.Governor(cfg)
        t = 0.0
        for _ in range(20):
            t += cfg.tick_s
            gov.step(t, {k: 0.020 for k in "abc"}, gov.rate * t)
        rate = gov.rate
        for _ in range(40):  # delay persistently +100 ms
            t += cfg.tick_s
            gov.step(t, {k: 0.120 for k in "abc"}, gov.rate * t)
        self.assertLess(gov.rate, rate)
        self.assertGreater(gov.estimator.filtered, 0.05)
        with tempfile.TemporaryDirectory() as d:
            th = ut.AdaptiveThrottle(ut.ThrottleConfig(csv_dir=d))
            th.governor = gov
            th.pacer.sent = 1
            th._write_csv(0)
            th.stop()
            header, row = (next(Path(d).glob("*.csv")).read_text().splitlines())[:2]
            self.assertTrue(header.endswith("control_delay_ms,probe_fails,window_ms"))
            self.assertEqual(len(row.split(",")), 10)

    def test_never_drops_below_floor_or_exceeds_cap(self):
        cfg = ut.ThrottleConfig(min_bps=16_000, max_bps=100_000)
        rows = simulate(lambda t: 12.5e6, cfg=cfg, secs=120)
        self.assertLessEqual(max(r[1] for r in rows), 100_000)
        rows = simulate(lambda t: 20e3, cross=lambda t: 5.0, cfg=cfg, secs=120)
        self.assertGreaterEqual(min(r[1] for r in rows), 16_000)

    def test_does_not_grow_when_sender_is_idle(self):
        cfg = ut.ThrottleConfig()
        gov = ut.Governor(cfg)
        t = 0.0
        for _ in range(60):
            t += cfg.tick_s
            gov.step(t, {"a": 0.029, "b": 0.029}, 0)  # nothing sent
        self.assertEqual(gov.rate, cfg.start_bps)

    def test_probe_timeouts_count_as_congestion(self):
        cfg = ut.ThrottleConfig()
        est = ut.DelayEstimator(cfg)
        for i in range(8):
            est.add_round(i * 0.5, {"a": 0.03, "b": 0.03, "c": 0.03})
        delay = est.add_round(5.0, {"a": None, "b": None, "c": None})
        self.assertGreater(delay, 0.9)

    def test_blind_probes_hold_rate(self):
        cfg = ut.ThrottleConfig()
        gov = ut.Governor(cfg)
        for i in range(10):
            gov.step(i * 0.5, {"a": 0.03}, 0)
        before = gov.rate
        for i in range(10, 20):
            gov.step(i * 0.5, {}, 0)
        self.assertTrue(gov.blind)
        self.assertEqual(gov.rate, before)

    def test_env_overrides(self):
        cfg = ut.ThrottleConfig.from_env({
            "YT_UPLOAD_TARGET_DELAY_MS": "50", "YT_UPLOAD_MAX_KBPS": "8000",
            "YT_UPLOAD_MIN_KBPS": "bogus", "YT_UPLOAD_PROBE_HOSTS": "example.com:443, 9.9.9.9:443"})
        self.assertAlmostEqual(cfg.target_delay_s, 0.05)
        self.assertEqual(cfg.max_bps, 1_000_000)
        self.assertEqual(cfg.min_bps, ut.ThrottleConfig().min_bps)
        self.assertEqual(cfg.probe_targets, ("example.com:443", "9.9.9.9:443"))

    def test_chunk_size_follows_rate_in_256k_units(self):
        throttle = ut.AdaptiveThrottle(ut.ThrottleConfig())
        for rate in (1_000, 350_000, 5_000_000, 500_000_000):
            throttle.pacer.rate = rate
            size = throttle.chunk_bytes()
            self.assertEqual(size % ut.CHUNK_UNIT, 0)
            self.assertTrue(ut.CHUNK_UNIT <= size <= ut.CHUNK_UNIT * ut.MAX_CHUNK_UNITS)
        throttle.pacer.rate = 350_000
        self.assertEqual(throttle.chunk_bytes(), 5 * ut.CHUNK_UNIT)


class PacerTests(unittest.TestCase):
    def test_pacer_holds_average_rate(self):
        now = [0.0]
        pacer = ut.Pacer(100_000, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
        for _ in range(500):
            pacer.acquire(2920)
        self.assertAlmostEqual(500 * 2920 / now[0], 100_000, delta=3_000)

    def test_slice_size_bounds(self):
        self.assertEqual(ut.Pacer(1_000).slice_size(), 2920)
        self.assertEqual(ut.Pacer(10**9).slice_size(), 16384)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received = []

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.received.append(body)
        self.send_response(308)  # resume incomplete: must not be followed as a redirect
        self.send_header("Range", f"bytes=0-{len(body) - 1}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "openssl required for local TLS server")
class PacedHttpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert, key = Path(tmp.name) / "c.pem", Path(tmp.name) / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        _Handler.received = []
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}/upload"

    def test_https_put_is_paced_intact_and_308_not_followed(self):
        pacer = ut.Pacer(200_000)
        http = ut.PacedHttp(pacer, timeout=30, disable_ssl_certificate_validation=True)
        payload = bytes(random.Random(0).randrange(256) for _ in range(256)) * 1024  # 256 KiB
        start = time.monotonic()
        resp, _ = http.request(self.url, method="PUT", body=payload, headers={"Content-Length": str(len(payload))})
        elapsed = time.monotonic() - start
        self.assertEqual(resp.status, 308)
        self.assertEqual(_Handler.received, [payload])
        self.assertGreaterEqual(pacer.sent, len(payload))
        self.assertAlmostEqual(len(payload) / elapsed, 200_000, delta=40_000)  # ~1.3 s at 200 kB/s

    def test_rate_change_applies_mid_transfer(self):
        pacer = ut.Pacer(100_000)
        http = ut.PacedHttp(pacer, timeout=30, disable_ssl_certificate_validation=True)
        payload = b"x" * 200_000
        threading.Timer(0.5, lambda: setattr(pacer, "rate", 1_000_000)).start()
        start = time.monotonic()
        http.request(self.url, method="PUT", body=payload, headers={"Content-Length": str(len(payload))})
        self.assertLess(time.monotonic() - start, 1.5)  # 2 s if the rate had stayed at 100 kB/s
        self.assertEqual(_Handler.received, [payload])


if __name__ == "__main__":
    unittest.main()
