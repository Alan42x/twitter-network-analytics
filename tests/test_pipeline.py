import json
import tempfile
import unittest
from pathlib import Path

from analysis.reference import analyze
from backend.app import Snapshot, dispatch

ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.content = analyze(ROOT / "tests/fixtures/edges.txt")
        cls.snapshot = Snapshot(cls.content)

    def payload(self, path, query=None):
        result = dispatch("GET", path, query, self.snapshot)
        return result["statusCode"], json.loads(result["body"])

    def test_dedup_direction_zero_degrees_and_loop_only_users(self):
        expected = {"1": (1, 1), "2": (2, 1), "3": (0, 1), "4": (0, 0),
                    "5": (0, 1), "6": (1, 0), "7": (0, 0)}
        self.assertEqual({u["user_id"]: (u["followers"], u["followees"])
                          for u in self.content["users"]}, expected)
        summary = self.content["summary"]
        self.assertEqual(summary["unique_edges"], 4)
        self.assertEqual(summary["duplicate_edges"], 2)
        self.assertEqual(summary["self_loop_rows"], 2)
        self.assertEqual(summary["invalid_rows"], 3)
        self.assertEqual(summary["blank_rows"], 1)
        self.assertEqual(summary["raw_lines"], 12)

    def test_ids_normalization_and_missing_users(self):
        status, user = self.payload("/api/users/0002")
        self.assertEqual(status, 200)
        self.assertEqual(user["followers"], 2)
        status, result = self.payload("/api/users", {"ids": "1,1,99"})
        self.assertEqual(status, 200)
        self.assertEqual(len(result["users"]), 1)
        self.assertEqual(result["missing_ids"], ["99"])
        self.assertEqual(self.payload("/api/users/99")[0], 404)

    def test_pagination_sort_and_prefix(self):
        status, result = self.payload("/api/users", {"sort": "followers", "limit": "2"})
        self.assertEqual(status, 200)
        self.assertEqual([u["user_id"] for u in result["users"]], ["2", "1"])
        self.assertEqual(result["next_offset"], 2)
        _, result = self.payload("/api/users", {"search": "7"})
        self.assertEqual(result["total"], 1)
        _, result = self.payload("/api/users", {"offset": "100"})
        self.assertEqual(result["users"], [])
        self.assertIsNone(result["next_offset"])

    def test_api_rejects_bad_parameters_and_methods(self):
        for query in [{"limit": "201"}, {"offset": "-1"}, {"sort": "bad"},
                      {"search": "<script>"}, {"ids": "1,,2"}, {"limit": "abc"}]:
            self.assertEqual(self.payload("/api/users", query)[0], 400, query)
        self.assertEqual(self.payload("/api/users/0")[0], 400)
        self.assertEqual(self.payload("/api/unknown")[0], 404)
        self.assertEqual(dispatch("POST", "/api/users", snapshot=self.snapshot)["statusCode"], 405)

    def test_snapshot_corruption_is_rejected(self):
        bad = json.loads(json.dumps(self.content))
        bad["users"][0]["followers"] = 42
        with self.assertRaises(ValueError):
            Snapshot(bad)

    def test_empty_input_is_well_defined(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.txt"
            path.touch()
            snapshot = analyze(path)
        self.assertEqual(snapshot["summary"]["total_users"], 0)
        self.assertEqual(snapshot["summary"]["unique_edges"], 0)
        Snapshot(snapshot)

    def test_unicode_line_terminators_are_invalid_records_not_ascii_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminators.txt"
            path.write_text("1 2\u2028\n3 4\u0085\n5 6\u2029\n\u2028\n\u0085\n\u2029\n")
            result = analyze(path)
        self.assertEqual(result["summary"]["raw_lines"], 6)
        self.assertEqual(result["summary"]["invalid_rows"], 6)
        self.assertEqual(result["summary"]["blank_rows"], 0)
        self.assertEqual(result["users"], [])


if __name__ == "__main__":
    unittest.main()
