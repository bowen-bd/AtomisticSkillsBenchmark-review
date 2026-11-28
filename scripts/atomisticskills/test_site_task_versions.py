"""Tests for current-only website metrics and the separate historical archive.

Run with:
    python -B -m unittest scripts.atomisticskills.test_site_task_versions

The fixtures are synthetic: no real job output, task answer or verifier log is read.
`ran_on_shipped_version` is patched so the tests exercise classification without
depending on the manifest's release hashes or the optional `dirhash` package.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
MODEL = "openai/gpt-5.6-sol"
TASK_PATH = "tasks/chemistry/alpha"
TASK_SLUG = "alpha"


def load_site_module():
    """Load build_site.py by path, with its own directory importable."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("build_site_under_test", HERE / "build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = load_site_module()


class SiteTaskVersionFilterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.jobs = base / "jobs"
        self.out = base / "site"
        self.out.mkdir()
        self.archive = self.out / "archive" / "index.html"
        task_dir = self.repo / TASK_PATH
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(
            '[metadata]\ndifficulty = "easy"\nfield = "chemistry"\n', encoding="utf-8")
        (task_dir / "instruction.md").write_text("Synthetic instruction.\n", encoding="utf-8")
        self.jobs.mkdir()

        # A current trial is one whose checksum is accepted for the shipped task.
        self._version_patch = mock.patch.object(
            site, "ran_on_shipped_version",
            side_effect=lambda slug, task_dir, checksum: checksum in {"current", "verified"})
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)

        self._make_trial("current-pass", reward="1.0", checksum="current", skills=True)
        self._make_trial("current-fail", reward="0.0", checksum="current", skills=False)
        self._make_trial("old-revision", reward="1.0", checksum="old-revision", skills=True)
        self._make_trial("infra-error", reward="1.0", checksum="current", skills=True, crashed=True)
        self._make_trial("infra-timeout", reward=None, checksum="current", skills=False,
                         crashed=True, exception_type="AgentTimeoutError")
        self._make_trial("no-reward", reward=None, checksum="current", skills=False)

        self.single_model = [deepcopy(next(m for m in site.SITE_MODELS if m["model"] == MODEL))]

    def tearDown(self):
        site.ARCHIVE_ENABLED = False
        self._tmp.cleanup()

    def _make_trial(self, name, *, reward, checksum, skills, crashed=False,
                    exception_type="VerifierError"):
        trial = self.jobs / name
        (trial / "verifier").mkdir(parents=True)
        config = {
            "task": {"path": TASK_PATH},
            "agent": {
                "name": "terminus-2",
                "model_name": MODEL,
                "skills": ["AtomisticSkills"] if skills else [],
            },
        }
        (trial / "config.json").write_text(json.dumps(config), encoding="utf-8")
        result = {"task_checksum": checksum}
        if crashed:
            result["exception_info"] = {"exception_type": exception_type,
                                        "exception_message": "synthetic"}
        (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
        if reward is not None:
            (trial / "verifier" / "reward.txt").write_text(reward, encoding="utf-8")

    def test_benchmark_version_matches_readme(self):
        root = HERE.parents[1]
        version = "v" + (root / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^v[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
        self.assertEqual(site.BENCHMARK_VERSION, version)
        self.assertIn(f"[{version}](VERSION)", (root / "README.md").read_text(encoding="utf-8"))

    def test_default_collection_is_current_only(self):
        tasks, dropped = site.collect_trials([str(self.jobs)], str(self.repo), max_per_mode=3)
        trials = tasks[TASK_SLUG]["trials"]
        self.assertEqual({tr["trial_id"] for tr in trials}, {"current-pass", "current-fail"})
        self.assertEqual({tr["passed"] for tr in trials}, {True, False})
        self.assertEqual(dropped, 0)
        self.assertTrue(all(tr["exclusion"] is None for tr in trials))
        self.assertTrue(all(tr["on_shipped_version"] for tr in trials))

    def test_archive_records_keep_reason_and_never_invent_zero(self):
        records = {r["trial_id"]: r for r in site.collect_archive_records(
            [str(self.jobs)], str(self.repo))}
        self.assertEqual(set(records), {
            "old-revision", "infra-error", "infra-timeout", "no-reward"})

        self.assertEqual(records["old-revision"]["exclusion"], site.OFF_VERSION)
        self.assertEqual(records["old-revision"]["reward"], 1.0)

        # A stale reward.txt must not turn an infrastructure crash into a score.
        self.assertEqual(records["infra-error"]["exclusion"], site.INFRA)
        self.assertIsNone(records["infra-error"]["passed"])

        # AgentTimeoutError follows the same exception_info classification as
        # pass_rates.py; it is not a capability failure and has no reward.
        self.assertEqual(records["infra-timeout"]["exclusion"], site.INFRA)
        self.assertIsNone(records["infra-timeout"]["passed"])
        self.assertIsNone(records["infra-timeout"]["reward"])

        self.assertEqual(records["no-reward"]["exclusion"], site.NO_REWARD)
        self.assertIsNone(records["no-reward"]["passed"])
        self.assertIsNone(records["no-reward"]["reward"])

    def test_current_scored_trials_defensively_rejects_archive_rows(self):
        current = {"passed": True, "crashed": False, "on_shipped_version": True, "exclusion": None}
        archived = [
            {"passed": None, "crashed": False, "on_shipped_version": True, "exclusion": site.NO_REWARD},
            {"passed": False, "crashed": True, "on_shipped_version": True, "exclusion": None},
            {"passed": True, "crashed": False, "on_shipped_version": False, "exclusion": None},
            {"passed": True, "crashed": False, "on_shipped_version": True, "exclusion": site.OFF_VERSION},
        ]
        self.assertEqual(site.current_scored_trials([current] + archived), [current])

    def test_archive_page_is_conspicuous_and_handles_no_records(self):
        records = site.collect_archive_records([str(self.jobs)], str(self.repo))
        site.render_archive_page(records, str(self.archive))
        page = self.archive.read_text(encoding="utf-8")

        self.assertIn('role="alert"', page)
        self.assertIn('<meta name="robots" content="noindex, nofollow" />', page)
        self.assertIn("Historical Archive (not current)", page)
        self.assertIn("not current benchmark results", page)
        self.assertIn("ARCHIVE / EXCLUDED RECORDS", page)
        self.assertIn("Superseded task revision", page)
        self.assertIn("Infrastructure error (agent, verifier or timeout)", page)
        self.assertIn("No verifier reward recorded", page)
        self.assertIn("not scored", page)
        self.assertNotIn("FAILED (0.0)", page)

        empty = self.out / "empty-archive.html"
        site.render_archive_page([], str(empty))
        empty_page = empty.read_text(encoding="utf-8")
        self.assertIn("No archived records", empty_page)
        self.assertIn("not current benchmark results", empty_page)

    def test_summary_counts_only_current_scored_trials(self):
        with mock.patch.object(site, "SITE_MODELS", self.single_model):
            summary = site.collect_all_models_summary([str(self.jobs)], str(self.repo))
        row = next(r for r in summary if r["model"] == MODEL)
        self.assertEqual((row["ws_p"], row["ws_t"]), (1, 1))
        self.assertEqual((row["ns_p"], row["ns_t"]), (0, 1))
        self.assertEqual(row["ns_rate"], 0.0)

        landing = self.out / "index.html"
        site.render_leaderboard_page(summary, str(landing))
        page = landing.read_text(encoding="utf-8")
        self.assertIn(site.CURRENT_ONLY_NOTE, page)
        self.assertIn(f"CROSS-MODEL BENCHMARK LEADERBOARD · {site.BENCHMARK_VERSION}", page)
        self.assertNotIn("archive/index.html", page)

        site.ARCHIVE_ENABLED = True
        site.render_leaderboard_page(summary, str(landing))
        self.assertIn("archive/index.html", landing.read_text(encoding="utf-8"))
        site.ARCHIVE_ENABLED = False

    def test_empty_current_with_archived_records_renders_both_modes(self):
        archive_jobs = self.jobs.parent / "archive-only-jobs"
        archive_jobs.mkdir()
        for name in ("old-revision", "infra-error", "infra-timeout", "no-reward"):
            shutil.move(str(self.jobs / name), str(archive_jobs / name))

        tasks, dropped = site.collect_trials([str(archive_jobs)], str(self.repo))
        self.assertEqual(tasks, {})
        self.assertEqual(dropped, 0)
        self.assertEqual(len(site.collect_archive_records([str(archive_jobs)], str(self.repo))), 4)

        with mock.patch.object(site, "SITE_MODELS", self.single_model), \
                mock.patch.object(sys, "argv", [
                    "build_site.py", str(archive_jobs), "-o", str(self.out),
                    "--repo-root", str(self.repo), "--archive"]):
            site.main()
        self.assertTrue((self.out / "index.html").exists())
        self.assertTrue(self.archive.exists())
        self.assertIn("4 records", self.archive.read_text(encoding="utf-8"))
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn(site.CURRENT_ONLY_NOTE, index)
        # No current trial means no score to show: em dashes, never a
        # synthetic 0.0% pass rate on the leaderboard or catalog.
        self.assertIn("—", index)
        self.assertIn("No scored trials on the current revisions yet.", index)
        self.assertNotIn('<span class="rank-badge rank-1">1</span>', index)
        self.assertNotIn("0.0%", index)
        site.ARCHIVE_ENABLED = False

    def test_main_standard_then_archive(self):
        with mock.patch.object(site, "SITE_MODELS", self.single_model), \
                mock.patch.object(sys, "argv", [
                    "build_site.py", str(self.jobs), "-o", str(self.out),
                    "--repo-root", str(self.repo)]):
            site.main()
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn(site.CURRENT_ONLY_NOTE, index)
        self.assertFalse(self.archive.exists())
        self.assertNotIn("archive/index.html", index)

        with mock.patch.object(site, "SITE_MODELS", self.single_model), \
                mock.patch.object(sys, "argv", [
                    "build_site.py", str(self.jobs), "-o", str(self.out),
                    "--repo-root", str(self.repo), "--archive"]):
            site.main()
        self.assertTrue(self.archive.exists())
        archive_page = self.archive.read_text(encoding="utf-8")
        self.assertIn('role="alert"', archive_page)
        self.assertIn("Historical Archive (not current)", archive_page)
        self.assertIn("4 records", archive_page)
        self.assertIn("archive/index.html", (self.out / "index.html").read_text(encoding="utf-8"))
        site.ARCHIVE_ENABLED = False


if __name__ == "__main__":
    unittest.main()
