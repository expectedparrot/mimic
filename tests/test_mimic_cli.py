from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from mimic.calibration import DEFAULT_RHO_VALUES, FitResult, regularization_selection_score
from mimic.cli import main
from mimic.parsing import load_results_ep_to_pandas


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def mini_metadata() -> dict:
    return {
        "wave": "T1",
        "battery": "TEST",
        "topic": "test topic",
        "context": "A small test survey.",
        "items": {
            "a": {"variable": "A", "item_text": "Item A", "question_stem": "How important is this?"},
            "b": {"variable": "B", "item_text": "Item B", "question_stem": "How important is this?"},
        },
        "option_codes": [1, 2],
        "option_labels": ["Yes", "No"],
        "truth": {"a": [0.7, 0.3], "b": [0.4, 0.6]},
    }


def write_raw(path: Path) -> None:
    rows = [
        {
            "scenario.job_id": "s1",
            "answer.resp": json.dumps({"persona": "yes type", "probabilities": {"a": [0.9, 0.1], "b": [0.2, 0.8]}}),
        },
        {
            "scenario.job_id": "s2",
            "answer.resp": json.dumps({"persona": "no type", "probabilities": {"a": [0.2, 0.8], "b": [0.8, 0.2]}}),
        },
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario.job_id", "answer.resp"])
        writer.writeheader()
        writer.writerows(rows)


class MimicCliTests(unittest.TestCase):
    def test_results_ep_zip_file_uses_edsl_git_loader(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            results_path = root / "mini.results.ep"
            with zipfile.ZipFile(results_path, "w") as zf:
                zf.writestr("manifest.json", "{}")

            calls: list[str] = []

            class FakeLoadedResults:
                def to_pandas(self, remove_prefix: bool = False) -> pd.DataFrame:
                    return pd.DataFrame([{"scenario.job_id": "s1", "answer.resp": "{}"}])

            class FakeGit:
                @staticmethod
                def load(path: str) -> FakeLoadedResults:
                    calls.append(path)
                    return FakeLoadedResults()

            class FakeResults:
                git = FakeGit()

                @staticmethod
                def from_dict(data: dict) -> FakeLoadedResults:
                    raise AssertionError("zip package should not be parsed as JSON")

                @staticmethod
                def load(path: str) -> FakeLoadedResults:
                    raise AssertionError("zip package should not use extension-appending loader")

            fake_edsl = types.SimpleNamespace(Results=FakeResults)
            with patch.dict(sys.modules, {"edsl": fake_edsl}):
                df = load_results_ep_to_pandas(results_path)

            self.assertEqual(calls, [str(results_path)])
            self.assertEqual(df.loc[0, "scenario.job_id"], "s1")

    def test_default_rho_grid_and_selection_penalize_collapse(self) -> None:
        self.assertIn(0.1, DEFAULT_RHO_VALUES)
        self.assertIn(0.3, DEFAULT_RHO_VALUES)
        collapsed = FitResult(weights=pd.Series([1.0]).to_numpy(), held_in_residual=0.1, effective_support=4.0, converged=True)
        diffuse = FitResult(weights=pd.Series([1.0]).to_numpy(), held_in_residual=0.104, effective_support=85.0, converged=True)
        self.assertLess(regularization_selection_score(diffuse), regularization_selection_score(collapsed))

    def test_support_build_parse_and_loo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            metadata_path = root / "metadata.json"
            raw_path = root / "raw.csv"
            write_json(metadata_path, mini_metadata())
            write_raw(raw_path)

            self.assertEqual(
                main(["support", "build", "--metadata", str(metadata_path), "--strategy", "pattern-coverage", "--tag", "mini", "--n-support", "4", "--out", str(root / "prompts")]),
                0,
            )
            self.assertTrue((root / "prompts" / "mini.jsonl").exists())
            self.assertTrue((root / "prompts" / "mini_design.csv").exists())

            self.assertEqual(main(["support", "parse", "--raw", str(raw_path), "--metadata", str(metadata_path), "--tag", "mini", "--out", str(root / "banks")]), 0)
            probs = pd.read_csv(root / "banks" / "mini_probabilities.csv")
            self.assertEqual(set(probs["item"]), {"a", "b"})
            self.assertEqual(probs["support_id"].nunique(), 2)

            self.assertEqual(main(["loo", "--support", str(root / "banks" / "mini_probabilities.csv"), "--metadata", str(metadata_path), "--tag", "mini", "--out", str(root / "derived")]), 0)
            summary = pd.read_csv(root / "derived" / "mini_generated_support_summary.csv")
            self.assertIn("generated support mixture", set(summary["method"]))
            self.assertEqual(main(["support", "inspect", "--bank", str(root / "banks" / "mini_probabilities.csv")]), 0)
            self.assertEqual(main(["compare", "--run", "mini=Mini:Test bank", "--derived", str(root / "derived"), "--out", str(root / "derived" / "comparison.csv")]), 0)
            comparison = pd.read_csv(root / "derived" / "comparison.csv")
            self.assertEqual(set(comparison["bank"]), {"Test bank"})
            self.assertEqual(main(["report", "--tag", "mini", "--derived", str(root / "derived"), "--out", str(root / "derived" / "mini.md")]), 0)
            self.assertTrue((root / "derived" / "mini.md").read_text().startswith("# mimic report: mini"))
            self.assertEqual(main(["report-data", "build", "--derived", str(root / "derived"), "--out", str(root / "report_data")]), 0)
            manifest = json.loads((root / "report_data" / "manifest.json").read_text())
            self.assertEqual(manifest["facts"]["support_banks"], 1)
            self.assertTrue((root / "report_data" / "support_bank_summary.csv").exists())
            self.assertTrue((root / "report_data" / "method_comparison.csv").exists())
            self.assertTrue((root / "report_data" / "diagnostics_flags.csv").exists())
            self.assertIn("mini", (root / "report_data" / "prose_facts.md").read_text())

    def test_fit_and_predict(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            metadata_path = root / "metadata.json"
            raw_path = root / "raw.csv"
            write_json(metadata_path, mini_metadata())
            write_raw(raw_path)
            self.assertEqual(main(["support", "parse", "--raw", str(raw_path), "--metadata", str(metadata_path), "--tag", "mini", "--out", str(root / "banks")]), 0)
            self.assertEqual(
                main(
                    [
                        "fit",
                        "--support",
                        str(root / "banks" / "mini_probabilities.csv"),
                        "--metadata",
                        str(metadata_path),
                        "--exclude-item",
                        "a",
                        "--tag",
                        "mini_holdout_a",
                        "--out",
                        str(root / "derived"),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "predict",
                        "--support",
                        str(root / "banks" / "mini_probabilities.csv"),
                        "--weights",
                        str(root / "derived" / "mini_holdout_a_weights.csv"),
                        "--metadata",
                        str(metadata_path),
                        "--item",
                        "a",
                        "--out",
                        str(root / "derived" / "prediction.csv"),
                    ]
                ),
                0,
            )
            pred = pd.read_csv(root / "derived" / "prediction.csv")
            self.assertEqual(set(pred["item"]), {"a"})

    def test_project_incremental_authoring(self) -> None:
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            os.chdir(root)
            try:
                self.assertEqual(main(["init"]), 0)
                self.assertEqual(main(["battery", "create", "--battery-id", "demo", "--wave", "T1", "--battery", "DEMO", "--topic", "demo topic", "--context", "demo context"]), 0)
                self.assertEqual(
                    main(
                        [
                            "question",
                            "add",
                            "--battery",
                            "demo",
                            "--item",
                            "q1",
                            "--question-stem",
                            "Stem?",
                            "--item-text",
                            "Text",
                            "--option-code",
                            "1",
                            "--option",
                            "Yes",
                            "--option-code",
                            "2",
                            "--option",
                            "No",
                        ]
                    ),
                    0,
                )
                self.assertEqual(main(["marginal", "add", "--battery", "demo", "--item", "q1", "--proportion", "0.25", "--proportion", "0.75"]), 0)
                self.assertEqual(main(["battery", "compile", "--battery", "demo", "--path", str(root / "compiled.json")]), 0)
                compiled = json.loads((root / "compiled.json").read_text())
                self.assertEqual(compiled["truth"]["q1"], [0.25, 0.75])
            finally:
                os.chdir(cwd)

    def test_support_build_from_generic_design(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            metadata_path = root / "metadata.json"
            design_path = root / "design.json"
            write_json(metadata_path, mini_metadata())
            write_json(
                design_path,
                {
                    "components": [
                        {
                            "type": "axes",
                            "name": "latent_axis_grid",
                            "axes": {
                                "outlook": ["optimistic", "skeptical"],
                                "response_style": ["moderate", "strong"],
                            },
                            "sampler": {"method": "maximin", "n": 3, "seed": 17},
                            "guidance": "Use the response scale literally.",
                        },
                        {"type": "option-coverage", "name": "coverage", "n": 2},
                    ]
                },
            )
            self.assertEqual(
                main(["support", "build", "--metadata", str(metadata_path), "--design", str(design_path), "--tag", "generic", "--n-support", "5", "--out", str(root / "prompts")]),
                0,
            )
            rows = [json.loads(line) for line in (root / "prompts" / "generic.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 5)
            self.assertIn("axis_values", rows[0])
            design = pd.read_csv(root / "prompts" / "generic_design.csv")
            self.assertIn("outlook", design.columns)
            self.assertIn("response_style", design.columns)
            self.assertEqual(set(design["design_type"]), {"axes", "option_coverage"})

    def test_guides_and_artifact_next(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            metadata_path = root / "metadata.json"
            design_path = root / "design.json"
            write_json(metadata_path, mini_metadata())
            write_json(design_path, {"axes": {"outlook": ["optimistic", "skeptical"]}, "sampler": {"method": "full-factorial", "n": 2}})
            self.assertEqual(main(["guide", "workflow"]), 0)
            self.assertEqual(main(["guide", "--topic", "ep-boundary"]), 0)
            self.assertEqual(
                main(
                    [
                        "next",
                        "--tag",
                        "demo",
                        "--metadata",
                        str(metadata_path),
                        "--design",
                        str(design_path),
                        "--prompt-dir",
                        str(root / "prompts"),
                        "--raw-dir",
                        str(root / "raw"),
                        "--derived-dir",
                        str(root / "derived"),
                    ]
                ),
                0,
            )
            self.assertEqual(main(["support", "build", "--metadata", str(metadata_path), "--design", str(design_path), "--tag", "demo", "--out", str(root / "prompts")]), 0)
            self.assertEqual(main(["next", "--tag", "demo", "--metadata", str(metadata_path), "--prompt-dir", str(root / "prompts"), "--raw-dir", str(root / "raw"), "--derived-dir", str(root / "derived")]), 0)

    def test_support_export_writes_jobs_ep(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            os.environ["EDSL_LOG_DIR"] = str(root / "edsl_logs")
            metadata_path = root / "metadata.json"
            write_json(metadata_path, mini_metadata())
            self.assertEqual(main(["support", "build", "--metadata", str(metadata_path), "--strategy", "archetype", "--tag", "mini", "--n-support", "2", "--out", str(root / "prompts")]), 0)
            jobs_path = root / "prompts" / "mini.jobs.ep"
            self.assertEqual(main(["support", "export", "--prompts", str(root / "prompts" / "mini.jsonl"), "--path", str(jobs_path), "--limit", "1"]), 0)
            self.assertTrue(jobs_path.exists())
            manifest = json.loads((root / "prompts" / "manifest.json").read_text())
            self.assertEqual(manifest["run_contract"]["owner"], "agent")
            self.assertTrue(manifest["run_command"].startswith("edsl run "))


if __name__ == "__main__":
    unittest.main()
