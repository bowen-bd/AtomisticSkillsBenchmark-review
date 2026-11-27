"""Run with `python -B -m unittest scripts.atomisticskills.test_issue_1`.

Uses the standard library except for the scientific checks that need pymatgen.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import warnings

ROOT = Path(__file__).resolve().parents[2]
MATERIALS = ROOT / "tasks/materials-science"
ALLOWLIST = {
    "ghs-hazard-summary": ["pubchem.ncbi.nlm.nih.gov"],
    "nist-janaf-query": ["webbook.nist.gov"],
    "nmr-reaction-kinetics": ["cactus.nci.nih.gov", "www.nmrdb.org"],
    **dict.fromkeys(("convex-hull-stability", "mgo-vacancy-energy",
                     "synthesis-precursor-recommendation", "xrd-mixture-phase-fit"),
                    ["api.materialsproject.org"]),
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.parent.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IssueOneTests(unittest.TestCase):
    def test_network_policies(self):
        seen_allowlists = set()
        configs = sorted(ROOT.glob("tasks/*/*/task.toml"))
        self.assertEqual(len(configs), 31)
        for path in configs:
            with self.subTest(task=path.parent.name):
                config = tomllib.loads(path.read_text())
                for section in ("environment", "verifier", "agent"):
                    block = config[section]
                    hosts = ALLOWLIST.get(path.parent.name) if section == "agent" else None
                    self.assertEqual(block["network_mode"], "allowlist" if hosts else "no-network")
                    self.assertNotIn("allow_internet", block)
                    if hosts:
                        self.assertCountEqual(block["allowed_hosts"], hosts)
                        seen_allowlists.add(path.parent.name)
                    else:
                        self.assertNotIn("allowed_hosts", block)
                self.assertEqual(config["verifier"]["environment_mode"], "separate")
        self.assertEqual(seen_allowlists, ALLOWLIST.keys())

    def test_verifier_wrapper_exit_codes(self):
        wrappers = sorted(ROOT.glob("tasks/*/*/tests/test.sh"))
        self.assertEqual(len(wrappers), 31)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            logdir = base / "capture"
            verifier = logdir / "verifier"
            verifier.mkdir(parents=True)
            reward_txt = verifier / "reward.txt"
            reward_json = verifier / "reward.json"
            pytest = base / "pytest"
            pytest.write_text('#!/bin/sh\nexit "$PYTEST_STATUS"\n')
            pytest.chmod(0o755)
            wrapper = base / "test.sh"
            for path in wrappers:
                # Redirect logs before running; never read or write real /logs.
                wrapper.write_text(path.read_text().replace("/logs", str(logdir)))
                self.assertNotIn("/logs", wrapper.read_text())
                for status in (0, 1, 2, 3, 4, 5, 124):
                    with self.subTest(task=path.parent.parent.name, status=status):
                        reward_txt.write_text("stale\n")
                        reward_json.write_text("{}\n")
                        proc = subprocess.run(
                            ["bash", str(wrapper)], cwd=base, capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{base}:{os.environ['PATH']}",
                                 "PYTEST_STATUS": str(status)}, timeout=10,
                        )
                        self.assertEqual(proc.returncode, 0 if status < 2 else status, proc.stderr)
                        self.assertFalse(reward_json.exists())
                        if status < 2:
                            self.assertEqual(reward_txt.read_text(), f"{1 - status}\n")
                        else:
                            self.assertFalse(reward_txt.exists())
                            self.assertIn("leaving reward unset", proc.stderr)

    def test_electrochemical_checks(self):
        tests = MATERIALS / "electrochemical-window/tests"
        verifier = load_module(tests / "test_outputs.py")
        targets = read_json(tests / "expected.json")["targets"]
        reference = {item["path"]: item["expected"] for item in targets}
        verifier.check_result(reference, targets)
        for changes, message in (
            ({"window_width_V": reference["window_width_V"] + 0.01}, "Window width"),
            ({"reduction_voltage_V": reference["reduction_voltage_V"] + 0.02}, "reduction_voltage_V"),
            ({"stable": not reference["stable"]}, "stability flag"),
            ({"target_energy_per_atom_eV": reference["target_energy_per_atom_eV"] + 0.002}, "target_energy"),
            ({"competing_phase_count": float(reference["competing_phase_count"])}, "integer count"),
            ({"energy_above_hull_meV_atom": -0.001}, "nonnegative"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(AssertionError, message):
                verifier.check_result(reference | changes, targets)

    def test_equilibrium_window_tracks_the_supplied_phase(self):
        if importlib.util.find_spec("pymatgen") is None:
            self.skipTest("requires pymatgen and SciPy")
        window = load_module(MATERIALS / "electrochemical-window/solution/window.py")

        def phase(name, formula, energy, count):
            return {"id": name, "formula": formula, "energy_eV": energy,
                    "n_sites": count, "converged": True}

        competitors = [phase("li", "Li", 0, 1), phase("p", "P", 0, 1),
                       phase("s", "S", 0, 1), phase("lips", "LiPS", -7, 3),
                       phase("li3ps", "Li3PS", -10, 5)]
        target = phase("target_cif", "Li2PS", -9, 4)
        reference = window.equilibrium_window(competitors + [target])
        self.assertTrue(reference["stable"])
        self.assertAlmostEqual(reference["reduction_voltage_V"], 1.0)
        self.assertAlmostEqual(reference["oxidation_voltage_V"], 2.0)
        polymorph = phase("other_polymorph", "Li2PS", -9.004, 4)
        actual = window.equilibrium_window(competitors + [target, polymorph])
        self.assertFalse(actual["stable"])
        self.assertAlmostEqual(actual["energy_above_hull_meV_atom"], 1.0)
        self.assertIsNone(actual["reduction_voltage_V"])
        self.assertIsNone(actual["oxidation_voltage_V"])
        self.assertIsNone(actual["independent_bounds_V"])

    def test_nist_sample_does_not_disclose_reference(self):
        task = MATERIALS / "nist-janaf-query"
        reference = read_json(task / "tests/expected.json")
        instruction = (task / "instruction.md").read_text().casefold()
        for value in [reference["formula"], reference["name"]["canonical"],
                      reference["nist_id"]["canonical"],
                      *(item["value"] for item in reference["thermochemistry"].values())]:
            self.assertNotIn(str(value).casefold(), instruction)

    def test_nist_identity_units_and_values(self):
        tests = MATERIALS / "nist-janaf-query/tests"
        verifier = load_module(tests / "test_outputs.py")
        expected = read_json(tests / "expected.json")
        reference = {
            "formula": expected["formula"], "name": expected["name"]["canonical"],
            "nist_id": expected["nist_id"]["canonical"],
            "thermochemistry": {key: {"value": row["value"], "units": row["units"]}
                                for key, row in expected["thermochemistry"].items()},
        }
        enthalpy, entropy = reference["thermochemistry"]
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch.multiple(verifier, RESULT_PATH=result, EXPECTED_PATH=tests / "expected.json"):
                result.write_text(json.dumps(reference))
                verifier.test_result_identity_and_thermochemistry()
                aliases = deepcopy(reference)
                aliases["name"] = expected["name"]["aliases"][0]
                aliases["nist_id"] = reference["nist_id"].removeprefix("C")
                aliases["thermochemistry"][enthalpy]["units"] = "kJ mol-1"
                aliases["thermochemistry"][entropy]["units"] = "J mol^-1 K^-1"
                result.write_text(json.dumps(aliases))
                verifier.test_result_identity_and_thermochemistry()

                invalid_units = deepcopy(reference)
                invalid_units["thermochemistry"][enthalpy]["units"] = "J/mol"
                invalid_value = deepcopy(reference)
                invalid_value["thermochemistry"][enthalpy]["value"] += 1
                missing_units = deepcopy(reference)
                del missing_units["thermochemistry"][entropy]["units"]
                for candidate, error in (
                    (reference | {"formula": "unknown"}, "formula is"),
                    (reference | {"name": "unknown"}, "name is"),
                    (reference | {"nist_id": "C0"}, "nist_id is"),
                    (invalid_units, "units is"), (invalid_value, "value is"),
                    (missing_units, "missing.*units"),
                ):
                    with self.subTest(error=error), self.assertRaisesRegex(AssertionError, error):
                        result.write_text(json.dumps(candidate))
                        verifier.test_result_identity_and_thermochemistry()

    def test_synthesis_counts_only_distinct_supported_routes(self):
        if importlib.util.find_spec("pymatgen") is None:
            self.skipTest("requires pymatgen")
        task = MATERIALS / "synthesis-precursor-recommendation"
        verifier = load_module(task / "tests/test_outputs.py")
        expected = read_json(task / "tests/expected.json")
        curated = read_json(task / "solution/curated_combinations.json")
        routes = [r["representative_formulas"] for r in curated["routes"]]
        minimum = expected["required_routes_count"]
        reference = {"target_formula": curated["target_formula"]}
        extras = [routes[0][::-1], ["H2O", "CO2"], None]
        for combinations in (routes[:minimum], routes, extras + routes[:minimum]):
            verifier.check_result(reference | {"precursor_combinations": combinations}, expected)
        with self.assertRaisesRegex(AssertionError, f"found {minimum - 1}"):
            verifier.check_result(reference | {"precursor_combinations": extras + routes[:minimum - 1]},
                                  expected)

    def test_crashes_are_reported_even_without_reward(self):
        report = load_module(ROOT / "scripts/atomisticskills/pass_rates.py")
        task = "tasks/materials-science/electrochemical-window"
        with tempfile.TemporaryDirectory() as tmp:
            for index, (crashed, reward) in enumerate(((True, None), (True, "0"), (False, "1"))):
                trial = Path(tmp) / "job" / str(index)
                (trial / "verifier").mkdir(parents=True)
                (trial / "config.json").write_text(json.dumps({
                    "task": {"path": task}, "agent": {"name": "terminus-2"},
                }))
                (trial / "result.json").write_text(json.dumps({
                    "exception_info": {"exception_type": "VerifierError"} if crashed else None,
                }))
                if reward is not None:
                    (trial / "verifier/reward.txt").write_text(reward)
            crashes = []
            cwd = Path.cwd()
            try:
                os.chdir(ROOT)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    scored = report.collect([tmp], crashes)
            finally:
                os.chdir(cwd)
        self.assertEqual([trial["reward"] for trial in scored], [1.0])
        self.assertEqual(crashes, [{"task": "electrochemical-window", "agent": "terminus-2"}] * 2)


if __name__ == "__main__":
    unittest.main()
