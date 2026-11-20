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
                    [
                        {
                            "collection": "latest",
                            "state": expected_state,
                            "effective_collection": "nightly-260102",
                        }
                    ],
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
                    {
                        "collection": "latest",
                        "state": latest,
                        "effective_collection": "nightly-260102",
                    },
                    {
                        "collection": "HEAD",
                        "state": head,
                        "effective_collection": "HEAD",
                    },
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

    def restore_steps(self):
        """Load restore steps by display name."""
        action = yaml.safe_load((ROOT / ".github/actions/restore-pack-state/action.yml").read_text(encoding="utf-8"))
        return {s["name"]: s for s in action["runs"]["steps"]}

    def test_restore_is_direct_named_download(self):
        action = yaml.safe_load((ROOT / ".github/actions/restore-pack-state/action.yml").read_text(encoding="utf-8"))
        self.assertEqual(set(action["inputs"]), {"state"})
        self.assertNotIn("outputs", action)
        self.assertEqual(len(action["runs"]["steps"]), 2)
        download = self.restore_steps()["Download the packed build"]
        self.assertEqual(download["with"], {"name": "${{ inputs.state }}"})
        self.assertNotIn("continue-on-error", download)
        self.env["STATE"] = "missing-state"
        script = self.restore_steps()["Untar the pack state and build dir"]["run"]
        self.assertNotEqual(self.run_script(script, False).returncode, 0)
        (self.root / "missing-state.tar").write_text("corrupt archive", encoding="utf-8")
        self.assertNotEqual(self.run_script(script, False).returncode, 0)

    def test_restore_uses_emitted_artifact_handles(self):
        upload = step("build", "Upload pack state and build dir")["with"]
        self.assertEqual(upload["state"], "${{ matrix.state }}")
        self.assertNotIn("label", upload)
        self.assertNotIn("package", upload)
        self.assertNotIn("collection", upload)
        outputs = WORKFLOW["jobs"]["build"]["outputs"]
        self.assertEqual(
            outputs["build_n_test_matrix"],
            "${{ needs.get-upstream-matrix.outputs.build_n_test_matrix }}",
        )
        self.assertNotIn("collections", outputs)
        self.assertNotIn("pack_states", outputs)
        matrix = WORKFLOW["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(
            matrix["include"],
            "${{ fromJSON(needs.build.outputs.build_n_test_matrix) }}",
        )
        self.assertNotIn("collection", matrix)
        self.assertNotIn("state", matrix)
        self.assertEqual(
            step("test", "Restore the cached build")["with"]["state"],
            "${{ matrix.state }}",
        )

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
        self.env["IPKG_PATH"] = str(ipkg)
        script = step("get-upstream-matrix", "Inspect package")["run"]
        self.run_script(script)
        original = self.values()["hash"]
        (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
        self.run_script(script)
        self.assertNotEqual(self.values()["hash"], original)
        configured = self.values()["hash"]
        (self.project / "src").mkdir()
        (self.project / "src/Main.idr").write_text("module Main\n", encoding="utf-8")
        self.run_script(script)
        self.assertNotEqual(self.values()["hash"], configured)

    def test_inspect_package(self):
        script = step("get-upstream-matrix", "Inspect package")["run"]
        self.env["IPKG_PATH"] = ""
        self.assertNotEqual(self.run_script(script, False).returncode, 0)
        ipkg = self.project / "example.ipkg"
        for builddir, expected in (("-- builddir = ignored", "build"), ('builddir = "output"', "output")):
            ipkg.write_text(f"package example\n{builddir}\n", encoding="utf-8")
            self.run_script(script)
            result = self.values()
            self.assertEqual(result["package"], "example")
            self.assertEqual(result["build_dir"], expected)
            self.assertEqual(result["project_dir"], ".")
            self.assertEqual(result["ipkg"], "./example.ipkg")
            self.assertRegex(result["hash"], r"^[a-f0-9]{16}$")
        (self.project / "other.ipkg").touch()
        self.assertNotEqual(self.run_script(script, False).returncode, 0)
        self.env["IPKG_PATH"] = "missing.ipkg"
        self.assertNotEqual(self.run_script(script, False).returncode, 0)
        nested = self.project / "nested"
        nested.mkdir()
        (nested / "lib.ipkg").write_text("package nested\n", encoding="utf-8")
        self.env["IPKG_PATH"] = "nested/lib.ipkg"
        self.run_script(script)
        self.assertEqual(self.values()["project_dir"], "nested")
        self.assertEqual(self.values()["package"], "nested")
        self.assertNotIn("GITHUB_ENV", script)
        outputs = WORKFLOW["jobs"]["get-upstream-matrix"]["outputs"]
        for key in ("package", "project_dir", "build_dir", "hash"):
            self.assertEqual(outputs[key], "${{ steps.inspect.outputs." + key + " }}")

    def test_empty_state_matrix(self):
        self.seed_pack_db()
        self.write_os_release()
        self.stub("chezscheme", 'echo "Chez Scheme Version 10.0.0" >&2')
        self.resolve_collections("[]")
        self.run_script(step("get-upstream-matrix", "Name pack states")["run"])
        result = self.values()
        self.assertEqual(json.loads(result["build_n_test_matrix"]), [])
        self.assertEqual(json.loads(result["pack_states"]), [])
        self.assertEqual(result["pack_state"], "")

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
        self.resolve_directories()
        self.env.update(self.values("GITHUB_ENV"))
        db = Path(self.env["PACK_STATE_DIR"]) / "db"
        db.mkdir(parents=True, exist_ok=True)
        for collection in (
            "HEAD",
            "nightly-260101",
            "nightly-260102",
            *extra_collections,
        ):
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
        self.resolve_collections(self.env["COLLECTIONS"])
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
        self.resolve_collections(self.env["COLLECTIONS"])
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

    def resolve_collections(self, collections):
        """Run batch resolution and retain its output for naming."""
        action = yaml.safe_load((ROOT / ".github/actions/effective-pack-collection/action.yml").read_text(encoding="utf-8"))
        self.env["COLLECTIONS"] = collections
        self.run_script(action["runs"]["steps"][0]["run"])
        result = self.values()["resolved_collections"]
        self.env["RESOLVED_COLLECTIONS"] = result
        return json.loads(result)

    def test_effective_collections(self):
        db = self.seed_pack_db()
        (db.parent / "pack.toml").write_text('collection="nightly-260101"', encoding="utf-8")
        names = ["latest", "HEAD", "nightly-260101"]
        rows = self.resolve_collections(json.dumps(names))
        self.assertEqual([r["collection"] for r in rows], names)
        self.assertEqual([r["effective_collection"] for r in rows], ["nightly-260102", "HEAD", "nightly-260101"])
        for row in rows:
            self.assertEqual(row["pack_collection_db"], str(db / (row["effective_collection"] + ".toml")))
        self.assertEqual(self.resolve_collections("[]"), [])
        for path in db.glob("nightly-*.toml"):
            path.unlink()
        self.assertEqual(self.resolve_collections('["latest"]')[0]["effective_collection"], "HEAD")
        for name in ('quote"name', "back\\slash"):
            (db / f"{name}.toml").touch()
            self.assertEqual(self.resolve_collections(json.dumps([name]))[0]["collection"], name)

    def test_invalid_effective_collections(self):
        self.seed_pack_db()
        action = yaml.safe_load((ROOT / ".github/actions/effective-pack-collection/action.yml").read_text(encoding="utf-8"))
        for value in ("bad", "{}", "null", "1", "[null]", '[""]', "[1]", '["../HEAD"]', '["."]', '[".."]', '["missing"]', '["line\\nname"]'):
            with self.subTest(value=value):
                self.env["COLLECTIONS"] = value
                self.assertNotEqual(self.run_script(action["runs"]["steps"][0]["run"], False).returncode, 0)
        shutil.rmtree(Path(self.env["PACK_STATE_DIR"]) / "db")
        self.env["COLLECTIONS"] = '["latest"]'
        self.assertNotEqual(self.run_script(action["runs"]["steps"][0]["run"], False).returncode, 0)

    def resolve_directories(self):
        """Run shared directory resolution and export its environment."""
        action = yaml.safe_load((ROOT / ".github/actions/resolve-pack-directories/action.yml").read_text(encoding="utf-8"))
        self.run_script(action["runs"]["steps"][0]["run"])
        self.env.update(self.values("GITHUB_ENV"))

    def test_pack_directory_resolution(self):
        for kind, suffix, xdg in (
            ("USER", ".config/pack", "CONFIG"),
            ("STATE", ".local/state/pack", "STATE"),
            ("CACHE", ".cache/pack", "CACHE"),
            ("BIN", ".local/bin", None),
        ):
            key = f"PACK_{kind}_DIR"
            self.env[key] = ""
            if xdg:
                self.env[f"XDG_{xdg}_HOME"] = ""
            self.resolve_directories()
            self.assertEqual(self.env[key], str(Path(self.env["HOME"]) / suffix))
            if xdg:
                self.env[key] = ""
                self.env[f"XDG_{xdg}_HOME"] = str(self.root / "xdg space")
                self.resolve_directories()
                self.assertEqual(self.env[key], str(self.root / "xdg space/pack"))
            self.env[key] = str(self.root / "override space")
            self.resolve_directories()
            self.assertEqual(self.env[key], str(self.root / "override space"))

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
        self.resolve_collections(self.env["COLLECTIONS"])
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

    def test_switch_collection_is_literal(self):
        name = 'nightly"; touch injected; #'
        switch = step("build", "Switch collection")
        log = self.root / "pack-args"
        self.env.update(
            COMMAND=switch["with"]["command"].replace("${{ matrix.effective_collection }}", name),
            EFFECTIVE_COLLECTION=name,
            WORKING_DIRECTORY=str(self.project),
            PACK_LOG=str(log),
        )
        self.stub("pack", 'printf "%s\\0" "$@" > "$PACK_LOG"')
        self.run_script(self.retry_script())
        self.assertEqual(log.read_bytes().split(b"\0"), [b"switch", name.encode(), b""])
        self.assertFalse((self.project / "injected").exists())
        self.assertEqual(switch["env"]["EFFECTIVE_COLLECTION"], "${{ matrix.effective_collection }}")

    def test_restore_precedes_compiler_setup_and_reselects_collection(self):
        self.prepare_cache()
        switch = step("build", "Switch collection")
        self.assertEqual(
            switch["uses"],
            "$/.github/actions/retry-pack-command",
        )
        self.assertEqual(
            switch["with"]["command"],
            'pack switch "$EFFECTIVE_COLLECTION"',
        )
        self.assertEqual(switch["with"]["working-directory"], "${{ env.PROJECT_DIR }}")
        self.assertNotIn("run", switch)
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
        self.env["COMMAND"] = switch["with"]["command"]
        self.env["WORKING_DIRECTORY"] = str(self.project)
        self.run_script(self.retry_script())
        self.assertIn("database-compiler", database.read_text(encoding="utf-8"))
        self.assertIn("nightly-260102", (state / "pack.toml").read_text(encoding="utf-8"))
        self.assertEqual((self.project / "pack.toml").read_text(encoding="utf-8"), CUSTOM)

    def test_bleeding_edge_pin_is_not_retried(self):
        action = "$/.github/actions/retry-pack-command"
        steps = WORKFLOW["jobs"]["build"]["steps"]
        restore_index = steps.index(step("build", "Restore build cache"))
        pin = step("build", "Pin bleeding-edge Idris2 commit")
        fetch = step("build", "Switch to the bleeding edge compiler")
        pin_index = steps.index(pin)
        self.assertEqual(pin["if"], "matrix.collection == 'HEAD'")
        self.assertEqual(fetch["if"], "matrix.collection == 'HEAD'")
        self.assertGreater(pin_index, restore_index)
        self.assertLess(pin_index, steps.index(fetch))
        self.assertEqual(fetch["uses"], action)
        self.assertEqual(fetch["with"]["command"], "pack fetch")
        self.assertEqual(fetch["with"]["working-directory"], "${{ env.PROJECT_DIR }}")
        self.assertNotIn("pack ", pin["run"])
        self.assertIn('commit = "latest:main"', pin["run"])
        self.assertIn("[idris2]", pin["run"])
        (self.project / "pack.toml").write_text(CUSTOM, encoding="utf-8")
        self.run_script(pin["run"])
        pinned = (self.project / "pack.toml").read_text(encoding="utf-8")
        self.assertEqual(pinned.count('commit = "latest:main"'), 1)
        self.assertNotIn("pack.toml", self.retry_script())

    # pylint: disable-next=too-many-locals,too-many-statements
    def test_artifact_round_trip(self):
        upload = yaml.safe_load((ROOT / ".github/actions/upload-pack-state/action.yml").read_text(encoding="utf-8"))
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
                self.resolve_directories()
                self.run_script(next(s for s in upload["runs"]["steps"] if s["name"] == "Tar the pack state and build dir")["run"])
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
                self.run_script(self.restore_steps()["Untar the pack state and build dir"]["run"])
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
        self.assertEqual(
            matrix["include"],
            "${{ fromJSON(needs.get-upstream-matrix.outputs.build_n_test_matrix) }}",
        )
        self.assertNotIn("collection", matrix)
        restore = step("build", "Restore build cache")
        save = step("build", "Save build cache")
        self.assertEqual(restore["with"]["key"], "${{ matrix.state }}")
        self.assertEqual(save["with"]["key"], "${{ matrix.state }}")
        script = step("build", "Compute cache key with pack state")["run"]
        self.assertNotIn("build-state-v2", script)
        self.assertNotIn("pack-state-v3", script)
        self.assertNotIn(
            "build-state-v2",
            (ROOT / ".github/workflows/build-and-test.yml").read_text(encoding="utf-8"),
        )

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
                {
                    "collection": "latest",
                    "state": latest,
                    "effective_collection": "nightly-260102",
                },
                {"collection": "HEAD", "state": head, "effective_collection": "HEAD"},
            ],
        )
        script = step("get-upstream-matrix", "Name pack states")["run"]
        self.assertIn('OS="${ID}-${VERSION_ID}"', script)
        self.assertIn("chezscheme --version 2>&1", script)
        bumped = self.name_states(chez="9.5.8", version_id="22.04")
        self.assertNotEqual(bumped["pack_state"], latest)
        self.assertIn("ubuntu-22.04-9.5.8-", bumped["pack_state"])

    def retry_script(self):
        """Load the retry action's shell."""
        action = yaml.safe_load((ROOT / ".github/actions/retry-pack-command/action.yml").read_text(encoding="utf-8"))
        return action["runs"]["steps"][0]["run"]

    def pack_attempts(self, body):
        """Stub pack and sleep, recording invocations and sleep arguments."""
        log = self.root / "pack-invocations"
        sleeps = self.root / "sleeps"
        log.unlink(missing_ok=True)
        sleeps.unlink(missing_ok=True)
        self.env["PACK_LOG"] = str(log)
        self.env["SLEEP_LOG"] = str(sleeps)
        self.stub("sleep", 'echo "$1" >> "$SLEEP_LOG"')
        self.stub("pack", body)
        return log, sleeps

    def warnings_of(self, result):
        """Return GitHub warning lines from a script run."""
        return [line for line in result.stdout.splitlines() if line.startswith("::warning::")]

    # pylint: disable-next=too-many-statements
    def test_retry_pack_command_on_ls_remote(self):
        script = self.retry_script()
        self.env["COMMAND"] = "pack fetch"
        self.env["WORKING_DIRECTORY"] = str(self.project)
        self.env["RETRY_PAUSE_SECONDS"] = "0"
        real_error = """echo 'Command: "git" "ls-remote" "https://git.sr.ht/~janus/utf8" "master"' >&2; exit 1"""

        log, _sleeps = self.pack_attempts(f"""
echo "$*" >> "$PACK_LOG"
if [ "$(grep -c . "$PACK_LOG")" -eq 1 ]; then
  {real_error}
fi
""")
        result = self.run_script(script)
        warnings = self.warnings_of(result)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["fetch", "fetch"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("retry 1 of 3", warnings[0])
        self.assertNotIn("git.sr.ht", warnings[0])

        log, sleeps = self.pack_attempts(f"""
echo "$*" >> "$PACK_LOG"
{real_error}
""")
        result = self.run_script(script, check=False)
        warnings = self.warnings_of(result)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4)
        self.assertEqual(len(warnings), 3)
        for number, warning in enumerate(warnings, start=1):
            self.assertIn(f"retry {number} of 3", warning)
        self.assertEqual(sleeps.read_text(encoding="utf-8").splitlines(), ["0", "0", "0"])

        log, sleeps = self.pack_attempts("""
echo "$*" >> "$PACK_LOG"
echo "[ fatal ] other" >&2
exit 7
""")
        result = self.run_script(script, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["fetch"])
        self.assertNotIn("::warning::", result.stdout)
        self.assertFalse(sleeps.exists())

        log, sleeps = self.pack_attempts("""
echo "$*" >> "$PACK_LOG"
echo 'Command: "git" "ls-remote"'
exit 0
""")
        result = self.run_script(script)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["fetch"])
        self.assertEqual(self.warnings_of(result), [])
        self.assertFalse(sleeps.exists())

        self.env.pop("RETRY_PAUSE_SECONDS")
        _log, sleeps = self.pack_attempts(f"""
echo "$*" >> "$PACK_LOG"
if [ "$(grep -c . "$PACK_LOG")" -eq 1 ]; then
  {real_error}
fi
""")
        self.run_script(script)
        self.assertEqual(sleeps.read_text(encoding="utf-8").strip(), "30")

    def test_relative_project_dir_is_applied_once(self):
        action = yaml.safe_load((ROOT / ".github/actions/retry-pack-command/action.yml").read_text(encoding="utf-8"))
        step_def = action["runs"]["steps"][0]
        self.assertNotIn("working-directory", step_def)
        nested = self.project / "lib"
        nested.mkdir()
        self.env["WORKING_DIRECTORY"] = "lib"
        self.env["COMMAND"] = "pwd -P"
        self.env["RETRY_PAUSE_SECONDS"] = "0"
        result = self.run_script(step_def["run"])
        self.assertIn(str(nested), result.stdout)


if __name__ == "__main__":
    unittest.main()
