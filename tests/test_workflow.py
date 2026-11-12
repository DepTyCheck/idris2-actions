"""Run the workflow's real shell snippets with isolated files and stub tools."""

import hashlib
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
        # GNU prefixes '\' when a filename contains a backslash or newline.
        if not shutil.which("sha256sum"):
            self.stub(
                "sha256sum",
                r"""
if [ "$#" -eq 0 ]; then
  exec shasum -a 256
fi
escaped=0
for arg in "$@"; do
  case "$arg" in
    *\\*|*$'\n'*) escaped=1 ;;
  esac
done
if [ "$escaped" -eq 1 ]; then
  shasum -a 256 "$@" | awk '{print "\\" $0}'
else
  exec shasum -a 256 "$@"
fi
""",
            )
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
                result = self.name_states(config)
                self.assertEqual(json.loads(result["collections"]), ["latest"])
                db = Path(self.env["PACK_STATE_DIR"]) / "db"
                expected_state = self.expected_state("latest", "nightly-260102", db / "nightly-260102.toml")
                self.assertEqual(json.loads(result["pack_states"]), [expected_state])
                self.assertEqual(result["pack_state"], expected_state)
                self.assertEqual(
                    json.loads(result["build_n_test_matrix"]),
                    [{"collection": "latest", "state": expected_state, "effective_collection": "nightly-260102"}],
                )

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
            result = self.name_states(config)
            self.assertEqual(json.loads(result["collections"]), ["latest", "HEAD"])
            db = Path(self.env["PACK_STATE_DIR"]) / "db"
            latest = self.expected_state("latest", "nightly-260102", db / "nightly-260102.toml")
            head = self.expected_state("HEAD", "HEAD", db / "HEAD.toml", compiler_version="222222222")
            self.assertEqual(json.loads(result["pack_states"]), [latest, head])
            self.assertEqual(
                json.loads(result["build_n_test_matrix"]),
                [
                    {"collection": "latest", "state": latest, "effective_collection": "nightly-260102"},
                    {"collection": "HEAD", "state": head, "effective_collection": "HEAD"},
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
        result = self.name_states(CUSTOM, json.dumps(names))
        self.assertEqual(json.loads(result["collections"]), names)
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        expected = [self.expected_state(name, name, db / f"{name}.toml") for name in names]
        self.assertEqual(json.loads(result["pack_states"]), expected)
        self.assertEqual(
            json.loads(result["build_n_test_matrix"]),
            [{"collection": name, "state": state, "effective_collection": name} for name, state in zip(names, expected, strict=True)],
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
        self.assertTrue(step("test", "Restore the cached build").get("continue-on-error"))
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
        upload = step("build", "Upload pack state and build dir")["with"]
        self.assertEqual(upload["state"], "${{ matrix.state }}")
        self.assertNotIn("label", upload)
        self.assertNotIn("package", upload)
        self.assertNotIn("collection", upload)
        outputs = WORKFLOW["jobs"]["build"]["outputs"]
        self.assertEqual(outputs["build_n_test_matrix"], "${{ needs.get-upstream-matrix.outputs.build_n_test_matrix }}")
        self.assertNotIn("collections", outputs)
        self.assertNotIn("pack_states", outputs)
        matrix = WORKFLOW["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(matrix["include"], "${{ fromJSON(needs.build.outputs.build_n_test_matrix) }}")
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

    def write_os_release(self, version_id="24.04"):
        """Fixture os-release. The suite runs on machines without /etc/os-release."""
        path = self.root / "os-release"
        path.write_text(
            f'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="{version_id}"\nVERSION="24.04.1 LTS (Noble Numbat)"\nID=ubuntu\n',
            encoding="utf-8",
        )
        self.env["OS_RELEASE"] = str(path)

    def seed_pack_db(self, extra_collections=()):
        """Resolve pack dirs and create the DB files the name step hashes."""
        self.run_script(step("get-upstream-matrix", "Resolve pack directories")["run"])
        self.env.update(self.values("GITHUB_ENV"))
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        db.mkdir(parents=True, exist_ok=True)
        for collection in ("HEAD", "nightly-260101", "nightly-260102", *extra_collections):
            path = db / f"{collection}.toml"
            if not path.exists():
                path.write_text('[idris2]\ncommit="database-compiler"\n', encoding="utf-8")
        return db

    def expected_state(self, collection, effective, pack_db, compiler_version=""):
        """The shared pack-state-v3 string for one collection."""
        digest = hashlib.sha256(pack_db.read_bytes()).hexdigest()[:16]
        suffix = f"-{compiler_version}" if compiler_version else ""
        if self.env.get("COMPILER_HASH"):
            suffix += f"-{self.env['COMPILER_HASH']}"
        return (
            f"pack-state-v3-{self.env['CONTAINER_TAG']}-ubuntu-{self.env.get('OS_VERSION_ID', '24.04')}-"
            f"{self.env.get('CHEZ_VERSION', '10.0.0')}-{self.env['HASH']}-"
            f"{collection}-{effective}-{digest}{suffix}"
        )

    def name_states(self, config=None, collections="[]", chez="10.0.0", version_id="24.04"):
        """Run collection selection and the pack-state naming step."""
        result = self.matrix(config, collections)
        self.env["COLLECTIONS"] = result["collections"]
        self.env["COMPILER_HASH"] = result["compiler_hash"]
        self.env["UPSTREAM"] = result["upstream"]
        self.env["OS_VERSION_ID"] = version_id
        self.env["CHEZ_VERSION"] = chez
        self.write_os_release(version_id)
        self.stub("chezscheme", f'echo "Chez Scheme Version {chez}" >&2')
        extras = [name for name in json.loads(result["collections"]) if name != "latest"]
        self.seed_pack_db(extras)
        Path(self.env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
        self.run_script(step("get-upstream-matrix", "Name pack states")["run"])
        result.update(self.values())
        return result

    def prepare_cache(self):
        """Create collection metadata and run cache-path preparation."""
        self.env["COLLECTIONS"] = '["latest"]'
        self.env["COMPILER_HASH"] = ""
        self.env["OS_VERSION_ID"] = "24.04"
        self.env["CHEZ_VERSION"] = "10.0.0"
        self.write_os_release()
        self.stub("chezscheme", 'echo "Chez Scheme Version 10.0.0" >&2')
        self.seed_pack_db()
        Path(self.env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
        self.run_script(step("get-upstream-matrix", "Name pack states")["run"])
        row = json.loads(self.values()["build_n_test_matrix"])[0]
        self.env["EFFECTIVE_COLLECTION"] = row["effective_collection"]
        self.env["CACHE_KEY"] = row["state"]
        Path(self.env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
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

    def rerun_names(self):
        """Re-run pack-state naming after an input change."""
        Path(self.env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
        self.run_script(step("get-upstream-matrix", "Name pack states")["run"])
        return self.values()["pack_state"]

    def test_cache_key_separates_inputs_and_database(self):
        original = self.name_states()["pack_state"]
        self.assertTrue(original.startswith("pack-state-v3-"))
        for key in ("HASH", "CONTAINER_TAG", "COMPILER_HASH"):
            old = self.env[key]
            self.env[key] = "different"
            self.assertNotEqual(self.rerun_names(), original, key)
            self.env[key] = old
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        (db / "nightly-260102.toml").write_text("changed database", encoding="utf-8")
        self.assertNotEqual(self.rerun_names(), original)
        self.env["COLLECTIONS"] = '["HEAD"]'
        head_key = self.rerun_names()
        self.assertNotEqual(head_key, original)
        self.env["UPSTREAM"] = "333333333"
        self.assertNotEqual(self.rerun_names(), head_key)
        self.env["UPSTREAM"] = ""
        self.assertEqual(self.rerun_names(), head_key)

    def test_restore_precedes_compiler_setup_and_reselects_collection(self):
        self.prepare_cache()
        switch = step("build", "Switch collection")
        self.assertEqual(switch["env"]["EFFECTIVE_COLLECTION"], "${{ matrix.effective_collection }}")
        cache_step = step("build", "Compute cache key with pack state")
        self.assertNotIn("env", cache_step)
        self.assertNotIn("EFFECTIVE_COLLECTION", cache_step["run"])
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
                result = self.name_states(CUSTOM)
                self.assertEqual(set(upload["inputs"]), {"state", "project_dir", "build_dir"})
                self.assertTrue(upload["inputs"]["state"]["required"])
                self.env["STATE"] = result["pack_state"]
                self.run_script(upload["runs"]["steps"][0]["run"])
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

    def test_build_uses_published_state(self):
        matrix = WORKFLOW["jobs"]["build"]["strategy"]["matrix"]
        self.assertEqual(matrix["include"], "${{ fromJSON(needs.get-upstream-matrix.outputs.build_n_test_matrix) }}")
        self.assertNotIn("collection", matrix)
        restore = step("build", "Restore build cache")
        save = step("build", "Save build cache")
        self.assertEqual(restore["with"]["key"], "${{ matrix.state }}")
        self.assertEqual(save["with"]["key"], "${{ matrix.state }}")
        script = step("build", "Compute cache key with pack state")["run"]
        self.assertNotIn("build-state-v2", script)
        self.assertNotIn("pack-state-v3", script)
        self.assertNotIn("build-state-v2", (ROOT / ".github/workflows/build-and-test.yml").read_text(encoding="utf-8"))

    def test_readme_documents_shared_cache_key(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        formula = "pack-state-v3-$CONTAINER_TAG-$OS-$CHEZ-$HASH-$COLLECTION-$effective_collection-$pack_hash[-$compiler_version][-$COMPILER_HASH]"
        self.assertIn(formula, readme)
        self.assertNotIn("build-state-v2", readme)
        self.assertIn("ubuntu-24.04", readme)
        self.assertIn("10.0.0", readme)
        self.assertIn("same string", readme)

    def test_pack_state_name_includes_os_and_chez(self):
        result = self.name_states()
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        latest = self.expected_state("latest", "nightly-260102", db / "nightly-260102.toml")
        head = self.expected_state("HEAD", "HEAD", db / "HEAD.toml", compiler_version="222222222")
        self.assertNotIn("24.04.1", latest)
        self.assertEqual(json.loads(result["pack_states"]), [latest, head])
        self.assertEqual(result["pack_state"], latest)
        self.assertEqual(
            json.loads(result["build_n_test_matrix"]),
            [
                {"collection": "latest", "state": latest, "effective_collection": "nightly-260102"},
                {"collection": "HEAD", "state": head, "effective_collection": "HEAD"},
            ],
        )
        script = step("get-upstream-matrix", "Name pack states")["run"]
        self.assertIn('OS="${ID}-${VERSION_ID}"', script)
        self.assertIn("chezscheme --version 2>&1", script)
        bumped = self.name_states(chez="9.5.8", version_id="22.04")
        self.assertNotEqual(bumped["pack_state"], latest)
        self.assertIn("ubuntu-22.04-9.5.8-", bumped["pack_state"])


if __name__ == "__main__":
    unittest.main()
