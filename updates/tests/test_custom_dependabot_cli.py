"""Custom checks, the CI outputs (result.json, body.md, changes.patch), the
generated Dependabot config, and the command line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import UPDATES, FakeHttp, RepoTest, UpdateError, yaml_available

import cli
from engine import config, dependabot, run, yamlio

SCRIPT = """\
import json, os, pathlib
out = pathlib.Path(os.environ["UPDATE_OUT"])
mode = pathlib.Path("mode").read_text().strip()
(out / "env.json").write_text(json.dumps(dict(os.environ)))
pathlib.Path(os.environ.get("ENV_DUMP", "/dev/null")).write_text(json.dumps(dict(os.environ)))
if mode in ("pr", "lie-pr"):
    if mode == "pr":
        pathlib.Path("pins.json").write_text('{"version": "b2"}\\n')
    (out / "result.json").write_text(json.dumps({"result": "pr", "title": "Update thing to b2", "version": "b2"}))
    (out / "body.md").write_text("thing b1 -> b2" + (" (dry run)" if os.environ.get("UPDATE_DRY_RUN") else ""))
elif mode == "workflow":
    pathlib.Path(".github/workflows/ci.yml").write_text("changed")
    (out / "result.json").write_text(json.dumps({"result": "pr", "title": "x"}))
    (out / "body.md").write_text("x")
elif mode == "fail":
    raise SystemExit(3)
else:
    (out / "result.json").write_text(json.dumps({"result": "none"}))
"""


class CustomTest(RepoTest):
    FILES = {
        "check.py": SCRIPT,
        "mode": "pr\n",
        "pins.json": '{"version": "b1"}\n',
        ".github/workflows/ci.yml": "on: push\n",
    }

    def cfg(self):
        return config.validate({"version": 1, "checks": [{"name": "thing", "type": "custom", "run": "python3 check.py", "notes": "Test by hand."}]})

    def run_it(self, mode, in_place=True, dry_run=False):
        self.repo.write("mode", mode + "\n")
        self.repo.git("add", "-A")
        self.repo.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", mode)
        return run.run(self.repo.dir, self.cfg(), "thing", FakeHttp(), dry_run, in_place)

    def test_a_pr_from_the_working_tree(self):
        o = self.run_it("pr")
        self.assertEqual((o.result, o.title, o.version, o.changed_files), ("pr", "Update thing to b2", "b2", ["pins.json"]))
        self.assertTrue(o.body.startswith("<!-- dependency-update:thing -->\n\nthing b1 -> b2"))
        self.assertIn("Test by hand.", o.body)
        out = Path(tempfile.mkdtemp())
        patch = run.write_outputs(self.repo.dir, o, out)
        self.assertIn('+{"version": "b2"}', patch)
        self.assertEqual(json.loads((out / "result.json").read_text())["result"], "pr")
        self.assertEqual((out / "changes.patch").read_text(), patch)

    def test_locally_it_runs_in_a_copy(self):
        o = self.run_it("pr", in_place=False)
        self.assertEqual(o.changed_files, ["pins.json"])
        self.assertIn('+{"version": "b2"}', o.preview)
        self.assertEqual(self.repo.read("pins.json"), '{"version": "b1"}\n', "your tree is untouched")

    def test_the_environment(self):
        dump = Path(tempfile.mkdtemp()) / "env.json"
        os.environ["ENV_DUMP"] = str(dump)
        try:
            self.run_it("none", dry_run=True)
        finally:
            del os.environ["ENV_DUMP"]
        env = json.loads(dump.read_text())
        self.assertEqual(env["UPDATE_DRY_RUN"], "1")
        self.assertEqual(env["UPDATE_CHECK"], "thing")
        self.assertTrue(Path(env["UPDATE_OUT"]).is_absolute())

    def test_contract_violations_fail(self):
        with self.assertRaisesRegex(UpdateError, "says pr, but no file changed"):
            self.run_it("lie-pr")
        with self.assertRaisesRegex(UpdateError, "never edit .github/workflows"):
            self.run_it("workflow")
        self.repo.git("checkout", "-q", "--", ".")
        with self.assertRaisesRegex(UpdateError, "exited with 3"):
            self.run_it("fail")

    def test_nothing(self):
        self.assertEqual(self.run_it("none").result, "none")


DEFAULTS = {
    "schedule": {"interval": "weekly", "day": "monday", "time": "09:00", "timezone": "Australia/Melbourne"},
    "group_update_types": ["minor", "patch"],
}


class DependabotTest(unittest.TestCase):
    PATHS = [
        "Cargo.lock",
        "frontend/package-lock.json",
        "frontend/node_modules/x/package-lock.json",
        "tools/a/requirements-dev.txt",
        "svc/go.sum",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "docs/yarn.lock",
    ]

    def test_ecosystems_are_detected_but_not_docker(self):
        found = dependabot.detect(self.PATHS)
        self.assertEqual(
            found,
            {"cargo": {"/"}, "npm": {"/frontend", "/docs"}, "pip": {"/tools/a"}, "gomod": {"/svc"}, "github-actions": {"/"}},
        )

    @unittest.skipUnless(yaml_available(), "needs yq or PyYAML")
    def test_render_merges_additions(self):
        c = config.validate(
            {
                "version": 1,
                "dependabot": {
                    "npm": [
                        {"directory": "/frontend", "ignore": [{"dependency-name": "@types/node", "update-types": ["version-update:semver-major"]}]},
                        {"directory": "/docs", "enabled": False},
                    ],
                    "docker": [{"directory": "/"}],
                },
            }
        )
        text = dependabot.render(self.PATHS, c, DEFAULTS)
        self.assertTrue(text.startswith("# Managed by infra"))
        doc = yamlio.loads(text)
        by = {(u["package-ecosystem"], u.get("directory") or tuple(u["directories"])): u for u in doc["updates"]}
        self.assertEqual(
            list(by),
            [("cargo", "/"), ("npm", "/frontend"), ("pip", "/tools/a"), ("gomod", "/svc"), ("github-actions", "/"), ("docker", "/")],
        )
        npm = by[("npm", "/frontend")]
        self.assertEqual(npm["ignore"][0]["dependency-name"], "@types/node")
        self.assertEqual(npm["schedule"]["time"], "09:00")
        self.assertEqual(npm["groups"], {"npm-minor-and-patch": {"update-types": ["minor", "patch"]}})

    def test_several_directories_share_one_entry(self):
        text = dependabot.render(["a/Cargo.lock", "b/Cargo.lock"], config.validate({"version": 1}), DEFAULTS)
        self.assertIn("directories:\n      - /a\n      - /b\n", text)

    def test_the_sync_check(self):
        from helpers import Repo

        repo = Repo({"Cargo.lock": "", ".github/workflows/ci.yml": ""})
        try:
            o = run.run_dependabot(repo.dir, config.validate({"version": 1}), DEFAULTS)
            self.assertEqual((o.result, o.changed_files), ("pr", [".github/dependabot.yml"]))
            self.assertIn("it didn't exist", o.body)
            o.changes.write()
            self.assertEqual(run.run_dependabot(repo.dir, config.validate({"version": 1}), DEFAULTS).result, "none")
        finally:
            repo.close()


class CliTest(RepoTest):
    FILES = {
        ".github/updates.yml": "version: 1\nchecks:\n  - name: thing\n    type: custom\n    run: bash none.sh\n",
        "none.sh": 'echo \'{"result": "none"}\' > "$UPDATE_OUT/result.json"\n',
        "Cargo.lock": "",
    }

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(UPDATES / "cli.py"), *args], cwd=self.repo.dir, capture_output=True, text=True,
            env={**os.environ, **(env or {})}, check=False,
        )

    @unittest.skipUnless(yaml_available(), "needs yq or PyYAML")
    def test_plan_outputs_the_matrix(self):
        gh_out = self.repo.dir.parent / f"{self.repo.dir.name}.out"
        r = self.cli("plan", "--out", str(gh_out) + ".json", env={"GITHUB_OUTPUT": str(gh_out)})
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = dict(line.split("=", 1) for line in gh_out.read_text().splitlines() if "=" in line and "<<" not in line)
        self.assertEqual(json.loads(lines["matrix"]), {"include": [{"name": "thing", "type": "custom"}, {"name": "dependabot-config", "type": "dependabot"}]})
        r = self.cli("plan", "--out", str(gh_out) + ".json", "--fail", "nope")
        self.assertIn("'nope' isn't a check", r.stderr)

    @unittest.skipUnless(yaml_available(), "needs yq or PyYAML")
    def test_an_invalid_config_says_why(self):
        self.repo.write(".github/updates.yml", "version: 1\nchecks:\n  - name: x\n    type: nope\n")
        r = self.cli("validate", env={"GITHUB_ACTIONS": "true"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::- checks[0] (x).type: one of", r.stderr)

    def test_caller_and_starter(self):
        r = self.cli("caller", "--infra-repo", "me/infra")
        self.assertIn("uses: me/infra/.github/workflows/dependency-updates.yml@main", r.stdout)
        self.assertIn('cron: "0 22 * * 0"', r.stdout)
        self.assertIn("id-token: write", r.stdout)
        if yaml_available():
            doc = yamlio.loads(r.stdout)
            self.assertEqual(doc["jobs"]["updates"]["with"]["infra_ref"], "main")
            self.assertEqual(config.validate(yamlio.loads(self.cli("starter").stdout))["checks"], [])

    def test_check_outputs_in_ci(self):
        out = self.repo.dir.parent / f"{self.repo.dir.name}-art"
        r = self.cli("check", "thing", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((out / "result.json").read_text())["result"], "none")


if __name__ == "__main__":
    unittest.main()
