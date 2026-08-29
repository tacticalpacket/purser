"""No workflow yet. This is the tripwire for the first one.

purser's repository is about to be public; the machine that develops it holds the
captain's real financial corpus. `.github/workflows/` is where those two facts meet,
because a workflow is the one tracked artefact a stranger's pull request can cause to
*run*. This slice adds no workflows on purpose -- public CI is a later, separate gate --
so every assertion over the real directory passes vacuously today.

Vacuous is not the same as useless, and it is also not the same as trustworthy, so each
policy is tested twice: once against `.github/workflows/` as it stands, and once against
a workflow written here to violate it. The second half is what proves the first half
would notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
      - uses: actions/checkout@v4
      - run: python -m pytest
"""


# --------------------------------------------------------------------------
# The repository as it stands
# --------------------------------------------------------------------------


def test_the_repository_violates_no_workflow_policy(lint):
    assert lint.check(WORKFLOWS) == []


def test_this_slice_added_no_workflows():
    """Public CI is a separate later gate; adding one here was out of scope."""
    present = sorted(p.name for p in WORKFLOWS.glob("*.y*ml")) if WORKFLOWS.is_dir() else []
    assert present == [], f"workflows appeared without the security gate: {present}"


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
      - uses: actions/download-artifact@v4
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
