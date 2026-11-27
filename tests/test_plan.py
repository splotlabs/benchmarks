# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for the campaign plan, the lock, the matrix and the real corpus files. No network."""

import argparse
import copy
import hashlib
import json
import shutil
import socket
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, bench, git, isolate_git, restore_git
from splotbench import corpus, plan, schema
from splotbench.util import BenchError
from synthetic_campaign import HARNESS, make_lock, make_plan, write_corpus


def make_repo_for_harness(directory: Path) -> Path:
    directory.mkdir(parents=True)
    git(directory, "init", "-q")
    (directory / "bench").write_text("synthetic harness\n")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "synthetic harness")
    return directory


def setUpModule():
    isolate_git()


def tearDownModule():
    restore_git()


class TempTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)

    def lock_digest(self, name: str, **kwargs) -> str:
        return make_lock(self.temporary / name, **kwargs)["lock_digest"]


class LockDigestTest(TempTest):
    def test_digest_matches_canonical_json_and_is_stable(self):
        first = make_lock(self.temporary / "one")
        # Same inputs in another directory: same repositories content is not the same commit,
        # so compare a second build over the same directory.
        second = plan.build_lock(self.temporary / "one" / "plan.toml", harness=HARNESS)
        self.assertEqual(first, second)
        independent = "sha256:" + hashlib.sha256(
            json.dumps(first["lock"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(first["lock_digest"], independent)
        self.assertEqual(schema.validate(first), [])
        self.assertEqual(plan.campaign_id(first), "synthetic-campaign-" + independent[7:19])

    def test_every_execution_relevant_change_changes_the_digest(self):
        base = self.lock_digest("base")
        changes = {
            "threads": {"threads = [1, 2]\n": "threads = [1, 3]\n"},
            "repetitions": {"repetitions = 3": "repetitions = 4"},
            "warmups": {"warmups = 1": "warmups = 2"},
            "seed": {"order_seed = 7": "order_seed = 8"},
            "timeout": {"timeout_s = 60": "timeout_s = 61"},
            "noise": {"noise_max_range_permille = 50": "noise_max_range_permille = 51"},
            "suite": {'suite = "quick"': 'suite = "full"'},
        }
        for name, replace in changes.items():
            with self.subTest(name):
                # the same directory content, so only the plan text differs
                directory = self.temporary / name
                shutil.copytree(self.temporary / "base", directory)
                self.assertNotEqual(plan.build_lock(make_plan(directory, replace=replace), harness=HARNESS)["lock_digest"], base)

    def test_new_commit_and_new_corpus_hash_change_the_digest(self):
        base = make_lock(self.temporary / "base")
        directory = self.temporary / "commit"
        shutil.copytree(self.temporary / "base", directory)
        (directory / "repo-a" / "a").write_bytes(b"changed")
        git(directory / "repo-a", "commit", "-q", "-am", "second synthetic commit")
        moved = plan.build_lock(make_plan(directory), harness=HARNESS)
        self.assertNotEqual(moved["lock"]["implementations"][0]["commit"], base["lock"]["implementations"][0]["commit"])
        self.assertNotEqual(moved["lock_digest"], base["lock_digest"])

        directory = self.temporary / "corpus"
        shutil.copytree(self.temporary / "base", directory)
        write_corpus(directory, {"synthetic-8bit": (8, b"other bytes"), "synthetic-10bit": (10, b"more bytes")})
        other = plan.build_lock(make_plan(directory), harness=HARNESS)
        self.assertNotEqual(other["lock"]["corpus"]["digest"], base["lock"]["corpus"]["digest"])
        self.assertNotEqual(other["lock_digest"], base["lock_digest"])

    def test_harness_snapshot_is_part_of_the_digest(self):
        path = make_plan(self.temporary / "base")
        other = {**HARNESS, "snapshot_sha256": "sha256:" + "9" * 64}
        self.assertNotEqual(
            plan.build_lock(path, harness=HARNESS)["lock_digest"], plan.build_lock(path, harness=other)["lock_digest"]
        )

    def test_quick_and_full_have_different_campaign_ids_and_case_lists(self):
        quick = make_lock(self.temporary / "quick")
        full = plan.build_lock(make_plan(self.temporary / "quick", replace={'suite = "quick"': 'suite = "full"'}), harness=HARNESS)
        self.assertNotEqual(plan.campaign_id(quick), plan.campaign_id(full))
        self.assertEqual([c["id"] for c in quick["lock"]["suite"]["cases"]], ["synthetic-8bit"])
        self.assertEqual([c["id"] for c in full["lock"]["suite"]["cases"]], ["synthetic-8bit", "synthetic-10bit"])
        self.assertEqual(full["lock"]["suite"]["id"], "full")


class PortabilityTest(TempTest):
    def test_lock_has_no_path_or_host_name(self):
        lock = make_lock(self.temporary / "first-host")
        text = json.dumps(lock).replace(self.temporary.as_uri(), "")  # only the file:// URLs that the plan names itself
        for forbidden in (str(self.temporary), socket.gethostname(), "/Users/", "/home/"):
            self.assertNotIn(forbidden, text)
        real = plan.parse_plan(ROOT / "campaigns" / "template.toml")
        body = plan._assemble(real, ROOT / "campaigns", HARNESS, None)
        real_text = json.dumps(body)
        for forbidden in (str(ROOT), str(self.temporary), socket.gethostname(), "file://"):
            self.assertNotIn(forbidden, real_text)

    def test_copied_lock_validates_and_keeps_its_digest(self):
        lock = make_lock(self.temporary / "first-host")
        original = self.temporary / "first.json"
        original.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        elsewhere = self.temporary / "second-host" / "deeper"
        elsewhere.mkdir(parents=True)
        copied = Path(shutil.copy(original, elsewhere / "lock-copy.json"))
        self.assertEqual(plan.load_lock(copied)["lock_digest"], lock["lock_digest"])
        self.assertEqual(bench(ROOT, "validate", str(copied)).returncode, 0)

    def test_edited_lock_fails_validation_with_digest_mismatch(self):
        lock = make_lock(self.temporary / "first-host")
        lock["lock"]["measurement"]["repetitions"] = 99
        path = self.temporary / "edited.json"
        path.write_text(json.dumps(lock))
        done = bench(ROOT, "validate", str(path))
        self.assertEqual(done.returncode, 1)
        self.assertIn("lock_digest", done.stderr)
        with self.assertRaises(BenchError) as raised:
            plan.load_lock(path)
        self.assertIn("lock_digest", str(raised.exception))


class SelectorTest(TempTest):
    def setUp(self):
        super().setUp()
        directory = self.temporary / "campaign"
        make_plan(directory)  # makes the repositories
        self.repo = directory / "repo-a"
        self.first = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a").write_bytes(b"second")
        git(self.repo, "commit", "-q", "-am", "second synthetic commit")
        self.second = git(self.repo, "rev-parse", "HEAD")

    def locked(self, select: str) -> dict:
        path = make_plan(self.temporary / "campaign", select_a=select)
        return plan.build_lock(path, harness=HARNESS)["lock"]["implementations"][0]

    def test_branch_selects_its_head_as_development(self):
        entry = self.locked("main")
        self.assertEqual((entry["commit"], entry["identity"], entry["release"]), (self.second, "development", None))
        self.assertEqual(entry["select"], "main")

    def test_commit_selector(self):
        entry = self.locked(self.first)
        self.assertEqual((entry["commit"], entry["identity"]), (self.first, "development"))

    def test_annotated_release_tag_gives_release_identity(self):
        git(self.repo, "tag", "-a", "v1.2.3", "-m", "synthetic release", self.first)
        entry = self.locked("v1.2.3")
        self.assertEqual(entry["commit"], self.first)  # the peeled commit, not the tag object
        self.assertEqual(entry["identity"], "release")
        self.assertEqual(
            entry["release"],
            {"tag": "v1.2.3", "version": "1.2.3", "url": "https://example.invalid/synthetic/fixture-a/releases/v1.2.3"},
        )

    def test_branch_at_a_tagged_commit_is_a_release_and_a_later_commit_is_not(self):
        git(self.repo, "tag", "v1.0.0", self.second)
        self.assertEqual(self.locked("main")["identity"], "release")
        (self.repo / "a").write_bytes(b"third")
        git(self.repo, "commit", "-q", "-am", "third synthetic commit")
        self.assertEqual(self.locked("main")["identity"], "development")

    def test_tag_outside_the_pattern_is_not_a_release(self):
        git(self.repo, "tag", "research-v1.0.0", self.second)
        git(self.repo, "tag", "v1.0.0-rc1", self.second)
        entry = self.locked("research-v1.0.0")
        self.assertEqual((entry["commit"], entry["identity"], entry["release"]), (self.second, "development", None))

    def test_unknown_selector_is_an_error(self):
        path = make_plan(self.temporary / "campaign", select_a="no-such-ref")
        with self.assertRaises(BenchError) as raised:
            plan.build_lock(path, harness=HARNESS)
        self.assertIn("no-such-ref", str(raised.exception))


class PlanValidationTest(TempTest):
    def error(self, replace: dict[str, str]) -> str:
        path = make_plan(self.temporary / "campaign", replace=replace)
        with self.assertRaises(BenchError) as raised:
            plan.parse_plan(path)
            plan.build_lock(path, harness=HARNESS)
        return str(raised.exception)

    def test_errors_name_the_key(self):
        self.assertIn("'repetitionz'", self.error({"repetitions = 3": "repetitionz = 3"}))
        self.assertIn("repetitions", self.error({"repetitions = 3": "repetitions = 0"}))
        self.assertIn("warmups", self.error({"warmups = 1": 'warmups = "one"'}))
        self.assertIn("'timeout_s'", self.error({"timeout_s = 60\n": ""}))
        self.assertIn("threads", self.error({"threads = [1, 2]\n": "threads = [1, 1]\n"}))
        self.assertIn("roles.arm64", self.error({'arch = "aarch64"': 'arch = "aarch64"\nspeed = 1'}))
        self.assertIn("arch", self.error({'arch = "aarch64"': 'arch = "riscv"'}))
        self.assertIn("suite", self.error({'suite = "quick"': 'suite = "huge"'}))

    def test_adapter_must_match_origin(self):
        self.assertIn("fixture", self.error({'origin = "synthetic"': 'origin = "standard"'}))
        self.assertIn("only that adapter", self.error({'adapter = "fixture"': 'adapter = "splot"'}))

    def test_bad_toml_and_missing_file(self):
        path = self.temporary / "bad.toml"
        path.write_text("name = \n")
        with self.assertRaises(BenchError):
            plan.parse_plan(path)
        with self.assertRaises(BenchError):
            plan.parse_plan(self.temporary / "missing.toml")

    def test_lock_command_refuses_to_overwrite(self):
        path = make_plan(self.temporary / "campaign")
        output = self.temporary / "out" / "lock.json"
        self.assertEqual(bench(ROOT, "lock", str(path), "-o", str(output)).returncode, 0)
        before = output.read_bytes()
        done = bench(ROOT, "lock", str(path), "-o", str(output))
        self.assertEqual(done.returncode, 1)
        self.assertIn("never overwritten", done.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_lock_command_prints_the_summary(self):
        path = make_plan(self.temporary / "campaign")
        output = self.temporary / "lock.json"
        done = bench(ROOT, "lock", str(path), "-o", str(output))
        self.assertEqual(done.returncode, 0, done.stderr)
        document = plan.load_lock(output)
        self.assertIn(f"campaign {plan.campaign_id(document)}", done.stdout)
        self.assertIn(f"digest {document['lock_digest']}", done.stdout)
        self.assertIn(f"fixture-a {document['lock']['implementations'][0]['commit']} development", done.stdout)
        self.assertIn("role arm64: 4 cells, 16 attempts", done.stdout)
        self.assertIn("role x86-64: 6 cells, 24 attempts", done.stdout)


class MatrixTest(TempTest):
    def setUp(self):
        super().setUp()
        # full suite: 2 cases, 2 implementations; arm64 threads [1, 2], x86-64 threads [1, 2, 4]
        self.lock = plan.build_lock(make_plan(self.temporary / "c", replace={'suite = "quick"': 'suite = "full"'}), harness=HARNESS)

    def test_counts_match_hand_computed_numbers(self):
        self.assertEqual(len(plan.enumerate_cells(self.lock, "arm64")), 2 * 2 * 2)
        self.assertEqual(len(plan.enumerate_cells(self.lock, "x86-64")), 2 * 3 * 2)
        cells = plan.enumerate_cells(self.lock)
        self.assertEqual(len(cells), 8 + 12)
        self.assertEqual(len(set(cells)), 20)
        self.assertEqual(plan.attempt_counts(self.lock), {"warmups": 1, "repetitions": 3})
        self.assertEqual(cells[0], ("arm64", "synthetic-8bit", 1, "fixture-a"))
        self.assertEqual(cells[1], ("arm64", "synthetic-8bit", 1, "fixture-b"))
        self.assertEqual(cells[-1], ("x86-64", "synthetic-10bit", 4, "fixture-b"))

    def test_thread_counts_come_in_ascending_order(self):
        lock = copy.deepcopy(self.lock)
        lock["lock"]["roles"]["x86-64"]["threads"] = [4, 1, 2]
        threads = [c.threads for c in plan.enumerate_cells(lock, "x86-64") if c.case == "synthetic-8bit" and c.implementation == "fixture-a"]
        self.assertEqual(threads, [1, 2, 4])

    def test_unknown_role(self):
        with self.assertRaises(BenchError):
            plan.enumerate_cells(self.lock, "riscv")

    def test_matrix_command_reads_only_the_lock(self):
        path = self.temporary / "lock.json"
        path.write_text(json.dumps(self.lock))
        shutil.rmtree(self.temporary / "c")  # no repository, plan or corpus is needed
        done = bench(ROOT, "matrix", "--lock", str(path), "--role", "x86-64")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[0], "x86-64\tsynthetic-8bit\t1\tfixture-a")
        self.assertIn("12 cells; each gets 1 warm-up and 3 timed attempts", done.stderr)
        self.assertEqual(len(bench(ROOT, "matrix", "--lock", str(path)).stdout.splitlines()), 20)
        self.assertEqual(bench(ROOT, "matrix", "--lock", str(path), "--role", "nope").returncode, 2)


class SchemeAndLimitsTest(TempTest):
    def test_standard_plan_refuses_file_urls(self):
        path = make_plan(self.temporary / "c", replace={'origin = "synthetic"': 'origin = "standard"', 'adapter = "fixture"': 'adapter = "splot"'})
        with self.assertRaises(BenchError) as raised:
            plan.build_lock(path, harness=HARNESS)  # the repositories are file: URLs
        self.assertIn("repository", str(raised.exception))
        self.assertIn("https", str(raised.exception))

    def test_standard_plan_needs_a_harness_commit_of_the_upstream(self):
        directory = self.temporary / "c"
        path = make_plan(directory, replace={'origin = "synthetic"': 'origin = "standard"', 'adapter = "fixture"': 'adapter = "splot"'})
        for name in ("plan.toml", "corpus.toml"):  # https URLs that are never contacted: the check stops first
            (directory / name).write_text((directory / name).read_text().replace("file://", "https://example.invalid"))
        upstream = make_repo_for_harness(self.temporary / "synthetic-harness-upstream")
        with mock.patch.object(plan.provenance, "HARNESS_REPOSITORY", upstream.as_uri()), \
                mock.patch.object(plan.provenance, "resolve", side_effect=AssertionError("resolved a decoder")):
            with self.assertRaisesRegex(BenchError, "a second host could not check out this harness"):
                plan.build_lock(path, harness=HARNESS)  # HARNESS names a commit that the upstream lacks
            dirty = HARNESS | {"dirty": True}
            with self.assertRaisesRegex(BenchError, "not a clean Git commit"):
                plan.build_lock(path, harness=dirty)

    def test_standard_manifest_refuses_file_urls_and_mirrors(self):
        make_plan(self.temporary / "c")
        with self.assertRaises(BenchError) as raised:
            corpus.load_manifest(self.temporary / "c" / "corpus.toml")
        self.assertIn("https", str(raised.exception))
        self.assertEqual(len(corpus.load_manifest(self.temporary / "c" / "corpus.toml", synthetic=True)["case"]), 2)
        path = self.temporary / "https.toml"
        text = (self.temporary / "c" / "corpus.toml").read_text().replace("file://", "https://example.invalid")
        path.write_text(text.replace('frames = 2', 'frames = 2\nmirrors = ["file:///x"]', 1))
        with self.assertRaises(BenchError) as raised:
            corpus.load_manifest(path)
        self.assertIn("file:///x", str(raised.exception))

    def test_timeout_has_an_upper_bound_in_plan_and_schema(self):
        path = make_plan(self.temporary / "c", replace={"timeout_s = 60": "timeout_s = 86400"})
        lock = plan.build_lock(path, harness=HARNESS)
        self.assertEqual(lock["lock"]["measurement"]["timeout_s"], 86400)
        with self.assertRaises(BenchError) as raised:
            plan.parse_plan(make_plan(self.temporary / "c", replace={"timeout_s = 60": "timeout_s = 86401"}))
        self.assertIn("timeout_s", str(raised.exception))
        lock["lock"]["measurement"]["timeout_s"] = 86401
        self.assertTrue(any("timeout_s" in problem for problem in schema.validate(plan.lock_document(lock["lock"]))))

    def test_suite_with_a_duplicate_id_is_refused(self):
        make_plan(self.temporary / "c")
        manifest = corpus.load_manifest(self.temporary / "c" / "corpus.toml", synthetic=True)
        with self.assertRaises(BenchError) as raised:
            corpus.suite_cases(manifest, {"quick": ["synthetic-8bit", "synthetic-8bit"]}, "quick")
        self.assertIn("synthetic-8bit", str(raised.exception))

    def test_suites_value_must_be_a_list_of_strings(self):
        for text in ('quick = "synthetic-8bit"\n', "quick = [1, 2]\n"):
            path = self.temporary / "suites.toml"
            path.write_text(text)
            with self.assertRaises(BenchError) as raised:
                corpus.load_suites(path)
            self.assertIn("quick", str(raised.exception))

    def test_output_file_that_appears_late_is_not_replaced(self):
        path = make_plan(self.temporary / "c")
        output = self.temporary / "lock.json"
        real = plan.build_lock

        def late(*args, **kwargs):
            document = real(*args, **kwargs)
            output.write_text("someone else's file")  # created after the first exists-check
            return document

        arguments = argparse.Namespace(plan=path, output=output)
        with mock.patch.object(plan, "build_lock", late), self.assertRaises(BenchError):
            plan._lock(arguments)
        self.assertEqual(output.read_text(), "someone else's file")
        self.assertEqual([p.name for p in self.temporary.iterdir() if p.name.endswith(".tmp")], [])


class CommittedPlansTest(unittest.TestCase):
    def test_every_committed_plan_is_valid_and_campaign_plans_pin_commits(self):
        plans = sorted((ROOT / "campaigns").glob("*.toml"))
        self.assertIn(ROOT / "campaigns" / "template.toml", plans)
        names = []
        for path in plans:
            with self.subTest(plan=path.name):
                parsed = plan.parse_plan(path)
                names.append(parsed["name"])
                if path.name != "template.toml":
                    # A campaign plan pins full commits: machines that lock it at different times measure the same sources.
                    for implementation in parsed["implementation"]:
                        self.assertRegex(implementation["select"], r"^[0-9a-f]{40}$")
        self.assertEqual(len(names), len(set(names)))

    def test_plans_of_one_round_differ_only_in_name_and_role(self):
        arm = plan.parse_plan(ROOT / "campaigns" / "2026-11-full-arm64.toml")
        x86 = plan.parse_plan(ROOT / "campaigns" / "2026-11-full-x86-64.toml")
        self.assertEqual((list(arm["roles"]), list(x86["roles"])), (["arm64"], ["x86-64"]))
        for key in arm.keys() | x86.keys():
            if key not in ("name", "roles"):
                self.assertEqual(arm[key], x86[key], key)
        self.assertEqual(arm["roles"]["arm64"]["threads"], x86["roles"]["x86-64"]["threads"])


class RealCorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = corpus.load_manifest()
        cls.suites = corpus.load_suites()
        cls.cases = {case["id"]: case for case in cls.manifest["case"]}

    def test_manifest_facts(self):
        self.assertEqual(len(self.cases), 88)
        for case in self.cases.values():
            self.assertRegex(case["id"], r"^[a-z0-9-]+$")
            self.assertNotIn("film_grain", case)
        imported = self.manifest["imported_from"]
        self.assertEqual(imported["path"], "tests/conformance/ewouth-av2.toml")
        self.assertRegex(imported["commit"], r"^[0-9a-f]{40}$")
        # Frame size times frames must equal the reference byte count, for every stream.
        for case in self.cases.values():
            frame = case["width"] * case["height"] * 3 // 2 * (1 if case["bit_depth"] == 8 else 2)
            self.assertEqual(frame * case["frames"], case["raw_bytes"], case["id"])

    def test_suites_follow_their_rule_and_ids_exist(self):
        luma = lambda c: c["width"] * c["height"]
        full = sorted(c["id"] for c in self.cases.values() if luma(c) >= 1280 * 720)
        self.assertEqual(sorted(self.suites["full"]), full)
        self.assertEqual(len(self.suites["full"]), len(set(self.suites["full"])))
        quick = []
        for depth in (8, 10):
            pool = [c for c in self.cases.values() if (c["width"], c["height"], c["bit_depth"]) == (1920, 1080, depth)]
            pool = sorted((c["frames"], c["id"]) for c in pool if c["frames"] >= 60)
            quick += [case_id for _, case_id in pool[:3]]
        self.assertEqual(self.suites["quick"], quick)
        self.assertEqual(len(quick), 6)
        for ids in self.suites.values():
            self.assertTrue(set(ids) <= self.cases.keys())

    def test_template_makes_a_valid_lock_for_both_suites(self):
        template = plan.parse_plan(ROOT / "campaigns" / "template.toml")
        self.assertEqual(sorted(template["roles"]), ["arm64", "x86-64"])
        self.assertEqual([e["id"] for e in template["implementation"]], ["splot", "dav2d", "avmdec"])
        for suite, count in (("quick", 6), ("full", 79)):
            body = plan._assemble({**template, "suite": suite}, ROOT / "campaigns", HARNESS, None)
            document = plan._checked(body)
            self.assertEqual(len(document["lock"]["suite"]["cases"]), count)
            self.assertTrue(all(c["film_grain"] is None for c in document["lock"]["suite"]["cases"]))

    def test_release_pattern_of_avm_skips_research_tags(self):
        import re

        pattern = re.compile(plan.parse_plan(ROOT / "campaigns" / "template.toml")["implementation"][2]["release_tag_pattern"])
        self.assertTrue(pattern.fullmatch("v1.0.0"))
        for tag in ("research-v9.0.0", "research-v7.0.0-rc1", "v1.0.0-rc1"):
            self.assertIsNone(pattern.fullmatch(tag))

    def test_corpus_digest_is_pinned(self):
        # An accidental edit of corpus/ewouth-av2.toml fails here. An intended corpus change
        # (a new import) must update this digest, and every lock made before it keeps the old one.
        self.assertEqual(
            corpus.manifest_digest(self.manifest),
            "sha256:967cd3500a1c3caeadb1313851a4e898f8bff96f500d3093625c1e6dd66226c0",
        )

    def test_corpus_digest_changes_with_a_stream_fact(self):
        changed = copy.deepcopy(self.manifest)
        changed["case"][0]["tiles"] = "9x9"
        self.assertNotEqual(corpus.manifest_digest(changed), corpus.manifest_digest(self.manifest))


if __name__ == "__main__":
    unittest.main()
