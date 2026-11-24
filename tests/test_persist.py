"""Zapis plików stanu z kilku wątków (24.11: równoczesne zmiany pingu wywaliły wątek na counters.json.tmp)."""

import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deco_monitor", "app"))

import main  # noqa: E402

THREADS = 16
ROUNDS = 40


def run_parallel(fn):
    barrier = threading.Barrier(THREADS)
    errors = []

    def worker(i):
        barrier.wait()
        try:
            for r in range(ROUNDS):
                fn(i, r)
        except Exception as exc:  # noqa: BLE001 - test zbiera każdy wyjątek z wątku
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors


class SaveJsonTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "counters.json")

    def test_parallel_writes_do_not_fail(self):
        errors = run_parallel(lambda i, r: main.save_json(self.path, {"w": i, "r": r}))
        self.assertEqual(errors, [])
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("w", json.load(fh))
        self.assertEqual(os.listdir(self.dir), ["counters.json"])  # żadnych porzuconych *.tmp

    def test_failed_dump_leaves_old_file_and_no_tmp(self):
        main.save_json(self.path, {"a": 1})
        with self.assertRaises(TypeError):
            main.save_json(self.path, {"a": object()})
        self.assertEqual(main.load_json(self.path, None), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["counters.json"])


class BumpTest(unittest.TestCase):
    def test_parallel_bumps_are_all_counted_and_saved(self):
        data_dir = tempfile.mkdtemp()
        fake = SimpleNamespace(counters={}, _counters_lock=threading.Lock(), data_dir=data_dir, set=mock.Mock())
        errors = run_parallel(lambda i, r: main.Monitor.bump(fake, "zaniki_ping"))
        self.assertEqual(errors, [])
        self.assertEqual(fake.counters["zaniki_ping"], THREADS * ROUNDS)
        saved = main.load_json(os.path.join(data_dir, "counters.json"), {})
        self.assertEqual(saved["zaniki_ping"], THREADS * ROUNDS)
        self.assertEqual(fake.set.call_count, THREADS * ROUNDS)


if __name__ == "__main__":
    unittest.main()
