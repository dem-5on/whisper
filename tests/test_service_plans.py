from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from whisper_service.plans import (
    ANALYSIS_KINDS,
    DEFAULT_PLANS,
    KIND_FEATURE,
    PlanConfigError,
    features_for_kinds,
    load_plans,
    plan_features,
    to_billing_mapping,
)


class PlanTests(unittest.TestCase):
    def test_placeholders_bundle_everything_in_one_allowance(self) -> None:
        pro = DEFAULT_PLANS["pro"]
        for kind in ANALYSIS_KINDS:
            self.assertIn(KIND_FEATURE[kind], pro.features)
        self.assertIn("cloud_transcription", pro.features)
        self.assertGreater(pro.cloud_minutes or 0, 0)
        self.assertEqual(DEFAULT_PLANS["free"].features, ())
        self.assertEqual(DEFAULT_PLANS["free"].cloud_minutes, 0)

    def test_operator_file_overrides_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plans.json"
            path.write_text(json.dumps({
                "team": {
                    "name": "Team",
                    "features": ["lecture_analysis", "translation"],
                    "cloud_minutes": 2000,
                    "analysis_minutes_per_stage": 2,
                    "period_days": 30,
                },
            }), encoding="utf-8")
            plans = load_plans(path)
        team = plans["team"]
        self.assertEqual(team.cloud_minutes, 2000)
        self.assertEqual(team.analysis_minutes_per_stage, 2)
        self.assertEqual(team.features, ("lecture_analysis", "translation"))

    def test_malformed_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(PlanConfigError):
                load_plans(missing)
            broken = Path(directory) / "broken.json"
            broken.write_text("{nope", encoding="utf-8")
            with self.assertRaises(PlanConfigError):
                load_plans(broken)
            bad_values = Path(directory) / "bad.json"
            bad_values.write_text(json.dumps({
                "x": {"name": "X", "features": ["ok"], "cloud_minutes": -5},
            }), encoding="utf-8")
            with self.assertRaises(PlanConfigError):
                load_plans(bad_values)

    def test_billing_adapters_keep_shapes_stable(self) -> None:
        mapping = to_billing_mapping(DEFAULT_PLANS)
        self.assertEqual(mapping["pro"], (DEFAULT_PLANS["pro"].features, 600))
        features = plan_features(DEFAULT_PLANS)
        self.assertIn("diarization", features["pro"])
        self.assertEqual(features_for_kinds(["notes", "translation"]), {"lecture_analysis", "translation"})
        with self.assertRaises(PlanConfigError):
            features_for_kinds(["teleport"])
