"""The built-in check types, offline, through the shared flow (engine/run.py)."""

from __future__ import annotations

import unittest

from helpers import FakeHttp, RepoTest, UpdateError

from engine import config, run
from engine.checks import chocolatey, debian_stable, node_lts, rust_toolchain
from engine.http import Http


def checks(*cs):
    return config.validate({"version": 1, "checks": list(cs)})


RUST = {
    "name": "rust",
    "type": "rust-toolchain",
    "notes": "New Clippy lints can fail CI on this PR.",
    "edits": [
        {"file": "rust-toolchain.toml", "pattern": r'^channel = "(?P<version>[\d.]+)"$'},
        {"file": "server/Cargo.toml", "pattern": r'^rust-version = "(?P<version>[\d.]+)"$'},
        {"file": "server/Dockerfile", "pattern": r"\brust:(?P<version>[\d.]+)-slim-\w+", "docker_image": "library/rust"},
    ],
}
CHANNEL = '[pkg.rust]\nversion = "{v} (0ef1bc386 2026-11-09)"\n'


class RustTest(RepoTest):
    FILES = {
        "rust-toolchain.toml": '[toolchain]\nchannel = "1.99.1"\n',
        "server/Cargo.toml": '[package]\nrust-version = "1.99.1"\n',
        "server/Dockerfile": "FROM rust:1.99.1-slim-trixie AS builder\n",
    }

    def run_check(self, latest, images=None, dry_run=False):
        http = FakeHttp({rust_toolchain.CHANNEL_URL: CHANNEL.format(v=latest)}, images=images)
        return run.run(self.repo.dir, checks(RUST), "rust", http, dry_run, in_place=True)

    def test_up_to_date_is_nothing(self):
        self.assertEqual(self.run_check("1.99.1").result, "none")

    def test_every_place_moves_together(self):
        o = self.run_check("1.100.0")
        self.assertEqual((o.result, o.title, o.version), ("pr", "Update Rust to 1.100.0", "1.100.0"))
        self.assertEqual(o.changed_files, ["rust-toolchain.toml", "server/Cargo.toml", "server/Dockerfile"])
        o.changes.write()
        self.assertIn('channel = "1.100.0"', self.repo.read("rust-toolchain.toml"))
        self.assertIn("rust:1.100.0-slim-trixie", self.repo.read("server/Dockerfile"))
        self.assertIn("<!-- dependency-update:rust -->", o.body)
        self.assertIn("1.99.1 → 1.100.0", o.body)
        self.assertIn("Image `library/rust:1.100.0-slim-trixie` exists", o.body)
        self.assertIn("New Clippy lints", o.body)
        self.assertIn("- `server/Cargo.toml`", o.body)
        self.assertIn("nothing merges automatically", o.body)

    def test_no_image_yet_is_nothing(self):
        self.assertEqual(self.run_check("1.100.0", images=set()).result, "none")

    def test_an_unreachable_source_fails(self):
        http = FakeHttp({rust_toolchain.CHANNEL_URL: UpdateError("couldn't fetch")})
        with self.assertRaises(UpdateError):
            run.run(self.repo.dir, checks(RUST), "rust", http, False, True)


class NodeTest(RepoTest):
    FILES = {"frontend/.nvmrc": "24\n", "server/Dockerfile": "FROM node:24-alpine AS frontend\n"}
    CHECK = {
        "name": "nodejs",
        "type": "node-lts",
        "edits": [
            {"file": "frontend/.nvmrc", "pattern": r"\A(?P<version>\d+)$"},
            {"file": "server/Dockerfile", "pattern": r"\bnode:(?P<version>\d+)-alpine\b", "docker_image": "library/node"},
        ],
    }

    def index(self, *releases):
        return {node_lts.INDEX_URL: [{"version": v, "lts": lts} for v, lts in releases]}

    def test_only_a_newer_lts_major_counts(self):
        same = FakeHttp(self.index(("v24.99.0", "X"), ("v25.0.0", False)))
        self.assertEqual(run.run(self.repo.dir, checks(self.CHECK), "nodejs", same, False, True).result, "none")
        newer = FakeHttp(self.index(("v27.0.0", False), ("v26.3.0", "Y"), ("v24.99.0", "X")))
        o = run.run(self.repo.dir, checks(self.CHECK), "nodejs", newer, False, True)
        self.assertEqual((o.title, o.version), ("Update Node.js to 26 LTS", "26"))
        o.changes.write()
        self.assertEqual(self.repo.read("frontend/.nvmrc"), "26\n")
        self.assertIn("node:26-alpine", self.repo.read("server/Dockerfile"))


FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  {entries}
  {next}
</feed>"""


def feed(vs, next_url=None):
    entries = "".join(
        f"<entry><m:properties><d:Version>{v}</d:Version>"
        f"<d:IsPrerelease>{'true' if pre else 'false'}</d:IsPrerelease></m:properties></entry>"
        for v, pre in vs
    )
    return FEED.format(entries=entries, next=f'<link rel="next" href="{next_url}" />' if next_url else "")


class ChocolateyTest(RepoTest):
    FILES = {"installer/inno-setup-version": "6.7.1\n"}
    CHECK = {
        "name": "inno-setup",
        "type": "chocolatey",
        "package": "innosetup",
        "edits": [{"file": "installer/inno-setup-version", "pattern": r"\A(?P<version>[\d.]+)$"}],
    }

    def test_the_highest_stable_version_across_pages(self):
        http = FakeHttp(
            {
                chocolatey.feed_url("innosetup"): feed([("5.5.9.20160928", False), ("6.2.0", False)], "page2"),
                "page2": feed([("99.0.0-beta", True), ("6.10.0", False), ("7.0.0", True)]),
            }
        )
        o = run.run(self.repo.dir, checks(self.CHECK), "inno-setup", http, False, True)
        self.assertEqual(o.title, "Update innosetup to 6.10.0")
        o.changes.write()
        self.assertEqual(self.repo.read("installer/inno-setup-version"), "6.10.0\n")
        self.assertIn("https://community.chocolatey.org/packages/innosetup/6.10.0", o.body)


def release(codename, version):
    return f"Origin: Debian\nCodename: {codename}\nVersion: {version}\n"


class DebianTest(RepoTest):
    FILES = {"Dockerfile": "FROM rust:1.99.1-slim-bookworm AS b\nFROM debian:bookworm-slim\n"}
    CHECK = {
        "name": "debian",
        "type": "debian-stable",
        "mode": "issue",
        "notes": "Check glibc.",
        "edits": [
            {"file": "Dockerfile", "pattern": r"\bdebian:(?P<version>[a-z]+)-slim\b"},
            {"file": "Dockerfile", "pattern": r"\brust:[\d.]+-slim-(?P<version>[a-z]+)\b", "docker_image": "library/rust"},
        ],
    }
    D = debian_stable.DISTS

    def test_a_newer_stable_is_an_issue(self):
        http = FakeHttp(
            {f"{self.D}/stable/Release": release("trixie", 13), f"{self.D}/trixie/Release": release("trixie", 13),
             f"{self.D}/bookworm/Release": release("bookworm", 12)},
            images=set(),
        )
        o = run.run(self.repo.dir, checks(self.CHECK), "debian", http, False, True)
        self.assertEqual((o.result, o.title), ("issue", "Move from Debian bookworm to trixie"))
        self.assertIsNone(o.changes, "an issue, not a PR")
        self.assertIn("`library/rust:1.99.1-slim-trixie` **is not published yet**", o.body)
        self.assertIn("+FROM debian:trixie-slim", o.body)
        self.assertIn("Check glibc.", o.body)
        self.assertIn("Later runs update this issue in place", o.body)
        self.assertEqual(self.repo.read("Dockerfile"), self.FILES["Dockerfile"], "nothing written")

    def test_the_same_or_a_newer_release_is_nothing(self):
        http = FakeHttp({f"{self.D}/stable/Release": release("bookworm", 12)})
        self.assertEqual(run.run(self.repo.dir, checks(self.CHECK), "debian", http, False, True).result, "none")
        http = FakeHttp(
            {f"{self.D}/stable/Release": release("trixie", 13), f"{self.D}/trixie/Release": release("trixie", 13),
             f"{self.D}/bookworm/Release": release("bookworm", 14)}
        )
        self.assertEqual(run.run(self.repo.dir, checks(self.CHECK), "debian", http, False, True).result, "none")

    def test_a_codename_gone_from_the_mirror_is_old(self):
        http = FakeHttp({f"{self.D}/stable/Release": release("trixie", 13), f"{self.D}/trixie/Release": release("trixie", 13)})
        self.assertEqual(run.run(self.repo.dir, checks(self.CHECK), "debian", http, False, True).result, "issue")


SHA_OLD, SHA_NEW = "a" * 64, "b" * 64
CLOUDFLARED = {
    "name": "cloudflared",
    "type": "github-release",
    "repo": "cloudflare/cloudflared",
    "edits": [
        {"file": "pins.env", "pattern": r"^CLOUDFLARED_VERSION=(?P<version>[\d.]+)$", "docker_image": "cloudflare/cloudflared"},
        {"file": "pins.env", "pattern": r"^CLOUDFLARED_LINUX_AMD64_SHA256=(?P<sha256>[0-9a-f]{64})$", "asset": "cloudflared-linux-amd64"},
    ],
}
API = "https://api.github.com/repos/cloudflare/cloudflared/releases?per_page=100"
DL = "https://github.com/cloudflare/cloudflared/releases/download/2026.12.0/cloudflared-linux-amd64"


def gh_release(tag, prerelease=False, draft=False, digest=SHA_NEW, size=1000):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "html_url": f"https://github.com/cloudflare/cloudflared/releases/tag/{tag}",
        "assets": [
            {"name": "cloudflared-linux-amd64", "size": size, "digest": f"sha256:{digest}" if digest else None,
             "browser_download_url": f"https://github.com/cloudflare/cloudflared/releases/download/{tag}/cloudflared-linux-amd64"},
        ],
    }


class GithubReleaseTest(RepoTest):
    FILES = {"pins.env": f"CLOUDFLARED_VERSION=2026.11.0\nCLOUDFLARED_LINUX_AMD64_SHA256={SHA_OLD}\n"}

    def http(self, releases, download=(SHA_NEW, 1000)):
        return FakeHttp({API: releases}, downloads={DL: download})

    def releases(self, **kw):
        return [gh_release("2027.1.0-rc1", prerelease=True), gh_release("2027.2.0", draft=True),
                gh_release("2026.12.0", **kw), gh_release("2026.11.0", digest=SHA_OLD), gh_release("nightly")]

    def test_the_highest_release_with_a_verified_hash(self):
        http = self.http(self.releases())
        o = run.run(self.repo.dir, checks(CLOUDFLARED), "cloudflared", http, False, True)
        self.assertEqual((o.result, o.title), ("pr", "Update cloudflare/cloudflared to 2026.12.0"))
        o.changes.write()
        self.assertEqual(self.repo.read("pins.env"), f"CLOUDFLARED_VERSION=2026.12.0\nCLOUDFLARED_LINUX_AMD64_SHA256={SHA_NEW}\n")
        self.assertEqual(http.downloaded, [DL])
        self.assertIn("https://github.com/cloudflare/cloudflared/compare/2026.11.0...2026.12.0", o.body)
        self.assertIn(f"| `cloudflared-linux-amd64` | `{SHA_NEW}` | 1,000 |", o.body)
        self.assertIn("matched a download", o.body)

    def test_prereleases_count_when_asked(self):
        c = dict(CLOUDFLARED, prerelease=True, edits=CLOUDFLARED["edits"][:1])
        c["tag_pattern"] = r"^(?P<version>[\d.]+)(-rc\d+)?$"
        o = run.run(self.repo.dir, checks(c), "cloudflared", self.http(self.releases()), True, True)
        self.assertEqual(o.version, "2027.1.0")

    def test_a_download_that_doesnt_match_the_api_fails(self):
        with self.assertRaisesRegex(UpdateError, "doesn't match the release API"):
            run.run(self.repo.dir, checks(CLOUDFLARED), "cloudflared", self.http(self.releases(), ("0" * 64, 1000)), False, True)
        with self.assertRaisesRegex(UpdateError, "doesn't match the release API"):
            run.run(self.repo.dir, checks(CLOUDFLARED), "cloudflared", self.http(self.releases(), (SHA_NEW, 999)), False, True)

    def test_a_missing_digest_or_asset_fails(self):
        with self.assertRaisesRegex(UpdateError, "no SHA-256 digest"):
            run.run(self.repo.dir, checks(CLOUDFLARED), "cloudflared", self.http(self.releases(digest=None)), False, True)
        c = dict(CLOUDFLARED, edits=[CLOUDFLARED["edits"][0], dict(CLOUDFLARED["edits"][1], asset="cloudflared-{version}.deb")])
        with self.assertRaisesRegex(UpdateError, "no asset named 'cloudflared-2026.12.0.deb'"):
            run.run(self.repo.dir, checks(c), "cloudflared", self.http(self.releases()), False, True)

    def test_a_dry_run_doesnt_download(self):
        http = self.http(self.releases())
        o = run.run(self.repo.dir, checks(CLOUDFLARED), "cloudflared", http, True, True)
        self.assertEqual(http.downloaded, [])
        self.assertIn("this was a dry run", o.body)


class DockerTagTest(RepoTest):
    FILES = {"pins.env": "PROBE_IMAGE=curlimages/curl:8.11.1\n"}
    CHECK = {
        "name": "probe-image",
        "type": "docker-tag",
        "image": "curlimages/curl",
        "tag_pattern": r"^(?P<version>\d+\.\d+\.\d+)$",
        "edits": [{"file": "pins.env", "pattern": r"^PROBE_IMAGE=curlimages/curl:(?P<version>[\d.]+)$"}],
    }

    def test_the_highest_matching_tag(self):
        http = FakeHttp(tags={"curlimages/curl": ["latest", "8.9.0", "8.12.1", "8.12.1-rc", "8.11.1", "9"]})
        o = run.run(self.repo.dir, checks(self.CHECK), "probe-image", http, False, True)
        self.assertEqual(o.title, "Update curlimages/curl to 8.12.1")
        self.assertIn("https://hub.docker.com/r/curlimages/curl/tags", o.body)


class RegistryTest(unittest.TestCase):
    """The anonymous-token flow of the registry v2 API."""

    def test_a_401_challenge_then_pages_of_tags(self):
        calls = []

        class H(Http):
            def request(self, url, accept=None, method="GET", headers=None):
                calls.append((method, url, (headers or {}).get("Authorization")))
                if url.startswith("https://auth.docker.io/token"):
                    return 200, {}, b'{"token": "T"}'
                if not headers:
                    return 401, {"WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'}, b""
                if url.endswith("n=1000"):
                    return 200, {"Link": '</v2/library/rust/tags/list?n=1000&last=b>; rel="next"'}, b'{"tags": ["a", "b"]}'
                return 200, {}, b'{"tags": ["c"]}'

        self.assertEqual(H().image_tags("rust"), ["a", "b", "c"])
        self.assertIn(("GET", "https://auth.docker.io/token?scope=repository%3Alibrary%2Frust%3Apull&service=registry.docker.io", None), calls)
        self.assertEqual(calls[-1], ("GET", "https://registry-1.docker.io/v2/library/rust/tags/list?n=1000&last=b", "Bearer T"))

    def test_image_names(self):
        from engine.http import split_image

        self.assertEqual(split_image("rust"), ("registry-1.docker.io", "library/rust"))
        self.assertEqual(split_image("library/rust"), ("registry-1.docker.io", "library/rust"))
        self.assertEqual(split_image("ghcr.io/a/b"), ("ghcr.io", "a/b"))
        self.assertEqual(split_image("quay.io/openbao/openbao"), ("quay.io", "openbao/openbao"))


if __name__ == "__main__":
    unittest.main()
