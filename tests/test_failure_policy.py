"""Check the reusable workflow's narrowly scoped failure policy."""

import unittest

from test_workflow import WORKFLOW, step


class FailurePolicyTests(unittest.TestCase):
    """Verify declarations and guards used by GitHub's step scheduler."""

    def test_continue_on_error_collections_is_opt_in_on_named_steps(self):
        trigger = WORKFLOW.get("on", WORKFLOW.get(True))
        inputs = trigger["workflow_call"]["inputs"]
        pack = inputs["pack_collections"]
        inp = inputs.get("continue_on_error_collections")
        self.assertIsNotNone(inp)
        self.assertEqual(inp["default"], "[]")
        self.assertEqual(inp["type"], pack["type"])
        self.assertEqual(inp.get("required"), pack.get("required"))
        expr = "${{ contains(fromJSON(inputs.continue_on_error_collections), matrix.collection) }}"
        for job, name in (
            ("build", "Run pre-build script"),
            ("build", "Build ${{ needs.get-upstream-matrix.outputs.package }}"),
            ("test", "Require a pack state from Build"),
            ("test", "Run pre-test script"),
            ("test", "Test ${{ needs.build.outputs.package }}"),
        ):
            self.assertEqual(step(job, name).get("continue-on-error"), expr, name)
        for job in ("build", "test"):
            self.assertNotIn("continue-on-error", WORKFLOW["jobs"][job])
        self.assertIs(step("test", "Restore the cached build")["continue-on-error"], True)
        self.assertNotEqual(step("test", "Restore the cached build").get("continue-on-error"), expr)
        for name in ("Save build cache", "Upload pack state and build dir"):
            self.assertNotIn("continue-on-error", step("build", name))

    def test_failed_pre_build_or_pack_build_skips_cache_and_upload(self):
        self.assertEqual(step("build", "Run pre-build script").get("id"), "pre-build")
        pack_build = step("build", "Build ${{ needs.get-upstream-matrix.outputs.package }}")
        self.assertEqual(pack_build.get("id"), "pack-build")
        self.assertEqual(pack_build.get("if"), "${{ steps.pre-build.outcome != 'failure' }}")
        skip_failed = "steps.pre-build.outcome != 'failure' && steps.pack-build.outcome != 'failure'"
        self.assertEqual(
            step("build", "Save build cache")["if"],
            "${{ steps.cache.outputs.cache-hit != 'true' && " + skip_failed + " }}",
        )
        self.assertEqual(
            step("build", "Upload pack state and build dir")["if"],
            "${{ inputs.upload_pack_state_artifacts && " + skip_failed + " }}",
        )

    def test_tests_require_successful_restore(self):
        pre = step("test", "Run pre-test script")
        test = step("test", "Test ${{ needs.build.outputs.package }}")
        self.assertEqual(pre["if"], "${{ inputs.pre_test != '' && steps.restore.outcome == 'success' }}")
        self.assertEqual(test["if"], "${{ steps.restore.outcome == 'success' }}")
        restore = step("test", "Restore the cached build")
        self.assertEqual(restore["with"], {"state": "${{ matrix.state }}"})
        self.assertEqual(restore["id"], "restore")
        require = step("test", "Require a pack state from Build")
        self.assertEqual(require["if"], "${{ steps.restore.outcome != 'success' }}")
        self.assertIn("exit 1", require["run"])
        allowed = {
            "Run pre-build script",
            "Build ${{ needs.get-upstream-matrix.outputs.package }}",
            "Restore the cached build",
            "Require a pack state from Build",
            "Run pre-test script",
            "Test ${{ needs.build.outputs.package }}",
        }
        for job in ("build", "test"):
            self.assertFalse(WORKFLOW["jobs"][job]["strategy"]["fail-fast"])
            for item in WORKFLOW["jobs"][job]["steps"]:
                if item.get("continue-on-error"):
                    self.assertIn(item["name"], allowed)
