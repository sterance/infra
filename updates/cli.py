#!/usr/bin/env python3
"""The dependency update engine's command line (see updates/README.md).

  cli.py validate [--root DIR]                 check .github/updates.yml
  cli.py check NAME [--root DIR] [--dry-run]   run one check; prints the diff and PR text
  cli.py check NAME --out DIR [--dry-run]      (CI) apply it in --root and leave result.json,
                                               body.md and changes.patch in DIR
  cli.py plan --root DIR --out FILE            (CI) validate, write the normalised config, and
                                               print the check matrix to $GITHUB_OUTPUT
  cli.py dependabot [--root DIR] [--paths-file F] [--config F]
                                               print the generated .github/dependabot.yml
  cli.py caller --infra-repo OWNER/infra [--ref main]
                                               print the caller workflow onboarding writes
  cli.py starter                               print a starter .github/updates.yml

Locally, `check` never changes your files: built-in checks print the diff,
and a custom check runs in a copy of the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import UpdateError, config as configmod, dependabot, run as runmod  # noqa: E402
from engine.http import Http  # noqa: E402

STARTER = """\
# What this repository pins, for the weekly dependency check
# (<INFRA_REPO>/.github/workflows/dependency-updates.yml).
# Every option and check type: https://github.com/<INFRA_REPO>/blob/main/updates/README.md
# and https://github.com/<INFRA_REPO>/blob/main/examples/updates.yml
#
# Dependabot (.github/dependabot.yml, generated from this file) already covers the
# ecosystems it detects: Cargo, npm, pip, Go modules and GitHub Actions.
version: 1

# dependabot:
#   npm:
#     - directory: /frontend
#       ignore:
#         - dependency-name: "@types/node"
#           update-types: [version-update:semver-major]

checks: []
"""


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            if "\n" in value:
                f.write(f"{name}<<__EOF__\n{value}\n__EOF__\n")
            else:
                f.write(f"{name}={value}\n")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def load_config(args) -> dict:
    if getattr(args, "config_json", None):
        return json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    return configmod.load(args.root, args.config)


def fence(text: str, lang: str = "") -> str:
    n = 3
    while "`" * n in text:
        n += 1
    return f"{'`' * n}{lang}\n{text.rstrip()}\n{'`' * n}\n"


def cmd_validate(args) -> int:
    c = load_config(args)
    print(f"{args.config}: valid, {len(c['checks'])} check(s): {', '.join(x['name'] for x in c['checks']) or 'none'}")
    return 0


def cmd_plan(args) -> int:
    c = load_config(args)
    names = [x["name"] for x in c["checks"]] + [runmod.DEPENDABOT_CHECK]
    if args.fail and args.fail not in names:
        raise UpdateError(f"fail: {args.fail!r} isn't a check (checks: {', '.join(names)})")
    args.out.write_text(json.dumps(c), encoding="utf-8")
    matrix = [{"name": x["name"], "type": x["type"]} for x in c["checks"]]
    matrix.append({"name": runmod.DEPENDABOT_CHECK, "type": "dependabot"})
    set_output("matrix", json.dumps({"include": matrix}))
    set_output("config", json.dumps(c))
    print(f"{len(c['checks'])} check(s) and the Dependabot sync: {', '.join(names)}")
    return 0


def cmd_check(args) -> int:
    c = load_config(args)
    in_place = args.out is not None
    outcome = runmod.run(args.root, c, args.name, Http(), args.dry_run, in_place)
    if outcome.result == "none":
        print("\nNothing to do.")
        if in_place:
            runmod.write_outputs(args.root, outcome, args.out)
        return 0

    print(f"\n{outcome.result.upper()}: {outcome.title}\n")
    if in_place:
        patch = runmod.write_outputs(args.root, outcome, args.out)
    else:
        patch = outcome.changes.diff() if outcome.changes is not None else outcome.preview
    if patch:
        print(patch)
    print("---- description ----\n")
    print(outcome.body)
    if args.dry_run:
        summary(
            f"### {args.name}: would open {'a PR' if outcome.result == 'pr' else 'an issue'}\n\n"
            f"**{outcome.title}**\n\n"
            + (f"<details open><summary>Diff</summary>\n\n{fence(patch, 'diff')}\n</details>\n\n" if patch else "")
            + f"<details><summary>Description</summary>\n\n{fence(outcome.body, 'markdown')}\n</details>\n"
        )
    return 0


def cmd_dependabot(args) -> int:
    c = load_config(args)
    if args.paths_file:
        paths = [p for p in Path(args.paths_file).read_text(encoding="utf-8").splitlines() if p]
    else:
        paths = runmod._repo_files(args.root)
    sys.stdout.write(dependabot.render(paths, c, dependabot.load_defaults()))
    return 0


def cmd_caller(args) -> int:
    d = dependabot.load_defaults()
    cron = d.get("workflow_cron")
    if not isinstance(cron, str):
        raise UpdateError("updates/defaults.yml needs workflow_cron")
    sys.stdout.write(
        f"""\
# Managed by onboard-repo.sh: the logic lives in {args.infra_repo}
# (.github/workflows/dependency-updates.yml), and what this repository pins is
# described in .github/updates.yml. Re-run onboard-repo.sh --updates to update this file.
name: Dependency updates
on:
  schedule:
    - cron: "{cron}"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Run every check and show the diffs; open nothing"
        type: boolean
        default: false
      fail:
        description: "Make this check fail on purpose (to test the failure issue)"
        type: string
        default: ""
permissions: {{ contents: read, issues: write, actions: read, id-token: write }}
jobs:
  updates:
    uses: {args.infra_repo}/.github/workflows/dependency-updates.yml@{args.ref}
    with:
      bao_addr: ${{{{ vars.BAO_ADDR }}}}
      infra_repo: {args.infra_repo}
      infra_ref: {args.ref}
      dry_run: ${{{{ inputs.dry_run == true }}}}
      fail: ${{{{ inputs.fail || '' }}}}
"""
    )
    return 0


def cmd_starter(args) -> int:
    sys.stdout.write(STARTER.replace("<INFRA_REPO>", args.infra_repo))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--root", type=Path, default=Path("."), help="the repository (default: .)")
        sp.add_argument("--config", default=configmod.CONFIG_PATH, help="config path inside --root")
        sp.add_argument("--config-json", help="an already validated config (from plan)")

    sp = sub.add_parser("validate")
    common(sp)
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("plan")
    common(sp)
    sp.add_argument("--out", type=Path, required=True)
    sp.add_argument("--fail", default="")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("check")
    common(sp)
    sp.add_argument("name")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--out", type=Path)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("dependabot")
    common(sp)
    sp.add_argument("--paths-file", help="list of the repo's files, one per line (default: git ls-files)")
    sp.set_defaults(fn=cmd_dependabot)

    sp = sub.add_parser("caller")
    sp.add_argument("--infra-repo", required=True)
    sp.add_argument("--ref", default="main")
    sp.set_defaults(fn=cmd_caller)

    sp = sub.add_parser("starter")
    sp.add_argument("--infra-repo", default="<owner>/infra")
    sp.set_defaults(fn=cmd_starter)

    args = p.parse_args(argv)
    if hasattr(args, "root"):
        args.root = args.root.resolve()
    try:
        return args.fn(args)
    except UpdateError as e:
        msg = str(e)
        if os.environ.get("GITHUB_ACTIONS"):
            # One annotation per line, so the run's summary shows every problem.
            for line in msg.splitlines():
                if line.strip():
                    print(f"::error::{line.strip()}", file=sys.stderr)
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
