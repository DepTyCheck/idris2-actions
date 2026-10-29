"""Run the workflow's real shell snippets with isolated files and stub tools."""

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/build-and-test.yml").read_text(encoding="utf-8"))
CUSTOM = """[idris2]
url = "https://github.com/spcfox/Idris2"
commit = "41b6b8904e6bc04e8103be424add995b7ae44143"
"""


def step(job, name):
    """Find a workflow step by its display name."""
    return next(s for s in WORKFLOW["jobs"][job]["steps"] if s.get("name") == name)


# pylint: disable-next=too-many-public-methods
class WorkflowTests(unittest.TestCase):
    """Exercise workflow scripts and artifact restoration in temporary directories."""

    def setUp(self):
        # TestCase owns cleanup; the directory must outlive setUp.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()  # pylint: disable=consider-using-with
        self.project = self.root / "nested"
        self.project.mkdir()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(("PACK_", "XDG_")):
                del self.env[key]
        self.env.update(
            HOME=str(self.root / "home"),
            PROJECT_DIR=str(self.project),
            BUILD_DIR="build",
            PACKAGE="example",
            HASH="sources",
            PACK_COLLECTIONS="[]",
            CURRENT="111111111",
            UPSTREAM="222222222",
            COLLECTION="latest",
            COMPILER_HASH="",
            CONTAINER_TAG="latest",
            CONTAINER_ID="container",
            RUNNER_TEMP=str(self.root),
            GITHUB_WORKSPACE=str(self.root),
            GITHUB_ENV=str(self.root / "env"),
            GITHUB_OUTPUT=str(self.root / "output"),
            PATH=f"{self.tools}:{os.environ['PATH']}",
        )
        self.stub("idris2", 'echo "Idris 2, version 0.8.0-111111111"')
        self.stub("git", 'echo "222222222abcdef refs/heads/main"')
        self.stub("pack", 'echo "unexpected pack command" >&2; exit 99')
        # macOS lacks sha256sum; the workflow itself runs on Linux.
        if not shutil.which("sha256sum"):
            self.stub("sha256sum", 'exec shasum -a 256 "$@"')
        if not Path("/etc/os-release").exists():
            self.stub(
                "cat",
                'if [ "$1" = /etc/os-release ]; then echo "ID=debian"; else exec /bin/cat "$@"; fi',
            )

    def stub(self, name, body):
        """Write a stub executable on the test PATH."""
        path = self.tools / name
        path.write_text(f"#!/bin/bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, script, check=True):
        """Execute a workflow snippet using the temporary project."""
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=self.project,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def values(self, name="GITHUB_OUTPUT"):
        """Read the key/value entries emitted by a workflow snippet."""
        return dict(line.split("=", 1) for line in Path(self.env[name]).read_text(encoding="utf-8").splitlines() if "=" in line and "<<" not in line)

    def matrix(self, config=None, collections="[]"):
        """Run matrix selection with local configuration and collection inputs."""
        path = self.project / "pack.toml"
        if config is not None:
            path.write_text(config, encoding="utf-8")
        elif path.exists():
            path.unlink()
        self.env["PACK_COLLECTIONS"] = collections
        self.run_script(step("get-upstream-matrix", "Compute build collections")["run"])
        return self.values()

    def test_custom_compilers_filter_head(self):
        for config in (
            CUSTOM,
            '[idris2]\ncommit="abc"',
            '["idris2"]\nurl="fork" # comment',
        ):
            with self.subTest(config=config):
                result = self.matrix(config)
                self.assertEqual(json.loads(result["collections"]), ["latest"])
                expected_state = f"pack-state-example-latest-{result['compiler_hash']}"
                self.assertEqual(json.loads(result["pack_states"]), [expected_state])
                self.assertEqual(result["pack_state"], expected_state)
                self.assertEqual(json.loads(result["test_matrix"]), [{"collection": "latest", "state": expected_state}])

    def test_explicit_custom_collections(self):
        for requested in ('["latest","HEAD","nightly-example"]', '["HEAD"]'):
            self.env["PACK_COLLECTIONS"] = requested
            (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
            result = self.run_script(step("get-upstream-matrix", "Compute build collections")["run"], False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("HEAD", result.stderr)
        result = self.matrix(CUSTOM, '["nightly-example","latest"]')
        self.assertEqual(json.loads(result["collections"]), ["nightly-example", "latest"])

    def test_ordinary_and_nested_config(self):
        (self.root / "pack.toml").write_text(CUSTOM, encoding="utf-8")
        for config in (None, 'collection="nightly-example"', "[idris2]\nextra-args=[]"):
            result = self.matrix(config)
            self.assertEqual(json.loads(result["collections"]), ["latest", "HEAD"])
            self.assertEqual(json.loads(result["pack_states"]), ["pack-state-example-latest", "pack-state-example-HEAD"])
            self.assertEqual(
                json.loads(result["test_matrix"]),
                [
                    {"collection": "latest", "state": "pack-state-example-latest"},
                    {"collection": "HEAD", "state": "pack-state-example-HEAD"},
                ],
            )
        self.stub("git", 'echo "111111111abcdef refs/heads/main"')
        self.assertEqual(json.loads(self.matrix()["collections"]), ["latest"])
        self.assertEqual(json.loads(self.matrix(collections='["HEAD"]')["collections"]), ["HEAD"])

    def test_custom_does_not_query_upstream(self):
        self.stub("git", "exit 99")
        self.stub("idris2", "exit 99")
        self.matrix(CUSTOM)

    def test_collection_selection_uses_bash(self):
        script = step("get-upstream-matrix", "Compute build collections")["run"]
        self.assertIn("current=$(idris2 --version", script)
        self.assertNotIn("import subprocess", script)
        self.assertNotIn("python3", script)

    def test_compiler_section_and_quoted_values(self):
        config = """[packages.example]
url = "unrelated"
[ 'idris2' ] # custom compiler
url = 'https://example.com/compiler#fork' # comment
commit = "abc" # comment
[packages.other]
commit = "unrelated"
"""
        self.assertEqual(json.loads(self.matrix(config)["collections"]), ["latest"])
        self.assertEqual(
            json.loads(self.matrix('[packages.example]\nurl="unrelated"')["collections"]),
            ["latest", "HEAD"],
        )

    def test_full_toml_compiler_syntax(self):
        for config in (
            '[idris2]\ncommit="""abc"""',
            'idris2.commit="abc"',
            'idris2 = { commit = "abc" }',
            '[idris2]\n"commit"="abc"',
        ):
            self.assertEqual(json.loads(self.matrix(config)["collections"]), ["latest"])

    def test_duplicate_and_invalid_compiler_values_fail(self):
        for config in (
            '[idris2]\ncommit="abc"\ncommit="def"',
            '[idris2]\nurl="fork" trailing-junk',
        ):
            (self.project / "pack.toml").write_text(config, encoding="utf-8")
            result = self.run_script(step("get-upstream-matrix", "Compute build collections")["run"], False)
            self.assertNotEqual(result.returncode, 0)

    def test_collection_names_are_json_escaped(self):
        names = ['nightly-"quoted"', "nightly-\\backslash"]
        result = self.matrix(CUSTOM, json.dumps(names))
        self.assertEqual(json.loads(result["collections"]), names)
        self.assertEqual(
            json.loads(result["pack_states"]),
            [f"pack-state-example-{c}-{result['compiler_hash']}" for c in names],
        )
        self.assertEqual(
            json.loads(result["test_matrix"]),
            [{"collection": c, "state": f"pack-state-example-{c}-{result['compiler_hash']}"} for c in names],
        )

    def test_invalid_configuration_fails(self):
        for config in ("[idris2", "[idris2]\ncommit="):
            (self.project / "pack.toml").write_text(config, encoding="utf-8")
            result = self.run_script(step("get-upstream-matrix", "Compute build collections")["run"], False)
            self.assertNotEqual(result.returncode, 0)

    def test_explicit_collections_do_not_query_compilers(self):
        self.stub("git", "exit 99")
        self.stub("idris2", "exit 99")
        result = self.matrix(collections='["HEAD"]')
        self.assertEqual(json.loads(result["collections"]), ["HEAD"])

    def test_compiler_settings_are_not_passed_to_build(self):
        result = self.matrix(CUSTOM)
        for key in ("custom_compiler", "compiler"):
            self.assertNotIn(key, result)
            self.assertNotIn(key, WORKFLOW["jobs"]["get-upstream-matrix"]["outputs"])
        self.assertNotIn("CUSTOM_COMPILER", WORKFLOW["jobs"]["build"]["env"])

    def test_build_and_test_job_names_share_package_and_collection(self):
        def substring(job, prefix):
            name = WORKFLOW["jobs"][job]["name"]
            self.assertTrue(name.startswith(f"{prefix} "), name)
            rest = name.removeprefix(f"{prefix} ")
            return re.sub(r"\$\{\{ needs\.[^}]+\.outputs\.package \}\}", "package", rest)

        shared = "package (${{ matrix.collection }})"
        self.assertEqual(substring("build", "Build"), shared)
        self.assertEqual(substring("test", "Test"), shared)

    def test_test_job_runs_after_a_failed_build_unless_cancelled(self):
        self.assertEqual(WORKFLOW["jobs"]["test"]["if"], "${{ !cancelled() && inputs.run_tests }}")

    def test_missing_pack_state_fails_test_with_build_message(self):
        restore = step("test", "Restore the cached build")
        self.assertEqual(restore.get("id"), "restore")
        self.assertTrue(restore.get("continue-on-error"))
        names = [s.get("name") for s in WORKFLOW["jobs"]["test"]["steps"]]
        self.assertIn("Require a pack state from Build", names)
        require = step("test", "Require a pack state from Build")
        self.assertEqual(require["if"], "${{ steps.restore.outcome != 'success' }}")
        result = self.run_script(require["run"], False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The Build job for this pack state failed, so tests were skipped.", result.stdout + result.stderr)
        steps = WORKFLOW["jobs"]["test"]["steps"]
        require_index = steps.index(require)
        self.assertGreater(require_index, steps.index(restore))
        self.assertGreater(steps.index(step("test", "Run pre-test script")), require_index)
        self.assertGreater(steps.index(step("test", "Test ${{ needs.build.outputs.package }}")), require_index)

    def test_restore_uses_emitted_artifact_handles(self):
        self.assertEqual(step("build", "Upload pack state and build dir")["with"]["label"], "${{ env.COMPILER_HASH }}")
        outputs = WORKFLOW["jobs"]["build"]["outputs"]
        self.assertEqual(outputs["test_matrix"], "${{ needs.get-upstream-matrix.outputs.test_matrix }}")
        self.assertNotIn("collections", outputs)
        self.assertNotIn("pack_states", outputs)
        matrix = WORKFLOW["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(matrix["include"], "${{ fromJSON(needs.build.outputs.test_matrix) }}")
        self.assertNotIn("collection", matrix)
        self.assertNotIn("state", matrix)
        self.assertEqual(step("test", "Restore the cached build")["with"]["state"], "${{ matrix.state }}")

    def test_compiler_hash_uses_only_url_and_commit(self):
        original = self.matrix(CUSTOM)["compiler_hash"]
        self.assertRegex(original, r"^[0-9a-f]{16}$")
        self.assertEqual(self.matrix(CUSTOM + "bootstrap=true\n")["compiler_hash"], original)
        for old, new in (("spcfox", "other"), ("41b6", "51b6")):
            self.assertNotEqual(self.matrix(CUSTOM.replace(old, new))["compiler_hash"], original)
        self.assertEqual(self.matrix()["compiler_hash"], "")
        for config in ('[idris2]\nurl="fork"', '[idris2]\ncommit="abc"'):
            self.assertRegex(self.matrix(config)["compiler_hash"], r"^[0-9a-f]{16}$")

    def test_source_hash_with_optional_config_and_source_directory(self):
        ipkg = self.project / "example.ipkg"
        ipkg.write_text("package example\n", encoding="utf-8")
        self.env["IPKG"] = str(ipkg)
        script = step("get-upstream-matrix", "Hash sources for the cache key")["run"]
        self.run_script(script)
        original = self.values("GITHUB_ENV")["HASH"]
        (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
        self.run_script(script)
        self.assertNotEqual(self.values("GITHUB_ENV")["HASH"], original)

    def prepare_cache(self):
        """Create collection metadata and run cache-key preparation."""
        self.run_script(step("build", "Resolve pack directories")["run"])
        self.env.update(self.values("GITHUB_ENV"))
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        db.mkdir(parents=True, exist_ok=True)
        for collection in ("HEAD", "nightly-260101", "nightly-260102"):
            (db / f"{collection}.toml").write_text('[idris2]\ncommit="database-compiler"\n', encoding="utf-8")
        self.run_script(step("build", "Compute cache key with pack state")["run"])
        self.env.update(self.values("GITHUB_ENV"))
        return self.env["CACHE_KEY"]

    def test_combined_cache_paths(self):
        self.prepare_cache()
        paths = Path(self.env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").split("cache_paths<<EOF\n")[-1].split("\nEOF")[0].splitlines()
        self.assertEqual(
            set(paths),
            {
                str(self.project / "build"),
                *(
                    self.env[key]
                    for key in (
                        "PACK_USER_DIR",
                        "PACK_STATE_DIR",
                        "PACK_CACHE_DIR",
                        "PACK_BIN_DIR",
                    )
                ),
            },
        )
        restore = step("build", "Restore build cache")
        save = step("build", "Save build cache")
        self.assertEqual(restore["with"]["path"], "${{ steps.cache-key.outputs.cache_paths }}")
        self.assertEqual(save["with"]["path"], restore["with"]["path"])
        self.assertNotIn(str(self.project / "pack.toml"), paths)
        self.assertNotIn("restore-keys", restore["with"])

    def test_pack_directory_overrides(self):
        for prefix in ("XDG", "PACK"):
            with self.subTest(prefix=prefix):
                for kind, xdg in (
                    ("USER", "CONFIG"),
                    ("STATE", "STATE"),
                    ("CACHE", "CACHE"),
                ):
                    self.env[f"XDG_{xdg}_HOME"] = str(self.root / f"xdg-{kind}")
                    self.env.pop(f"PACK_{kind}_DIR", None)
                    if prefix == "PACK":
                        self.env[f"PACK_{kind}_DIR"] = str(self.root / f"explicit-{kind}")
                self.env["PACK_BIN_DIR"] = str(self.root / "explicit-bin")
                self.prepare_cache()
                for kind in ("USER", "STATE", "CACHE"):
                    expected = self.root / f"explicit-{kind}" if prefix == "PACK" else self.root / f"xdg-{kind}" / "pack"
                    self.assertEqual(self.env[f"PACK_{kind}_DIR"], str(expected))

    def test_cache_key_separates_inputs_and_database(self):
        original = self.prepare_cache()
        self.assertTrue(original.startswith("build-state-v2-"))
        for key in ("HASH", "CONTAINER_TAG", "COMPILER_HASH"):
            old = self.env[key]
            self.env[key] = "different"
            self.run_script(step("build", "Compute cache key with pack state")["run"])
            self.assertNotEqual(self.values("GITHUB_ENV")["CACHE_KEY"], original, key)
            self.env[key] = old
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        (db / "nightly-260102.toml").write_text("changed database", encoding="utf-8")
        self.run_script(step("build", "Compute cache key with pack state")["run"])
        self.assertNotEqual(self.values("GITHUB_ENV")["CACHE_KEY"], original)
        self.env["COLLECTION"] = "HEAD"
        self.run_script(step("build", "Compute cache key with pack state")["run"])
        head_key = self.values("GITHUB_ENV")["CACHE_KEY"]
        self.assertNotEqual(head_key, original)
        self.env["UPSTREAM"] = "333333333"
        self.run_script(step("build", "Compute cache key with pack state")["run"])
        self.assertNotEqual(self.values("GITHUB_ENV")["CACHE_KEY"], head_key)
        self.env["UPSTREAM"] = ""
        self.run_script(step("build", "Compute cache key with pack state")["run"])
        self.assertEqual(self.values("GITHUB_ENV")["CACHE_KEY"], head_key)

    def test_restore_precedes_compiler_setup_and_reselects_collection(self):
        self.prepare_cache()
        self.assertEqual(self.env["EFFECTIVE_COLLECTION"], "nightly-260102")
        steps = WORKFLOW["jobs"]["build"]["steps"]
        restore_index = steps.index(step("build", "Restore build cache"))
        for name in (
            "Switch to the bleeding edge compiler",
            "Switch collection",
            "Install dependencies",
        ):
            self.assertGreater(steps.index(step("build", name)), restore_index)
        # An exact cache keeps the DB but may change the global collection.
        state = Path(self.env["PACK_STATE_DIR"])
        (state / "pack.toml").write_text('collection="wrong"', encoding="utf-8")
        database = state / "db/nightly-260102.toml"
        self.assertFalse((self.root / "pack-collection.toml").exists())
        marker = state / "install/compiler"
        marker.parent.mkdir(parents=True)
        marker.touch()
        (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
        self.stub(
            "pack",
            """
test -f "$PACK_STATE_DIR/install/compiler"
test "$1" = switch
test "$2" = nightly-260102
grep -q database-compiler "$PACK_STATE_DIR/db/$2.toml"
printf 'collection="%s"\n' "$2" > "$PACK_STATE_DIR/pack.toml"
""",
        )
        self.run_script(step("build", "Switch collection")["run"])
        self.assertIn("database-compiler", database.read_text(encoding="utf-8"))
        self.assertIn("nightly-260102", (state / "pack.toml").read_text(encoding="utf-8"))
        self.assertEqual((self.project / "pack.toml").read_text(encoding="utf-8"), CUSTOM)

    # pylint: disable-next=too-many-locals,too-many-statements
    def test_artifact_round_trip(self):
        upload = yaml.safe_load((ROOT / ".github/actions/upload-pack-state/action.yml").read_text(encoding="utf-8"))
        restore = yaml.safe_load((ROOT / ".github/actions/restore-pack-state/action.yml").read_text(encoding="utf-8"))
        for mode in ("default", "pack", "xdg"):
            with self.subTest(mode=mode):
                directories = {}
                for kind, default, xdg in (
                    ("USER", ".config/pack", "CONFIG"),
                    ("STATE", ".local/state/pack", "STATE"),
                    ("CACHE", ".cache/pack", "CACHE"),
                    ("BIN", ".local/bin", None),
                ):
                    self.env.pop(f"PACK_{kind}_DIR", None)
                    if xdg:
                        self.env.pop(f"XDG_{xdg}_HOME", None)
                    if mode == "pack":
                        directory = self.root / f"explicit-{kind}"
                        self.env[f"PACK_{kind}_DIR"] = str(directory)
                    elif mode == "xdg" and xdg:
                        base = self.root / f"xdg-{kind}"
                        self.env[f"XDG_{xdg}_HOME"] = str(base)
                        directory = base / "pack"
                    else:
                        directory = Path(self.env["HOME"]) / default
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "marker").write_text(kind, encoding="utf-8")
                    directories[kind] = directory
                compiler = directories["STATE"] / "install/41b6/idris2/bin/idris2"
                compiler.parent.mkdir(parents=True, exist_ok=True)
                compiler.write_text("#!/bin/sh\necho custom-compiler\n", encoding="utf-8")
                compiler.chmod(0o755)
                launcher = directories["BIN"] / "idris2"
                launcher.unlink(missing_ok=True)
                launcher.symlink_to(compiler)
                (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
                build = self.project / "build"
                build.mkdir(exist_ok=True)
                (build / "Main.ttc").write_text("compiled", encoding="utf-8")
                result = self.matrix(CUSTOM)
                self.env.update(STATE=result["pack_state"], LABEL=result["compiler_hash"])
                describe = upload["runs"]["steps"][0]["run"]
                self.run_script(describe)
                self.assertEqual(self.values()["state"], self.env["STATE"])
                self.run_script(upload["runs"]["steps"][1]["run"])
                archive = self.root / f"{self.env['STATE']}.tar"
                with tarfile.open(archive) as packed:
                    members = {m.name for m in packed.getmembers()}
                    self.assertIn(str(compiler).lstrip("/"), members)
                    for directory in directories.values():
                        self.assertIn(str(directory / "marker").lstrip("/"), members)
                for directory in directories.values():
                    shutil.rmtree(directory)
                shutil.rmtree(build)
                (self.project / "pack.toml").unlink()
                self.run_script(restore["runs"]["steps"][1]["run"])
                self.assertTrue(launcher.is_symlink())
                self.assertEqual(
                    subprocess.check_output([str(launcher)], text=True).strip(),
                    "custom-compiler",
                )
                self.assertEqual((self.project / "pack.toml").read_text(encoding="utf-8"), CUSTOM)
                self.assertEqual((build / "Main.ttc").read_text(encoding="utf-8"), "compiled")
                for kind, directory in directories.items():
                    self.assertEqual((directory / "marker").read_text(encoding="utf-8"), kind)


if __name__ == "__main__":
    unittest.main()
