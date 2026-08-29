"""The tripwire on `.github/workflows/`, now that there is something in it.

purser's repository is public; the machine that develops it holds the captain's real
financial corpus. `.github/workflows/` is where those two facts meet, because a workflow
is the one tracked artefact a stranger's pull request can cause to *run*. `ci.yml` is
that workflow, and it runs on every fork pull request.

Each policy is tested twice: once against `.github/workflows/` as it stands, and once
against a workflow written here to violate it. The second half is what proves the first
half would notice -- an assertion that only ever sees a passing directory cannot tell a
clean repository from a broken check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


@pytest.fixture
def lint(script):
    return script("check_workflow_policy")


@pytest.fixture
def workflows(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    return directory


def _write(directory: Path, body: str, name: str = "ci.yml") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


SAFE = """\
name: tests
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: python -m pytest
"""


# --------------------------------------------------------------------------
# The repository as it stands
# --------------------------------------------------------------------------


def test_the_repository_violates_no_workflow_policy(lint):
    assert lint.check(WORKFLOWS) == []


def test_public_ci_is_the_only_workflow():
    """One file. Each additional one is another thing a fork's pull request can run."""
    present = sorted(p.name for p in WORKFLOWS.glob("*.y*ml")) if WORKFLOWS.is_dir() else []
    assert present == ["ci.yml"]


def test_public_ci_is_unprivileged_and_github_hosted():
    """The shape the lint above allows, asserted directly on the file that ships.

    `lint.check` proves no workflow takes a forbidden shape; it cannot prove this one
    takes the intended shape, because a workflow that had quietly lost its triggers, or
    its `pull_request` run, would pass every policy in this file.
    """
    document = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    triggers = document.get("on", document.get(True))

    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"]["branches"] == ["main"]
    assert document["permissions"] == {"contents": "read"}

    jobs = document["jobs"]
    assert list(jobs) == ["checks"]
    assert jobs["checks"]["runs-on"] == "ubuntu-latest"
    assert "permissions" not in jobs["checks"], "the job needs nothing above the default"

    # Comments stripped: the header prose discusses secrets, which is not the same
    # as a `${{ secrets.* }}` expression handing one to a step.
    body = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "secrets." not in code, "public CI reads no secret; fixtures are synthetic"


def test_public_ci_runs_the_suite_and_the_category_reference_check():
    """The two commands the workflow exists to run, named where a rename would show."""
    document = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    commands = [
        step["run"] for step in document["jobs"]["checks"]["steps"] if "run" in step
    ]
    assert any(command.strip() == "python -m pytest" for command in commands)
    assert any(
        "scripts/check_category_refs.py" in command and "--no-private" in command
        for command in commands
    ), "public CI may read the tracked layer only, never the captain's overlay"


def test_public_ci_checks_out_without_leaving_a_token_behind():
    """`persist-credentials: false`: no token in `.git/config` for a later step to find.

    The default leaves the job's token in the checkout, where anything that runs
    afterwards -- a test, a dependency's build hook, a fork's new script -- can read it.
    """
    document = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    checkouts = [
        step for step in document["jobs"]["checks"]["steps"]
        if "actions/checkout@" in step.get("uses", "")
    ]
    assert checkouts, "the job checks the code out"
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False


def test_an_ordinary_safe_workflow_passes(lint, workflows):
    """The lint must not make a normal GitHub-hosted test workflow unwritable."""
    _write(workflows, SAFE)
    assert lint.check(workflows) == []


# --------------------------------------------------------------------------
# ...and the four shapes it must refuse
# --------------------------------------------------------------------------


def test_a_self_hosted_runner_is_refused(lint, workflows):
    """A self-hosted runner is a machine that can reach the private corpus."""
    _write(workflows, SAFE.replace("runs-on: ubuntu-latest", "runs-on: self-hosted"))
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "self-hosted runner" in violations[0]


def test_a_self_hosted_runner_hidden_in_a_label_list_is_refused(lint, workflows):
    _write(workflows, SAFE.replace(
        "runs-on: ubuntu-latest", "runs-on: [self-hosted, linux, x64]"
    ))
    assert any("self-hosted" in v for v in lint.check(workflows))


def test_a_self_hosted_runner_group_is_refused(lint, workflows):
    _write(workflows, SAFE.replace(
        "runs-on: ubuntu-latest",
        "runs-on:\n      group: builders\n      labels: [self-hosted]",
    ))
    assert any("self-hosted" in v for v in lint.check(workflows))


def test_pull_request_target_is_refused(lint, workflows):
    """It runs the base repo's token and secrets against a fork's code."""
    _write(workflows, SAFE.replace("  pull_request:", "  pull_request_target:"))
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "pull_request_target" in violations[0]


def test_a_workflow_run_that_consumes_artifacts_is_refused(lint, workflows):
    """Unpacking a fork's build output with the base repo's permissions."""
    _write(workflows, """\
name: comment
on:
  workflow_run:
    workflows: [tests]
    types: [completed]
permissions:
  contents: read
jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
      - run: ./report.sh
""")
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "consumes artifacts" in violations[0]


def test_a_workflow_run_that_consumes_nothing_is_allowed(lint, workflows):
    """The trigger is not the problem on its own; consuming the artifact is."""
    _write(workflows, """\
name: notify
on:
  workflow_run:
    workflows: [tests]
    types: [completed]
permissions:
  contents: read
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - run: echo done
""")
    assert lint.check(workflows) == []


def test_top_level_write_permissions_are_refused(lint, workflows):
    """A top-level grant applies to every job, including ones added later."""
    _write(workflows, SAFE.replace("  contents: read", "  contents: write"))
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "more than read at the top level" in violations[0]
    assert "pytest" in violations[0], "it must name the job that lacks a justification"


def test_write_all_at_the_top_level_is_refused(lint, workflows):
    _write(workflows, SAFE.replace("permissions:\n  contents: read", "permissions: write-all"))
    assert any("more than read at the top level" in v for v in lint.check(workflows))


def test_a_justified_top_level_grant_is_allowed(lint, workflows):
    """The escape hatch is a sentence a human wrote, not a flag."""
    _write(workflows, """\
name: release
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  publish:
    # permissions-justification: contents:write attaches the built wheel to the tag.
    runs-on: ubuntu-latest
    steps:
      - run: ./publish.sh
""")
    assert lint.check(workflows) == []


def test_a_second_unjustified_job_reopens_the_finding(lint, workflows):
    """The grant is top-level, so a later job inherits it without saying anything."""
    _write(workflows, """\
name: release
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  publish:
    # permissions-justification: contents:write attaches the built wheel to the tag.
    runs-on: ubuntu-latest
    steps:
      - run: ./publish.sh
  tidy:
    runs-on: ubuntu-latest
    steps:
      - run: ./tidy.sh
""")
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "tidy" in violations[0]
    assert "publish" not in violations[0]


def test_an_action_referenced_by_tag_is_refused(lint, workflows):
    """A tag is mutable by whoever owns it; the code it names can change under you."""
    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "actions/checkout@v4"
    ))
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "not pinned to a full commit SHA" in violations[0]
    assert "actions/checkout@v4" in violations[0], "it must name the reference"


def test_an_action_referenced_by_branch_is_refused(lint, workflows):
    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "actions/checkout@main"
    ))
    assert any("not pinned" in v for v in lint.check(workflows))


def test_a_short_sha_is_refused(lint, workflows):
    """A prefix is not an identity: a short SHA can be collided with deliberately."""
    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "actions/checkout@3d3c42e"
    ))
    assert any("not pinned" in v for v in lint.check(workflows))


def test_an_unpinned_reusable_workflow_call_is_refused(lint, workflows):
    """A job-level `uses:` runs a whole file someone else controls."""
    _write(workflows, """\
name: tests
on:
  pull_request:
permissions:
  contents: read
jobs:
  shared:
    uses: someone/else/.github/workflows/build.yml@v1
""")
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "not pinned" in violations[0]


def test_a_local_action_needs_no_pin(lint, workflows):
    """`./` is this repository's own code, already fixed by the commit under test."""
    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "./.github/actions/setup"
    ))
    assert lint.check(workflows) == []


def test_a_container_step_is_pinned_by_image_digest(lint, workflows):
    """The container equivalent of a SHA; a `docker://` tag is refused like any other."""
    digest = "docker://alpine@sha256:" + "a" * 64
    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", digest
    ))
    assert lint.check(workflows) == []

    _write(workflows, SAFE.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "docker://alpine:3.20"
    ))
    assert any("not pinned" in v for v in lint.check(workflows))


def test_read_only_permissions_need_no_justification(lint, workflows):
    _write(workflows, SAFE.replace("  contents: read",
                                   "  contents: read\n  pull-requests: read"))
    assert lint.check(workflows) == []


# --------------------------------------------------------------------------
# The parser's own sharp edge
# --------------------------------------------------------------------------


def test_the_yaml_on_key_is_read_as_a_trigger_not_a_boolean(lint, workflows):
    """YAML 1.1 resolves a bare `on:` to True. Reading only "on" sees no triggers.

    A lint that quietly finds no triggers passes every trigger-based policy, which
    is the failure mode where a tripwire is worse than none.
    """
    path = _write(workflows, SAFE.replace("  pull_request:", "  pull_request_target:"))
    import yaml
    document = yaml.safe_load(path.read_text())
    assert "on" not in document and True in document, "the gotcha is still real"
    assert lint._triggers(document) == {"pull_request_target", "push"}


def test_an_unparseable_workflow_is_a_violation_not_a_skip(lint, workflows):
    _write(workflows, "name: broken\n  bad: [indent\n")
    violations = lint.check(workflows)
    assert len(violations) == 1
    assert "not parseable YAML" in violations[0]


def test_a_missing_workflow_directory_is_simply_no_violations(lint, tmp_path):
    assert lint.check(tmp_path / "no-such-dir") == []
