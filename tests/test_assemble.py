# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Tests for `bench assemble`. Every bundle is written here from a compact synthetic description.

Throughput is fps = frames / wall seconds. Every case has 2 frames, so a wall time of
1e9 ns gives 2 fps, 5e8 gives 4 fps, 2e9 gives 1 fps and 4e9 gives 0.5 fps.
"""

import copy
import csv
import hashlib
import json
from unittest import mock
import shutil
import tempfile
import unittest
from pathlib import Path

import synthetic_campaign
from helpers import ROOT, bench, isolate_git, restore_git
from splotbench import assemble, bundle, schema, validate
from splotbench.build import native_arch
from splotbench.util import BenchError, canonical_json
from synthetic_runs import FIXTURES, digest_of, full_cells, make_lock, write_run

S = 1_000_000_000
C8, C8B, C10 = "fixture-case-8bit", "fixture-case-8bit-b", "fixture-case-10bit"
A, B = "fixture-a", "fixture-b"
ARM_RUN = "arm64-20260101T000000Z-0f1e2d3c"
X86_RUN = "x86-64-20260101T000000Z-1a2b3c4d"


def add_case(lock):
    extra = copy.deepcopy(lock["suite"]["cases"][0])
    extra["id"], extra["name"] = C8B, "fixture-case-8bit-b.obu"
    lock["suite"]["cases"].insert(1, extra)


LOCK = make_lock(add_case)


def lock_with(change):
    """A lock with the extra case and `change(lock)` applied."""
    return make_lock(lambda lock: (add_case(lock), change(lock)))


# Arm, per (implementation, case, threads). Expected values are worked out in the tests.
ARM = {
    (A, C8, 1): [S, S // 2, 2 * S],  # fps 2, 4, 1
    (B, C8, 1): [2 * S] * 3,  # fps 1
    (A, C8, 2): [S // 2] * 3,  # fps 4
    (B, C8, 2): [S] * 3,  # fps 2
    (A, C8B, 1): [S // 2] * 3,  # fps 4
    (B, C8B, 1): [4 * S] * 3,  # fps 0.5
    (A, C8B, 2): [S] * 3,  # fps 2
    (B, C8B, 2): "unsupported",
    (A, C10, 1): [S] * 3,  # fps 2
    (B, C10, 1): "incorrect",
    (A, C10, 2): [S, "timeout", S],
    (B, C10, 2): [S] * 3,  # fps 2
}
# x86: every cell is 2 fps except fixture-a on the first 8-bit case with 1 thread (4 fps).
X86 = {(A, C8, 1): [S // 2] * 3}


# The assembling harness is injected, so the tests do not depend on the state of the clone.
HARNESS = {
    "repository": "https://github.com/splotlabs/benchmarks", "commit": "a" * 40,
    "snapshot_sha256": "sha256:" + "a1" * 32, "class": "verified",
}  # fmt: skip


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.lock_path = self.tmp / "LOCK.json"
        self.lock_path.write_text(json.dumps(LOCK))
        self.out = self.tmp / "out"
        self._count = 0

    def run_bundle(self, role="arm64", cells=None, lock=LOCK, run_id=None, machine="synthetic-arm", **kwargs):
        self._count += 1
        work = self.tmp / f"work{self._count}"
        work.mkdir()
        cells = full_cells(lock, role, cells if cells is not None else (ARM if role == "arm64" else X86))
        return write_run(work, lock, role, cells, machine_id=machine, run_id=run_id or ARM_RUN, **kwargs)[0]

    def arm(self, **kwargs):
        return self.run_bundle("arm64", ARM, **kwargs)

    def x86(self, **kwargs):
        kwargs.setdefault("run_id", X86_RUN)
        kwargs.setdefault("machine", "synthetic-x86")
        kwargs.setdefault("binary_seed", "x86")
        return self.run_bundle("x86-64", X86, **kwargs)

    def assemble(self, *bundles, lock_path=None, **kwargs):
        kwargs.setdefault("harness", HARNESS)
        return assemble.assemble(lock_path or self.lock_path, list(bundles), self.out, **kwargs)

    def summary(self, target):
        return json.loads((target / "summary.json").read_text())

    def cell(self, summary, machine, case, threads, implementation):
        (found,) = [
            c for c in summary["cells"]
            if (c["machine_id"], c["case_id"], c["threads"], c["implementation"]) == (machine, case, threads, implementation)
        ]  # fmt: skip
        return found

    def aggregate(self, summary, machine, threads, depth, implementation, baseline):
        (found,) = [
            a for a in summary["aggregates"]
            if (a["machine_id"], a["threads"], a["bit_depth"], a["implementation"], a["baseline"])
            == (machine, threads, depth, implementation, baseline)
        ]  # fmt: skip
        return found

    def assertRejected(self, *bundles, text, **kwargs):
        with self.assertRaises(BenchError) as raised:
            self.assemble(*bundles, **kwargs)
        self.assertIn(text, str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 1)
        self.assertFalse((self.out / "datasets").exists())
        return str(raised.exception)


class DatasetTest(Base):
    def setUp(self):
        super().setUp()
        self.target = self.assemble(self.arm(), self.x86())
        self.doc = self.summary(self.target)

    def test_two_bundles_make_one_complete_valid_dataset(self):
        manifest = json.loads((self.target / "manifest.json").read_text())
        self.assertEqual(self.target.name, manifest["dataset_id"])
        self.assertEqual((manifest["origin"], manifest["complete"]), ("synthetic", True))
        self.assertEqual(
            sorted(p.name for p in self.target.iterdir()),
            ["SHA256SUMS", "bundles", "lock.json", "manifest.json", "summary.csv", "summary.json"],
        )
        self.assertEqual(
            sorted(p.name for p in (self.target / "bundles").iterdir()), [f"{ARM_RUN}.tar.gz", f"{X86_RUN}.tar.gz"]
        )
        self.assertEqual(validate.problems_for(self.target), [])
        # 2 roles x 3 cases x 2 thread budgets x 2 implementations; arm has 3 failed cells.
        coverage = manifest["coverage"]
        self.assertEqual((coverage["cells_expected"], coverage["cells_ok"]), (24, 21))
        self.assertEqual(coverage["status_counts"], {"incorrect": 1, "ok": 21, "timeout": 1, "unsupported": 1})
        self.assertEqual([m["role"] for m in self.doc["machines"]], ["arm64", "x86-64"])
        bundle_entry = self.doc["machines"][0]["bundle"]
        self.assertEqual(bundle_entry["sha256"], digest_of(self.target / bundle_entry["path"]))

    def test_cell_statistics_from_measured_attempts_only(self):
        # Walls 1e9, 5e8, 2e9 ns give fps 2, 4, 1: median 2, min 1, max 4, |dev| 0, 2, 1 so MAD 1.
        # Range (2e9 - 5e8) / median 1e9 = 1.5 is above the 50 permille rule: noisy. The 999 s warm-up is ignored.
        cell = self.cell(self.doc, "synthetic-arm", C8, 1, A)
        self.assertEqual(
            (cell["status"], cell["n_measured"], cell["n_valid"], cell["frames"]), ("ok", 3, 3, 2)
        )
        self.assertEqual((cell["wall_ns_median"], cell["fps_median"], cell["fps_min"], cell["fps_max"]), (S, 2, 1, 4))
        self.assertEqual((cell["fps_mad"], cell["noisy"]), (1, True))
        self.assertEqual(cell["cpu_ns_median"], S // 2)  # cpu = wall / 2: 5e8, 2.5e8, 1e9
        self.assertEqual(cell["peak_rss_bytes_median"], 1 << 20)
        self.assertIsNone(cell["first_frame_latency_ns"])
        steady = self.cell(self.doc, "synthetic-arm", C8, 1, B)
        self.assertEqual((steady["fps_median"], steady["fps_mad"], steady["noisy"]), (1, 0, False))

    def test_scaling_against_one_thread_of_the_same_machine(self):
        self.assertEqual(self.cell(self.doc, "synthetic-arm", C8, 1, A)["scaling_vs_1t"], 1)
        self.assertEqual(self.cell(self.doc, "synthetic-arm", C8, 2, A)["scaling_vs_1t"], 2)  # 4 fps / 2 fps
        self.assertEqual(self.cell(self.doc, "synthetic-arm", C8, 2, B)["scaling_vs_1t"], 2)  # 2 fps / 1 fps
        self.assertEqual(self.cell(self.doc, "synthetic-x86", C8, 2, A)["scaling_vs_1t"], 0.5)  # 2 fps / 4 fps
        # fixture-b on the 10-bit case is ok with 2 threads, but its 1-thread cell is incorrect.
        ok = self.cell(self.doc, "synthetic-arm", C10, 2, B)
        self.assertEqual((ok["status"], ok["scaling_vs_1t"]), ("ok", None))

    def test_failures_stay_visible_without_numbers(self):
        incorrect = self.cell(self.doc, "synthetic-arm", C10, 1, B)
        self.assertEqual((incorrect["status"], incorrect["n_measured"], incorrect["fps_median"]), ("incorrect", 0, None))
        unsupported = self.cell(self.doc, "synthetic-arm", C8B, 2, B)
        self.assertEqual((unsupported["status"], unsupported["wall_ns_median"]), ("unsupported", None))
        timeout = self.cell(self.doc, "synthetic-arm", C10, 2, A)
        self.assertEqual((timeout["status"], timeout["n_measured"], timeout["n_valid"]), ("timeout", 3, 2))
        self.assertIsNone(timeout["fps_median"])
        self.assertEqual(len(self.doc["cells"]), 24)

    def test_speedups_only_between_ok_cells(self):
        arm = {
            (s["case_id"], s["threads"], s["implementation"], s["baseline"]): s["ratio"]
            for s in self.doc["speedups"]
            if s["machine_id"] == "synthetic-arm"
        }
        self.assertEqual(
            arm,
            {
                (C8, 1, A, B): 2, (C8, 1, B, A): 0.5,  # 2 fps / 1 fps
                (C8, 2, A, B): 2, (C8, 2, B, A): 0.5,  # 4 fps / 2 fps
                (C8B, 1, A, B): 8, (C8B, 1, B, A): 0.125,  # 4 fps / 0.5 fps
            },
        )  # fmt: skip
        # Nothing for the unsupported, incorrect and timeout cells, in either direction.
        self.assertEqual(len([s for s in self.doc["speedups"] if s["machine_id"] == "synthetic-x86"]), 12)

    def test_aggregates_use_matched_cases_and_show_the_denominator(self):
        # 8-bit, 1 thread: both cases ok with ratios 2 and 8: geometric mean sqrt(16) = 4; reverse 0.5 * 0.125 -> 0.25.
        forward = self.aggregate(self.doc, "synthetic-arm", 1, 8, A, B)
        self.assertAlmostEqual(forward["geomean_ratio"], 4)
        self.assertEqual((forward["n_matched"], forward["n_expected"], forward["excluded"]), (2, 2, []))
        self.assertAlmostEqual(self.aggregate(self.doc, "synthetic-arm", 1, 8, B, A)["geomean_ratio"], 0.25)
        # 8-bit, 2 threads: the second case is unsupported for fixture-b, so one of two cases matches.
        partial = self.aggregate(self.doc, "synthetic-arm", 2, 8, A, B)
        self.assertAlmostEqual(partial["geomean_ratio"], 2)
        self.assertEqual((partial["n_matched"], partial["n_expected"]), (1, 2))
        self.assertEqual(partial["excluded"], [{"case_id": C8B, "reason": "fixture-b: unsupported"}])
        # 10-bit is separate: with 1 thread the only case is incorrect for fixture-b.
        none = self.aggregate(self.doc, "synthetic-arm", 1, 10, A, B)
        self.assertEqual((none["geomean_ratio"], none["n_matched"], none["n_expected"]), (None, 0, 1))
        self.assertEqual(none["excluded"], [{"case_id": C10, "reason": "fixture-b: incorrect"}])
        timeout = self.aggregate(self.doc, "synthetic-arm", 2, 10, B, A)
        self.assertEqual(timeout["excluded"], [{"case_id": C10, "reason": "fixture-a: timeout"}])
        # The other machine has its own aggregate: ratios 2 and 1, geometric mean sqrt(2).
        self.assertAlmostEqual(self.aggregate(self.doc, "synthetic-x86", 1, 8, A, B)["geomean_ratio"], 2**0.5)

    def test_every_pair_machine_threads_and_depth_has_an_aggregate(self):
        # 2 machines x 2 thread budgets x 2 bit depths x 2 ordered pairs
        self.assertEqual(len(self.doc["aggregates"]), 16)

    def test_different_binaries_per_role_are_accepted_and_kept(self):
        binaries = {(b["machine_id"], b["implementation"]): b["binary_sha256"] for b in self.doc["builds"]}
        self.assertEqual(len(binaries), 4)
        self.assertEqual(len(set(binaries.values())), 4)
        self.assertNotEqual(self.doc["builds"][0]["build_id"], self.doc["builds"][2]["build_id"])

    def test_csv_has_one_line_per_cell_and_empty_fields_for_null(self):
        rows = list(csv.reader((self.target / "summary.csv").read_text().splitlines()))
        self.assertEqual(rows[0], list(assemble.CELL_FIELDS))
        self.assertEqual(len(rows), 25)
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual((first["machine_id"], first["case_id"], first["fps_median"], first["noisy"]), (
            "synthetic-arm", C8, "2.0", "true"))  # fmt: skip
        failed = dict(zip(rows[0], next(r for r in rows if r[0] == "synthetic-arm" and r[1] == C10 and r[3] == B and r[2] == "1")))
        self.assertEqual((failed["status"], failed["fps_median"], failed["frames"]), ("incorrect", "", ""))

    def test_summary_and_index_entry_follow_the_contract(self):
        self.assertEqual(schema.validate(self.doc), [])
        self.assertEqual(
            list(self.doc["cells"][0]), list(assemble.CELL_FIELDS), "cell fields use the contract names and order"
        )
        self.assertEqual(self.doc["aggregation"]["harness"]["class"], "synthetic")
        self.assertEqual(self.doc["downloads"]["checksums"], "SHA256SUMS")
        entry = assemble.index_entry(self.doc)
        self.assertEqual(entry["path"], f"datasets/{self.doc['dataset_id']}/")
        self.assertEqual(entry["measured_utc"], {"first": "2026-01-01T00:02:00Z", "last": "2026-01-01T00:48:00Z"})
        self.assertEqual([s["role"] for s in entry["series"]], ["arm64", "x86-64"])
        index = {"schema": "splotbench.index/1", "latest_complete": None, "datasets": [
            {**entry, "published_utc": "2026-02-01T00:00:00Z"}]}  # fmt: skip
        self.assertEqual(schema.validate(index), [])


class CompatibilityTest(Base):
    def test_other_lock_is_rejected_and_names_the_differing_part(self):
        other = lock_with(lambda lock: lock["corpus"].update(digest="sha256:" + "ab" * 32))
        bundle = self.run_bundle(lock=other)
        message = self.assertRejected(bundle, text="made for another lock")
        self.assertIn("Differing parts: corpus", message)

    def test_wrong_architecture(self):
        bundle = self.run_bundle(arch="x86_64")
        self.assertRejected(bundle, text="host architecture x86_64 does not match role arm64 (aarch64)")

    def test_role_not_in_the_lock(self):
        def edit(documents):
            documents["run"]["role"] = "riscv"

        self.assertRejected(self.run_bundle(edit=edit), text="role 'riscv' is not in the lock (roles: arm64, x86-64)")

    def test_run_of_another_campaign(self):
        def edit(documents):
            documents["run"]["campaign_id"] = "synthetic-other-000000000000"

        self.assertRejected(self.run_bundle(edit=edit), text="campaign_id synthetic-other-000000000000 differs")

    def test_build_of_another_commit(self):
        other = lock_with(lambda lock: lock["implementations"][0].update(commit="f" * 40))
        lock_path = self.tmp / "other-lock.json"
        lock_path.write_text(json.dumps(other))
        bundle = self.run_bundle(lock=other)
        self.assertRejected(bundle, text="differs from the lock commit " + "f" * 40, lock_path=lock_path)

    def test_attempt_with_unknown_build_or_case(self):
        def edit(documents):
            documents["attempts"][0]["build_id"] = "bld-" + "0" * 16
            documents["correctness"][0]["case_id"] = "no-such-case"

        message = self.assertRejected(self.run_bundle(edit=edit), text="unknown build_id bld-0000000000000000")
        self.assertIn("unknown case 'no-such-case'", message)

    def test_duplicate_sequence_and_attempt_id_mismatch(self):
        def edit(documents):
            documents["attempts"][1]["sequence"] = documents["attempts"][0]["sequence"]

        message = self.assertRejected(self.run_bundle(edit=edit), text="appears twice")
        self.assertIn("does not match sequence", message)

    def test_attempt_without_correctness_row(self):
        def edit(documents):
            documents["correctness"].pop(0)

        self.assertRejected(self.run_bundle(edit=edit), text="without a correctness row with outcome ok")

    def test_correctness_row_of_another_reference(self):
        def edit(documents):
            documents["correctness"][0]["workload"]["reference_sha256"] = "sha256:" + "9" * 64

        self.assertRejected(self.run_bundle(edit=edit), text=f"case {C8} was checked against another reference")

    def test_corrupt_bundle_is_rejected_by_the_bundle_checks(self):
        bundle = self.arm()
        data = bytearray(bundle.read_bytes())
        data[len(data) // 2] ^= 0xFF
        bundle.write_bytes(bytes(data))
        self.assertRejected(bundle, text="invalid bundle")


class RunSelectionTest(Base):
    def test_same_run_twice_is_idempotent(self):
        bundle = self.arm()
        copy_path = self.tmp / "copy.tar.gz"
        shutil.copy(bundle, copy_path)
        once = self.assemble(bundle, self.x86())
        twice = self.assemble(bundle, copy_path, bundle, self.x86())
        self.assertEqual(once, twice)
        self.assertEqual(self.cell(self.summary(twice), "synthetic-arm", C8, 1, A)["n_measured"], 3)
        self.assertEqual(len(self.summary(twice)["machines"]), 2)

    def test_same_run_id_with_other_content_is_a_conflict(self):
        slower = {**ARM, (A, C8, 1): [9 * S] * 3}
        other = self.run_bundle("arm64", slower)
        self.assertRejected(self.arm(), other, text=f"both have run id {ARM_RUN} but different content")

    def test_two_runs_for_one_role_need_a_selection(self):
        first, second = self.arm(), self.run_bundle("arm64", ARM, run_id="arm64-20260102T000000Z-aaaaaaaa")
        self.assertRejected(first, second, text="Choose one with --select arm64=RUN_ID")
        self.assertRejected(first, second, text="no imported run with that id", select={"arm64": "arm64-20260103T000000Z-bbbbbbbb"})
        self.assertRejected(first, second, text="no imported bundle has role x86-64", select={"x86-64": X86_RUN})

    def test_unselected_run_is_excluded_evidence_and_never_chosen_by_speed(self):
        slow = self.run_bundle("arm64", {**ARM, (A, C8, 1): [9 * S] * 3}, run_id="arm64-20260102T000000Z-aaaaaaaa")
        fast = self.arm()
        target = self.assemble(fast, slow, select={"arm64": "arm64-20260102T000000Z-aaaaaaaa"})
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual([r["run_id"] for r in manifest["runs"]], ["arm64-20260102T000000Z-aaaaaaaa"])
        self.assertEqual([r["run_id"] for r in manifest["excluded_runs"]], [ARM_RUN])
        self.assertTrue((target / "bundles" / f"{ARM_RUN}.tar.gz").exists())
        self.assertEqual(self.cell(self.summary(target), "synthetic-arm", C8, 1, A)["fps_median"], 2 / 9)
        self.assertEqual(validate.problems_for(target), [])

    def test_regeneration_from_the_bundles_inside_a_dataset(self):
        slow = self.run_bundle("arm64", {**ARM, (A, C8, 1): [9 * S] * 3}, run_id="arm64-20260102T000000Z-aaaaaaaa")
        select = {"arm64": ARM_RUN}
        target = self.assemble(self.arm(), slow, self.x86(), select=select)
        again = assemble.assemble(target / "lock.json", sorted((target / "bundles").iterdir()), self.tmp / "again", select, harness=HARNESS)
        self.assertEqual(target.name, again.name)
        first = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        second = {p.relative_to(again).as_posix(): p.read_bytes() for p in again.rglob("*") if p.is_file()}
        self.assertEqual(first, second)

    def test_existing_dataset_is_kept_or_refused(self):
        target = self.assemble(self.arm(), self.x86())
        before = (target / "summary.json").read_bytes()
        self.assertEqual(self.assemble(self.arm(), self.x86()), target)  # same content: no-op
        (target / "summary.csv").write_text("changed\n")
        with self.assertRaisesRegex(BenchError, "exists with different content"):
            self.assemble(self.arm(), self.x86())
        self.assertEqual((target / "summary.json").read_bytes(), before)


class HarnessIdentityTest(Base):
    def test_summary_names_the_assembling_harness_and_the_locking_commit(self):
        arm, x86 = self.arm(), self.x86()
        doc = self.summary(self.assemble(arm, x86))
        # Synthetic lock: class synthetic, commit and snapshot of the injected assembling harness.
        self.assertEqual(doc["aggregation"]["harness"], {**HARNESS, "class": "synthetic"})
        self.assertEqual(doc["reproduce"]["harness_commit"], LOCK["lock"]["harness"]["snapshot"]["commit"])
        self.assertNotEqual(doc["reproduce"]["harness_commit"], HARNESS["commit"])
        manifest = json.loads((self.out / "datasets" / doc["dataset_id"] / "manifest.json").read_text())
        self.assertEqual(manifest["aggregation"]["harness"], doc["aggregation"]["harness"])

    def test_standard_lock_gives_the_class_of_the_assembling_harness(self):
        lock = lock_with(lambda lock: lock.update(origin="standard"))
        self.lock_path.write_text(json.dumps(lock))
        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="development")
        dirty = {**HARNESS, "class": "development"}
        self.assertEqual(self.summary(self.assemble(arm, harness=dirty))["aggregation"]["harness"]["class"], "development")

    def test_other_aggregation_code_never_shares_a_dataset_id(self):
        arm = self.arm()
        first = self.assemble(arm)
        other = self.assemble(arm, harness={**HARNESS, "snapshot_sha256": "sha256:" + "b2" * 32})
        self.assertNotEqual(first.name, other.name)
        self.assertEqual(self.assemble(arm).name, first.name)  # same harness, same inputs: same dataset

    def test_official_refuses_a_development_harness(self):
        lock = lock_with(lambda lock: lock.update(origin="standard"))
        self.lock_path.write_text(json.dumps(lock))
        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="verified")
        x86 = self.run_bundle("x86-64", X86, lock=lock, run_class="verified", run_id=X86_RUN, machine="synthetic-x86")
        self.assertRejected(arm, x86, text="assembling harness has class development", official=True,
                            harness={**HARNESS, "class": "development"})  # fmt: skip
        self.assertTrue(self.assemble(arm, x86, official=True).exists())


class SourceAgreementTest(Base):
    def test_snapshot_disagreement_between_runs_is_refused(self):
        arm = self.arm(snapshot_sha256="sha256:" + "11" * 32)
        x86 = self.x86(snapshot_sha256="sha256:" + "22" * 32)
        message = self.assertRejected(arm, x86, text="source of fixture-a differs between runs")
        for part in ("snapshot_sha256 sha256:" + "11" * 32, "(role arm64)", "sha256:" + "22" * 32, "(role x86-64)"):
            self.assertIn(part, message)

    def test_equal_snapshots_are_accepted(self):
        digest = "sha256:" + "33" * 32
        self.assemble(self.arm(snapshot_sha256=digest), self.x86(snapshot_sha256=digest))

    def test_attempts_of_a_failed_correctness_cell_are_rejected(self):
        def edit(documents):
            documents["correctness"][0]["outcome"] = "incorrect"

        self.assertRejected(self.arm(edit=edit), text="without a correctness row with outcome ok")


class MissingBuildTest(Base):
    def x86_without_b(self):
        cells = {k: None for k in full_cells(LOCK, "x86-64") if k[0] == B}
        return self.run_bundle("x86-64", cells, run_id=X86_RUN, machine="synthetic-x86", without=(B,))

    def test_run_without_a_build_gives_not_run_cells(self):
        target = self.assemble(self.arm(), self.x86_without_b())
        doc = self.summary(target)
        cell = self.cell(doc, "synthetic-x86", C8, 1, B)
        self.assertEqual((cell["status"], cell["build_id"]), ("not-run", None))
        self.assertEqual(self.cell(doc, "synthetic-x86", C8, 1, A)["status"], "ok")
        self.assertFalse(doc["complete"])
        self.assertEqual([b["machine_id"] for b in doc["builds"] if b["implementation"] == B], ["synthetic-arm"])
        self.assertEqual(validate.problems_for(target), [])

    def test_official_names_the_missing_build(self):
        lock = lock_with(lambda lock: lock.update(origin="standard"))
        self.lock_path.write_text(json.dumps(lock))
        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="verified")
        cells = {k: None for k in full_cells(lock, "x86-64") if k[0] == B}
        x86 = self.run_bundle("x86-64", cells, lock=lock, run_class="verified", run_id=X86_RUN,
                              machine="synthetic-x86", without=(B,))  # fmt: skip
        self.assertRejected(arm, x86, text=f"run {X86_RUN} has no build of {B}", official=True)


class TrustBoundaryTest(Base):
    def frames_edit(self, **frames):
        def edit(documents):
            documents["attempts"][1]["workload"]["frames"].update(frames)

        return edit

    def test_ok_attempt_must_match_the_lock_frame_count(self):
        for change in ({"expected": 2000}, {"reported": 3}, {"reported": 0}):
            with self.subTest(change=change):
                bundle_path = self.arm(edit=self.frames_edit(**change))
                self.assertRejected(bundle_path, text="but the lock has 2 frames")

    def test_fps_uses_the_lock_frame_count(self):
        def edit(documents):
            documents["attempts"][1]["workload"]["frames"].update(source="decoder", reported=2)

        cell = self.cell(self.summary(self.assemble(self.arm(edit=edit))), "synthetic-arm", C8, 1, A)
        self.assertEqual((cell["frames"], cell["fps_median"]), (2, 2.0))

    def test_official_needs_verified_builds(self):
        lock = lock_with(lambda lock: lock.update(origin="standard"))
        self.lock_path.write_text(json.dumps(lock))

        def edit(documents):
            documents["builds"][0]["class"] = "development"

        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="verified", edit=edit)
        x86 = self.run_bundle("x86-64", X86, lock=lock, run_class="verified", run_id=X86_RUN, machine="synthetic-x86")
        self.assertRejected(arm, x86, text="of run " + ARM_RUN + " has class development, not verified", official=True)

    def test_run_and_build_classes_must_fit_the_lock_origin(self):
        standard = lock_with(lambda lock: lock.update(origin="standard"))
        self.lock_path.write_text(json.dumps(standard))
        self.assertRejected(self.run_bundle(lock=standard), text="a standard lock allows no synthetic runs and builds")
        self.lock_path.write_text(json.dumps(LOCK))
        message = self.assertRejected(self.arm(run_class="development"), text="a synthetic lock needs synthetic")
        self.assertIn("build bld-", message)

    def test_one_read_of_the_bundle_gives_the_parsed_bytes(self):
        bundle_path = self.arm()
        real = bundle.read_bundle

        def swap_after_read(archive, name=None):
            bundle_path.write_bytes(b"swapped")  # a file change after the read must not matter
            return real(archive, name)

        with mock.patch.object(bundle, "read_bundle", swap_after_read):
            target = self.assemble(bundle_path)
        copied = (target / "bundles" / f"{ARM_RUN}.tar.gz").read_bytes()
        self.assertEqual(self.summary(target)["machines"][0]["bundle"]["sha256"], assemble.sha256_bytes(copied))
        self.assertEqual(validate.problems_for(target), [])

    def test_metric_values_are_bounded_by_the_schema(self):
        row = json.loads((FIXTURES / "run" / "attempts.jsonl").read_text().splitlines()[1])
        for metric in ("wall_time", "cpu_time", "peak_rss"):
            row["workload"]["metrics"][metric] = 10**15
            self.assertEqual(schema.validate(row), [])
            row["workload"]["metrics"][metric] = 10**15 + 1
            self.assertNotEqual(schema.validate(row), [])
            row["workload"]["metrics"][metric] = 1

    def test_lock_with_repeated_ids_is_invalid(self):
        def twice(lock):
            lock["suite"]["cases"].append(copy.deepcopy(lock["suite"]["cases"][0]))
            lock["implementations"].append(copy.deepcopy(lock["implementations"][0]))
            lock["roles"]["arm64"]["threads"] = [1, 1]

        problems = "\n".join(schema.validate(make_lock(twice)))
        for text in ("suite.cases: 'fixture-case-8bit' appears more than once", "implementations: 'fixture-a'",
                     "roles.arm64.threads: 1 appears more than once"):  # fmt: skip
            self.assertIn(text, problems)


class StatusRuleTest(Base):
    def cell_of(self, cells, state="complete", edit=None):
        bundle_path = self.run_bundle("arm64", {**ARM, (A, C8, 1): cells}, state=state, edit=edit)
        return self.cell(self.summary(self.assemble(bundle_path)), "synthetic-arm", C8, 1, A)

    def test_interrupted_attempt_is_ignored_when_the_counts_are_full(self):
        cell = self.cell_of([S, "interrupted", S, S])
        self.assertEqual((cell["status"], cell["n_measured"], cell["n_valid"]), ("ok", 4, 3))
        self.assertEqual(cell["fps_median"], 2.0)

    def test_short_counts_are_error_in_a_complete_run_and_interrupted_otherwise(self):
        self.assertEqual(self.cell_of([S, S, "interrupted"])["status"], "error")
        self.assertEqual(self.cell_of([S, S, "interrupted"], state="interrupted")["status"], "interrupted")

    def test_a_failed_warm_up_counts(self):
        def edit(documents):
            documents["attempts"][0]["outcome"] = "incorrect"  # the warm-up of the first cell

        self.assertEqual(self.cell_of([S, S, S], edit=edit)["status"], "incorrect")

    def test_more_ok_attempts_than_the_lock_requires_is_refused(self):
        bundle_path = self.run_bundle("arm64", {**ARM, (A, C8, 1): [S] * 4})
        self.assertRejected(bundle_path, text="4 ok measure attempts, the lock requires 3")


class StatisticsTest(Base):
    def test_even_repetition_count_uses_the_two_middle_values(self):
        lock = lock_with(lambda lock: lock["measurement"].update(repetitions=4))
        self.lock_path.write_text(json.dumps(lock))
        cells = full_cells(lock, "arm64", {(A, C8, 1): [S, 2 * S, 4 * S, 5 * S]}, default=(S,) * 4)
        archive = write_run(self.tmp, lock, "arm64", cells, machine_id="synthetic-arm", run_id=ARM_RUN)[0]
        cell = self.cell(self.summary(self.assemble(archive)), "synthetic-arm", C8, 1, A)
        # Walls 1, 2, 4, 5 s: median 3 s. fps 2, 1, 0.5, 0.4: sorted 0.4, 0.5, 1, 2: median 0.75.
        # Deviations 1.25, 0.25, 0.25, 0.35: sorted 0.25, 0.25, 0.35, 1.25: MAD 0.3.
        self.assertEqual((cell["wall_ns_median"], cell["fps_median"]), (3e9, 0.75))
        self.assertAlmostEqual(cell["fps_mad"], 0.3)
        for field in ("wall_ns_median", "fps_median", "fps_mad", "cpu_ns_median"):
            self.assertIsInstance(cell[field], float)

    def test_medians_are_floats_for_an_odd_count_too(self):
        doc = self.summary(self.assemble(self.arm()))
        cell = self.cell(doc, "synthetic-arm", C8, 2, B)
        for field in ("wall_ns_median", "fps_median", "fps_mad", "cpu_ns_median", "peak_rss_bytes_median"):
            self.assertIsInstance(cell[field], float)
        csv_text = (self.out / "datasets" / doc["dataset_id"] / "summary.csv").read_text()
        self.assertIn("1000000000.0", csv_text)

    def test_noise_boundary_at_50_permille(self):
        ms = 1_000_000
        for high, noisy in ((1025, False), (1026, True)):
            with self.subTest(high=high):
                # Range (high - 975) ms against 50 permille of the 1000 ms median: 50 ms is allowed, 51 ms is noisy.
                bundle_path = self.run_bundle("arm64", {**ARM, (A, C8, 2): [975 * ms, 1000 * ms, high * ms]})
                cell = self.cell(self.summary(self.assemble(bundle_path)), "synthetic-arm", C8, 2, A)
                self.assertEqual(cell["noisy"], noisy)
                shutil.rmtree(self.out)


class ExcludedEvidenceTest(Base):
    def test_excluded_runs_are_part_of_the_dataset_id(self):
        keep, other = self.arm(), self.run_bundle("arm64", ARM, run_id="arm64-20260102T000000Z-aaaaaaaa")
        third = self.run_bundle("arm64", {**ARM, (A, C8, 1): [9 * S] * 3}, run_id="arm64-20260103T000000Z-bbbbbbbb")
        select = {"arm64": ARM_RUN}
        one = self.assemble(keep, other, select=select)
        two = self.assemble(keep, other, third, select=select)
        self.assertNotEqual(one.name, two.name)
        self.assertEqual(self.summary(one)["cells"], self.summary(two)["cells"])


class CompletenessTest(Base):
    def test_missing_role_is_incomplete_and_counted_not_run(self):
        target = self.assemble(self.arm())
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["coverage"]["roles_present"], ["arm64"])
        self.assertEqual(manifest["coverage"]["status_counts"]["not-run"], 12)
        self.assertEqual(validate.problems_for(target), [])

    def test_interrupted_run_has_interrupted_cells(self):
        cells = {**ARM, (A, C8B, 2): None, (B, C8B, 2): None, (A, C10, 1): [S, S]}
        bundle = self.run_bundle("arm64", cells, state="interrupted")
        target = self.assemble(bundle, self.x86())
        doc = self.summary(target)
        self.assertEqual(self.cell(doc, "synthetic-arm", C8B, 2, A)["status"], "interrupted")  # no rows
        short = self.cell(doc, "synthetic-arm", C10, 1, A)
        self.assertEqual((short["status"], short["n_measured"], short["fps_median"]), ("interrupted", 2, None))
        self.assertFalse(doc["complete"])

    def test_missing_attempts_in_a_complete_run_are_an_error(self):
        target = self.assemble(self.run_bundle("arm64", {**ARM, (A, C10, 1): [S, S]}), self.x86())
        self.assertEqual(self.cell(self.summary(target), "synthetic-arm", C10, 1, A)["status"], "error")

    def test_no_correctness_row_in_a_complete_run_is_not_run_and_incomplete(self):
        target = self.assemble(self.run_bundle("arm64", {**ARM, (A, C8B, 2): None}), self.x86())
        doc = self.summary(target)
        self.assertEqual(self.cell(doc, "synthetic-arm", C8B, 2, A)["status"], "not-run")
        self.assertFalse(doc["complete"])

    def test_failed_attempt_precedence(self):
        cells = {**ARM, (A, C8, 1): [S, "error", "timeout"], (B, C8, 1): [S, "error", "incorrect"]}
        doc = self.summary(self.assemble(self.run_bundle("arm64", cells), self.x86()))
        self.assertEqual(self.cell(doc, "synthetic-arm", C8, 1, A)["status"], "timeout")
        self.assertEqual(self.cell(doc, "synthetic-arm", C8, 1, B)["status"], "incorrect")

    def test_cpu_median_is_null_when_one_attempt_lacks_it(self):
        cells = {**ARM, (A, C8, 2): [(S, 100), (S, None), (S, 300)]}
        doc = self.summary(self.assemble(self.run_bundle("arm64", cells), self.x86()))
        cell = self.cell(doc, "synthetic-arm", C8, 2, A)
        self.assertEqual((cell["status"], cell["cpu_ns_median"]), ("ok", None))
        self.assertEqual(cell["peak_rss_bytes_median"], 1 << 20)


class OfficialTest(Base):
    standard = staticmethod(lambda: lock_with(lambda lock: lock.update(origin="standard")))

    def official_bundles(self, **kwargs):
        lock = self.standard()
        self.lock_path.write_text(json.dumps(lock))
        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="verified", run_id=ARM_RUN, **kwargs)
        x86 = self.run_bundle("x86-64", X86, lock=lock, run_class="verified", run_id=X86_RUN, machine="synthetic-x86")
        return arm, x86

    def test_complete_verified_runs_make_an_official_dataset(self):
        target = self.assemble(*self.official_bundles(), official=True)
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual((manifest["origin"], manifest["complete"]), ("official", True))
        doc = self.summary(target)
        self.assertEqual(doc["aggregation"]["harness"]["class"], "verified")
        self.assertEqual(validate.problems_for(target), [])

    def test_local_origin_without_the_flag(self):
        target = self.assemble(*self.official_bundles())
        self.assertEqual(json.loads((target / "manifest.json").read_text())["origin"], "local")

    def test_official_refuses_a_missing_role(self):
        arm, _ = self.official_bundles()
        self.assertRejected(arm, text="role x86-64 has no run", official=True)

    def test_official_refuses_interrupted_runs(self):
        arm, x86 = self.official_bundles(state="interrupted")
        self.assertRejected(arm, x86, text=f"run {ARM_RUN} is interrupted, not complete", official=True)

    def test_official_refuses_development_class(self):
        lock = self.standard()
        self.lock_path.write_text(json.dumps(lock))
        arm = self.run_bundle("arm64", ARM, lock=lock, run_class="development")
        x86 = self.run_bundle("x86-64", X86, lock=lock, run_class="verified", run_id=X86_RUN, machine="synthetic-x86")
        self.assertRejected(arm, x86, text=f"run {ARM_RUN} has class development, not verified", official=True)

    def test_official_refuses_synthetic_lock(self):
        self.assertRejected(self.arm(), self.x86(), text="the lock is synthetic", official=True)


class CliTest(Base):
    def test_command_line(self):
        arm, x86 = self.arm(), self.x86()
        done = bench(ROOT, "assemble", "--lock", str(self.lock_path), str(arm), str(x86), "-o", str(self.out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("synthetic dataset, complete", done.stdout)
        (target,) = (self.out / "datasets").iterdir()
        self.assertEqual(bench(ROOT, "validate", str(target)).returncode, 0)

    def test_failures_exit_1_with_one_message_and_usage_errors_exit_2(self):
        arm = self.arm()
        done = bench(ROOT, "assemble", "--lock", str(self.lock_path), str(arm), "-o", str(self.out), "--official")
        self.assertEqual(done.returncode, 1)
        self.assertIn("bench assemble: cannot assemble an official dataset", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse(self.out.exists())
        done = bench(ROOT, "assemble", "--lock", str(self.lock_path), str(arm), "-o", str(self.out), "--select", "bad")
        self.assertEqual(done.returncode, 2)


class RealRunnerTest(unittest.TestCase):
    """The bundle comes from the real producer: lock, prepare, run and bundle with the fixture decoder."""

    def test_bundle_of_the_real_runner_assembles_into_a_valid_dataset(self):
        isolate_git()
        self.addCleanup(restore_git)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        campaign, work, lock = root / "campaign", root / "work", root / "LOCK.json"
        campaign.mkdir()
        # The fixture decoder reads its behaviour from the stream; its raw output is 12288 bytes of 0x07.
        stream = canonical_json({"mode": "ok", "fill": 7, "bytes": 12288, "frames": 2})
        synthetic_campaign.write_corpus(campaign, {"synthetic-8bit": (8, stream)})
        manifest = campaign / "corpus.toml"
        manifest.write_text(manifest.read_text().replace("6" * 64, hashlib.sha256(b"\x07" * 12288).hexdigest()))
        role = "arm64" if native_arch() == "aarch64" else "x86-64"
        common = ["--lock", str(lock), "--role", role, "--work", str(work)]
        archive = root / "run.tar.gz"
        steps = [
            ["lock", str(synthetic_campaign.make_plan(campaign)), "-o", str(lock)],
            ["prepare", *common, "--jobs", "1"],
            ["run", *common, "--machine-id", "synthetic-host"],
        ]
        for step in steps:
            done = bench(ROOT, *step)
            self.assertEqual(done.returncode, 0, f"{step[0]}: {done.stderr}")
        (run_id,) = [path.name for path in (work / "runs").iterdir()]
        self.assertIn(f"bench bundle --work {work} --run {run_id} -o {run_id}.tar.gz", done.stdout)
        for step in (["bundle", "--work", str(work), "--run", run_id, "-o", str(archive)],
                     ["assemble", "--lock", str(lock), str(archive), "-o", str(root / "out")]):  # fmt: skip
            done = bench(ROOT, *step)
            self.assertEqual(done.returncode, 0, f"{step[0]}: {done.stderr}")
        (dataset,) = (root / "out" / "datasets").iterdir()
        done = bench(ROOT, "validate", str(dataset))
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads((dataset / "summary.json").read_text())
        self.assertEqual(summary["origin"], "synthetic")
        cells = [cell for cell in summary["cells"] if cell["machine_id"] == "synthetic-host"]
        self.assertEqual(len(cells), 2 * len(json.loads(lock.read_text())["lock"]["roles"][role]["threads"]))
        self.assertEqual({(cell["status"], cell["n_valid"]) for cell in cells}, {("ok", 3)})


if __name__ == "__main__":
    unittest.main()
