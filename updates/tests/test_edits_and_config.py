"""Edits (the shared pin mechanism), versions, and config validation."""

from __future__ import annotations

import os
import unittest

from helpers import INFRA, RepoTest, UpdateError, yaml_available

from engine import config, versions, yamlio
from engine.edits import Changes, Edit, apply, current_version


class VersionTest(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertTrue(versions.newer("1.100.0", "1.99.1"))
        self.assertFalse(versions.newer("1.0", "1.0.0"))
        self.assertTrue(versions.newer("2026.11.0", "2025.12.3"))
        with self.assertRaises(UpdateError):
            versions.numeric("1.x")


class EditTest(RepoTest):
    FILES = {
        "rust-toolchain.toml": '[toolchain]\nchannel = "1.99.1"\n',
        "a/Cargo.toml": 'rust-version = "1.99.1"\n',
        "Dockerfile": "FROM rust:1.99.1-slim-bookworm AS b\nFROM rust:1.99.1-slim-bookworm AS c\n",
        "pins.env": "X_VERSION=1.0.0\nX_SHA256=" + "a" * 64 + "\nX_SIZE=10\n",
    }

    def changes(self):
        return Changes(self.repo.dir)

    def test_the_current_version_is_what_every_edit_agrees_on(self):
        edits = [
            Edit("rust-toolchain.toml", r'^channel = "(?P<version>[\d.]+)"$'),
            Edit("a/Cargo.toml", r'^rust-version = "(?P<version>[\d.]+)"$'),
            Edit("Dockerfile", r"\brust:(?P<version>[\d.]+)-slim-\w+", count=2),
        ]
        self.assertEqual(current_version(self.changes(), edits, "rust"), "1.99.1")

    def test_disagreeing_versions_fail(self):
        self.repo.write("a/Cargo.toml", 'rust-version = "1.98.0"\n')
        edits = [
            Edit("rust-toolchain.toml", r'^channel = "(?P<version>[\d.]+)"$'),
            Edit("a/Cargo.toml", r'^rust-version = "(?P<version>[\d.]+)"$'),
        ]
        with self.assertRaisesRegex(UpdateError, "disagrees with itself: 1.98.0 in a/Cargo.toml; 1.99.1 in rust-toolchain.toml"):
            current_version(self.changes(), edits, "rust")

    def test_wrong_match_counts_fail(self):
        with self.assertRaisesRegex(UpdateError, "expected .* 1 time.*found it 2"):
            current_version(self.changes(), [Edit("Dockerfile", r"rust:(?P<version>[\d.]+)")], "x")
        with self.assertRaisesRegex(UpdateError, "found it 0"):
            current_version(self.changes(), [Edit("pins.env", r"^NOPE=(?P<version>.*)$")], "x")
        with self.assertRaisesRegex(UpdateError, "doesn't exist"):
            current_version(self.changes(), [Edit("missing.txt", r"(?P<version>x)")], "x")

    def test_only_the_named_groups_change(self):
        c = self.changes()
        tags = apply(c, Edit("Dockerfile", r"\brust:(?P<version>[\d.]+)-slim-\w+", count=2), {"version": "1.100.0"})
        self.assertEqual(c.files["Dockerfile"], "FROM rust:1.100.0-slim-bookworm AS b\nFROM rust:1.100.0-slim-bookworm AS c\n")
        self.assertEqual(tags, ["1.100.0-slim-bookworm"] * 2)
        apply(c, Edit("pins.env", r"^X_SHA256=(?P<sha256>[0-9a-f]{64})$"), {"version": "2", "sha256": "b" * 64})
        apply(c, Edit("pins.env", r"^X_SIZE=(?P<size>\d+)$"), {"size": "99"})
        self.assertEqual(c.files["pins.env"], "X_VERSION=1.0.0\nX_SHA256=" + "b" * 64 + "\nX_SIZE=99\n")

    def test_image_tags(self):
        c = self.changes()
        self.assertEqual(apply(c, Edit("pins.env", r"^X_VERSION=(?P<version>[\d.]+)$"), {"version": "2.0.0"}), ["2.0.0"])
        tagged = Edit("Dockerfile", r"\brust:(?P<tag>(?P<version>[\d.]+)-slim)-\w+", count=2)
        self.assertEqual(apply(c, tagged, {"version": "9.9.9"}), ["9.9.9-slim"] * 2)

    def test_workflow_files_are_refused(self):
        with self.assertRaisesRegex(UpdateError, "never edit .github/workflows"):
            Edit(".github/workflows/ci.yml", r"(?P<version>x)")
        with self.assertRaisesRegex(UpdateError, "inside the repository"):
            Edit("../outside", r"(?P<version>x)")
        with self.assertRaises(UpdateError):
            Changes(self.repo.dir).set(".github/workflows/x.yml", "")

    def test_symlinks_are_refused(self):
        os.symlink("pins.env", self.repo.dir / "link.env")
        with self.assertRaisesRegex(UpdateError, "symbolic link"):
            Changes(self.repo.dir).read("link.env")


def valid(**extra):
    base = {
        "version": 1,
        "checks": [
            {
                "name": "rust",
                "type": "rust-toolchain",
                "edits": [{"file": "rust-toolchain.toml", "pattern": r'^channel = "(?P<version>[\d.]+)"$'}],
            }
        ],
    }
    base.update(extra)
    return base


class ConfigTest(unittest.TestCase):
    def problems(self, raw):
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(raw)
        return "\n".join(cm.exception.problems)

    def test_a_valid_config_is_normalised(self):
        c = config.validate(valid())
        self.assertEqual(c["checks"][0]["mode"], "pr")
        self.assertEqual(c["checks"][0]["edits"][0]["count"], 1)
        self.assertEqual(config.validate({"version": 1}), {"version": 1, "dependabot": {}, "checks": []})

    def test_every_problem_is_reported_with_where_it_is(self):
        p = self.problems(
            {
                "version": 2,
                "extra": 1,
                "checks": [
                    {"name": "Bad Name", "type": "rust-toolchain", "edits": [{"file": "x", "pattern": "nogroup"}]},
                    {"name": "a", "type": "nope"},
                    {"name": "b", "type": "chocolatey", "edits": [{"file": "f", "pattern": "(?P<version>.)"}]},
                    {"name": "c", "type": "custom", "run": "x", "mode": "pr", "edits": []},
                    {"name": "d", "type": "node-lts", "edits": [{"file": ".github/workflows/ci.yml", "pattern": "(?P<version>.)"}]},
                    {"name": "e", "type": "node-lts", "edits": [{"file": "f", "pattern": "(?P<sha256>.)"}]},
                    {"name": "f", "type": "github-release", "repo": "a/b", "tag_pattern": "v(.*)",
                     "edits": [{"file": "f", "pattern": "(?P<version>.)"}, {"file": "f", "pattern": "(?P<sha256>.)"}]},
                    {"name": "dependabot-config", "type": "rust-toolchain", "edits": [{"file": "f", "pattern": "(?P<version>.)"}]},
                    {"name": "b", "type": "rust-toolchain", "edits": [{"file": "f", "pattern": "(?P<version>.)", "count": 0}]},
                ],
            }
        )
        for want in [
            "extra: unknown key",
            "version: must be 1",
            "checks[0] (Bad Name).name: required",
            "checks[0] (Bad Name).edits[0].pattern: needs a (?P<version>",
            "checks[1] (a).type: one of",
            "checks[2] (b).package: required for type chocolatey",
            "checks[3] (c).mode: a custom check chooses",
            "checks[3] (c).edits: a custom check edits files itself",
            "checks[4] (d).edits[0]: .github/workflows/ci.yml: checks never edit",
            "checks[5] (e).edits[0].pattern: sha256/size groups are filled in from release assets",
            "checks[6] (f).tag_pattern: needs a (?P<version>",
            "checks[6] (f).edits[1].asset: required",
            "'dependabot-config' is reserved",
            "checks[8] (b).name: 'b' is used twice",
            "checks[8] (b).edits[0].count",
        ]:
            self.assertIn(want, p)

    def test_dependabot_additions(self):
        c = config.validate(valid(dependabot={"npm": [{"directory": "/frontend/", "ignore": []}]}))
        self.assertEqual(c["dependabot"]["npm"][0]["directory"], "/frontend")
        p = self.problems(valid(dependabot={"npm": [{"directory": "frontend", "schedule": {}}], "Bad": []}))
        self.assertIn("dependabot.npm[0].schedule: not allowed here (the schedule is the same for every repo", p)
        self.assertIn("dependabot.npm[0].directory: required", p)
        self.assertIn("dependabot.Bad: not a Dependabot package-ecosystem name", p)

    @unittest.skipUnless(yaml_available(), "needs yq or PyYAML")
    def test_infra_and_the_example_configs_are_valid(self):
        for path in (INFRA / ".github/updates.yml", INFRA / "examples/updates.yml"):
            with self.subTest(path=path.name):
                c = config.validate(yamlio.load(path))
                self.assertTrue(c["checks"])

    @unittest.skipUnless(yaml_available(), "needs yq or PyYAML")
    def test_yaml_round_trip(self):
        value = {"a": [{"b": "09:00", "c": ["x", "@types/node", "on"]}, {"d": {}}], "e": True, "f": 3, "g": "a: b"}
        self.assertEqual(yamlio.loads(yamlio.dump(value)), value)


if __name__ == "__main__":
    unittest.main()
