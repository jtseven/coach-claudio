"""Offline synthetic ingestion regression tests; no athlete files or services."""
import importlib.util
import json
from pathlib import Path
import sys
import os
import subprocess
import http.client
import threading
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skill" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("ingestion_under_test", SCRIPTS / "health_server.py")
assert spec is not None and spec.loader is not None
hs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hs)


def export(points, aggregation=None, name="step_count"):
    metric = {"name": name, "units": "count", "data": [
        {"date": f"2026-06-01 {stamp} +0000", "qty": qty} for stamp, qty in points]}
    if aggregation:
        metric["aggregation"] = aggregation
    return {"data": {"metrics": [metric]}}


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.override = patch.object(hs, "DAILY_DIR", str(self.root / "daily"))
        self.override.start()
        self.addCleanup(self.override.stop)

    def day(self):
        return json.loads((self.root / "daily/2026-06-01.json").read_text())

    def test_one_hour_partial_preserves_other_buckets(self):
        hs.store(export([("01:00:00", 10), ("02:00:00", 20)]))
        hs.store(export([("02:00:00", 25)]))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [10, 25])

    def test_explicit_daily_replaces_hourly_without_double_count(self):
        hs.store(export([("01:00:00", 10), ("02:00:00", 20)], "hourly"))
        hs.store(export([("00:00:00", 35)], "daily"))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [35])
        with self.assertRaises(ValueError):
            hs.store(export([("03:00:00", 5)], "hourly"))

    def test_invalid_payload_is_rejected_without_any_mutation(self):
        cases = [[], {"data": []}, {"metrics": {}}, {"metrics": [None]},
                 {"metrics": [{"name": "steps", "data": [None]}]},
                 {"workouts": [{"start": "../escaped"}]},
                 {"workouts": [{"start": "2026-02-30 12:00:00 +0000"}]},
                 {"workouts": "wrong"}]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hs.store(bad)
        good = export([("01:00:00", 10)])
        good["data"]["metrics"][0]["data"].append({"date": "2026-02-30", "qty": 1})
        with self.assertRaises(ValueError):
            hs.store(good)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_resolved_path_containment(self):
        daily = self.root / "daily"
        daily.mkdir()
        target = self.root / "outside.json"
        target.write_text('{"metrics": {}}')
        (daily / "2026-06-01.json").symlink_to(target)
        with self.assertRaises(ValueError):
            hs.store(export([("01:00:00", 1)]))
        self.assertEqual(target.read_text(), '{"metrics": {}}')

    def request(self, payload, key="synthetic-key", path="/health", raw=False, chunked=False):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(conn.close)
        headers = {} if key is None else {"x-api-key": key}
        body = payload if raw else json.dumps(payload).encode()
        if chunked:
            body = iter([body[:5], body[5:]])
        conn.request("POST", path, body=body, headers=headers, encode_chunked=chunked)
        response = conn.getresponse()
        response.read()
        return response.status

    def start_server(self, key="synthetic-key"):
        override = patch.object(hs, "API_KEY", key)
        override.start()
        self.addCleanup(override.stop)
        self.server = hs.ThreadingHTTPServer(("127.0.0.1", 0), hs.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def test_startup_requires_key(self):
        env = dict(os.environ, COACH_HOME=str(self.root), HEALTH_PORT="0", HEALTH_API_KEY="")
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "health_server.py")],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            try:
                _, err = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                self.fail("server started without an API key")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(b"HEALTH_API_KEY", err)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=3)

    def test_http_auth_fails_closed_even_when_key_is_empty(self):
        self.start_server("")
        self.assertEqual(self.request({}), 403)

    def test_http_auth_and_invalid_payloads(self):
        self.start_server()
        self.assertEqual(self.request({}, key=None), 403)
        self.assertEqual(self.request({}, key="wrong"), 403)
        for payload in ([], {"metrics": {}}, {"workouts": [{"start": "../escaped"}]}):
            self.assertEqual(self.request(payload), 400)
        self.assertEqual(self.request(b"not-json", raw=True), 400)

    def test_bind_default_loopback_and_explicit_lan_override(self):
        for value, expected in ((None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")):
            with self.subTest(bind=value), patch.dict(os.environ, {}, clear=False), patch.object(hs, "API_KEY", "synthetic-key"):
                os.environ.pop("HEALTH_BIND", None)
                if value is not None:
                    os.environ["HEALTH_BIND"] = value
                with patch.object(hs, "ThreadingHTTPServer", side_effect=RuntimeError("stop before bind")) as factory:
                    with self.assertRaises(RuntimeError):
                        hs.main()
                    self.assertEqual(factory.call_args.args[0], (expected, hs.PORT))

    def test_raw_and_hourly_cannot_be_silently_mixed(self):
        hs.store(export([("01:00:00", 10), ("02:00:00", 20)], "hourly"))
        before = self.day()
        with self.assertRaises(ValueError):
            hs.store(export([("01:00:01", 3), ("01:00:02", 4)], "raw"))
        self.assertEqual(self.day(), before)

    def test_point_metric_from_two_automations_keeps_raw_and_stores_the_rest(self):
        # Raw automation first, then the hourly one carrying the same vital
        # plus sleep-adjacent sums: the vital keeps its raw record and the
        # rest of the export is not thrown away.
        hs.store(export([("18:42:10", 41)], "raw", name="cardio_recovery"))
        hourly = export([("18:00:00", 41)], "hourly", name="cardio_recovery")
        hourly["data"]["metrics"] += export([("01:00:00", 10)], "hourly")["data"]["metrics"]
        hs.store(hourly)
        day = self.day()
        self.assertEqual([p["date"][11:19] for p in day["metrics"]["cardio_recovery"]], ["18:42:10"])
        self.assertEqual([p["qty"] for p in day["metrics"]["step_count"]], [10])
        # and raw arriving after hourly replaces it
        hs.store(export([("09:00:00", 50)], "hourly", name="vo2_max"))
        hs.store(export([("09:13:00", 51)], "raw", name="vo2_max"))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["vo2_max"]], [51])

    def test_offset_hourly_buckets_from_export_start_do_not_sink_the_export(self):
        # The day already has clock-hour buckets; a later export starting at
        # 22:29:26 re-sends that tail as offset buckets plus a new day.
        hs.store(export([("22:00:00", 100), ("23:00:00", 50)]))
        offset = export([("22:29:26", 70), ("23:29:26", 30)])
        offset["data"]["metrics"][0]["data"].append(
            {"date": "2026-06-02 01:00:00 +0000", "qty": 12})
        hs.store(offset)
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [100, 50])
        nxt = json.loads((self.root / "daily/2026-06-02.json").read_text())
        self.assertEqual([p["qty"] for p in nxt["metrics"]["step_count"]], [12])
        # genuine raw samples (varying seconds) still may not mix
        with self.assertRaises(ValueError):
            hs.store(export([("22:10:01", 3), ("22:41:17", 4)]))

    def test_legacy_daily_is_not_mixed_into_hourly(self):
        hs.store(export([("00:00:00", 100)]))
        with self.assertRaises(ValueError):
            hs.store(export([("01:00:00", 10)], "hourly"))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [100])

    def test_explicit_hourly_does_not_accept_raw_samples(self):
        with self.assertRaises(ValueError):
            hs.store(export([("01:00:01", 3)], "hourly"))

    def test_explicit_raw_hour_aligned_samples_are_folded(self):
        hs.store(export([("01:00:00", 3), ("01:00:00", 4)], "raw"))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [7])

    def test_http_raw_accumulating_requires_full_timestamps_before_writes(self):
        hs.store(export([("01:00:01", 10)], "raw"))
        path = self.root / "daily/2026-06-01.json"
        before = path.read_bytes()
        self.start_server()
        for stamp in ("2026-06-02", "2026-06-02T01:00"):
            with self.subTest(stamp=stamp):
                payload = export([("01:00:01", 20)], "raw")
                payload["data"]["metrics"][0]["data"].append({"date": stamp, "qty": 100})
                status = self.request(payload)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.root / "daily/2026-06-02.json").exists())
                self.assertEqual(status, 400)

    def test_date_only_daily_and_legacy_nonraw_points_remain_supported(self):
        for mode in (None, "daily"):
            with self.subTest(mode=mode):
                payload = export([("00:00:00", 100)], mode)
                payload["data"]["metrics"][0]["data"][0]["date"] = "2026-06-01"
                hs.store(payload)
                self.assertEqual(self.day()["metrics"]["step_count"][0]["qty"], 100)
        payload = export([("00:00:00", 60)], "raw", name="resting_heart_rate")
        payload["data"]["metrics"][0]["data"][0]["date"] = "2026-06-01"
        hs.store(payload)
        self.assertEqual(self.day()["metrics"]["resting_heart_rate"][0]["qty"], 60)

    def test_legacy_raw_samples_are_not_mixed_with_folded_buckets(self):
        daily = self.root / "daily"
        daily.mkdir()
        points = export([("01:00:01", 3), ("01:00:02", 4)])["data"]["metrics"][0]["data"]
        (daily / "2026-06-01.json").write_text(json.dumps({"metrics": {"step_count": points}}))
        hs.store(export([("02:00:01", 5)], "raw"))
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [7, 5])

    def test_sleep_without_span_is_idempotent(self):
        payload = {"metrics": [{"name": "sleep_analysis", "data": [
            {"date": "2026-06-01", "totalSleep": 7}]}]}
        hs.store(payload)
        hs.store(payload)
        self.assertEqual(len(self.day()["metrics"]["sleep_analysis"]), 1)

    def test_http_multi_posts_are_idempotent_and_partial_hourly_is_preserved(self):
        self.start_server()
        full = export([("01:00:00", 10), ("02:00:00", 20)], "hourly")
        self.assertEqual(self.request(full), 200)
        self.assertEqual(self.request(full, path="/healthexport", chunked=True), 200)
        self.assertEqual(self.request(export([("02:00:00", 25)], "hourly")), 200)
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [10, 25])
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(conn.close)
        for path in ("/ping", "/healthexport/ping"):
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

    def test_http_raw_fold_and_sleep_spans(self):
        self.start_server()
        payload = export([("01:00:01", 3), ("01:00:02", 4), ("02:05:00", 5)], "raw")
        self.assertEqual(self.request(payload), 200)
        self.assertEqual(self.request(payload), 200)
        self.assertEqual([p["qty"] for p in self.day()["metrics"]["step_count"]], [7, 5])
        full = {"date": "2026-06-01", "sleepStart": "2026-05-31 22:00:00 +0000",
                "sleepEnd": "2026-06-01 06:00:00 +0000", "totalSleep": 8}
        shorter = dict(full, sleepStart="2026-06-01 00:00:00 +0000", totalSleep=6)
        nap = dict(full, sleepStart="2026-06-01 13:00:00 +0000",
                   sleepEnd="2026-06-01 14:00:00 +0000", totalSleep=1)
        for points in ([full], [shorter, nap], [shorter, nap]):
            self.assertEqual(self.request({"metrics": [{"name": "sleep_analysis", "data": points}]}), 200)
        self.assertEqual([p["totalSleep"] for p in self.day()["metrics"]["sleep_analysis"]], [8, 1])

    def test_invalid_later_day_does_not_mutate_earlier_day(self):
        hs.store(export([("01:00:00", 10)], "hourly"))
        before = self.day()
        payload = export([("01:00:00", 20)], "daily")
        payload["data"]["metrics"][0]["data"].extend([
            {"date": "2026-06-02 01:00:00 +0000", "qty": 3},
            {"date": "2026-06-02 02:00:00 +0000", "qty": 4}])
        with self.assertRaises(ValueError):
            hs.store(payload)
        self.assertEqual(self.day(), before)
        self.assertFalse((self.root / "daily/2026-06-02.json").exists())

    def test_accumulating_data_requires_numeric_quantity(self):
        payload = export([("01:00:01", 1)], "raw")
        del payload["data"]["metrics"][0]["data"][0]["qty"]
        with self.assertRaises(ValueError):
            hs.store(payload)

    def test_datapoint_units_are_preserved_when_metric_units_absent(self):
        payload = export([("01:00:00", 1)], "hourly")
        metric = payload["data"]["metrics"][0]
        del metric["units"]
        metric["data"][0]["units"] = "count"
        hs.store(payload)
        self.assertEqual(self.day()["metrics"]["step_count"][0]["units"], "count")

    def test_http_invalid_later_datapoint_units_do_not_mutate_any_day(self):
        hs.store(export([("01:00:00", 10)], "hourly"))
        path = self.root / "daily/2026-06-01.json"
        before = path.read_bytes()
        self.start_server()
        for metric_units in (None, "count"):
            with self.subTest(metric_units=metric_units):
                payload = export([("01:00:00", 20)], "hourly")
                metric = payload["data"]["metrics"][0]
                metric["units"] = metric_units
                metric["data"].append({"date": "2026-06-02 01:00:00 +0000",
                                       "qty": 100, "units": {"name": "count"}})
                status = self.request(payload)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.root / "daily/2026-06-02.json").exists())
                self.assertEqual(status, 400)

    def test_http_malformed_length_returns_400(self):
        self.start_server()
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(conn.close)
        conn.request("POST", "/health", body=b"{}", headers={
            "x-api-key": "synthetic-key", "Content-Length": "invalid"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        response.read()

    def test_raw_fold_rejects_mixed_units_in_one_hour(self):
        payload = export([("01:00:01", 1), ("01:00:02", 2)], "raw")
        metric = payload["data"]["metrics"][0]
        del metric["units"]
        metric["data"][0]["units"] = "m"
        metric["data"][1]["units"] = "km"
        with self.assertRaises(ValueError):
            hs.store(payload)

    def test_iso_sleep_spans_keep_fuller_record(self):
        full = {"date": "2026-06-01", "sleepStart": "2026-05-31T22:00:00Z",
                "sleepEnd": "2026-06-01T06:00:00Z", "totalSleep": 8}
        shorter = dict(full, sleepStart="2026-06-01T00:00:00Z", totalSleep=6)
        for point in (full, shorter):
            hs.store({"metrics": [{"name": "sleep_analysis", "data": [point]}]})
        self.assertEqual([p["totalSleep"] for p in self.day()["metrics"]["sleep_analysis"]], [8])

    def test_fractional_raw_samples_fold_to_one_true_hour_bucket(self):
        hs.store(export([("01:00:00.123", 3), ("01:00:00.456", 4)]))
        points = self.day()["metrics"]["step_count"]
        self.assertEqual([(p["date"], p["qty"]) for p in points],
                         [("2026-06-01 01:00:00 +0000", 7)])

    def test_http_wrong_key_with_malformed_length_still_returns_403(self):
        self.start_server()
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(conn.close)
        conn.request("POST", "/health", body=b"{}", headers={
            "x-api-key": "wrong", "Content-Length": "invalid"})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()


if __name__ == "__main__":
    unittest.main()
