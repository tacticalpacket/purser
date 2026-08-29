#!/usr/bin/env python3
"""Policy lint for GitHub Actions workflows in a repository that holds nothing.

purser's repository is public; the machine that develops it holds the captain's real
financial corpus. Those two facts meet in `.github/workflows/`, because a workflow is
the one tracked artefact that a stranger's pull request can cause to *run*.

`.github/workflows/ci.yml` is the one workflow, and it runs on every fork pull request.
This check exists so that it -- and anything added beside it later -- cannot take one of
the five shapes that would matter:

1. **`runs-on: self-hosted`** -- a self-hosted runner is the captain's own machine, or
   one that can reach it. Fork pull-request code running there is code running next to
   the corpus. Public CI runs on GitHub-hosted runners, against synthetic fixtures.

2. **`pull_request_target`** -- runs with the *base* repository's token and secrets while
   checking out the *fork's* code. It is the documented way to hand write access to an
   untrusted contributor, and there is no use for it here.

3. **`workflow_run` consuming pull-request artifacts** -- the same privilege escalation
   wearing a different hat: a privileged workflow unpacking an artifact a fork's build
   produced is executing the fork's bytes with the base repository's permissions.

4. **Top-level write permissions** -- the default token scope applies to every job in the
   file, including any job that later gets added to it. Write belongs on the one job that
   needs it, with a comment saying why:

       jobs:
         release:
           # permissions-justification: needs contents:write to attach the built wheel.
           permissions:
             contents: write

   A top-level grant above read is accepted only when every job carries such a comment.

5. **An action referenced by tag or branch** -- `actions/checkout@v4` resolves to whatever
   that tag points at when the job starts, and a tag is mutable by whoever owns it. A
   compromised or retargeted tag runs its new contents in a job that has already checked
   out this repository. Pin the full 40-character commit SHA, with the human-readable
   version in a trailing comment:

       - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

   GitHub enforces this independently -- the repository has `sha_pinning_required` set --
   but it enforces it at run time, on a push that has already happened. This says so
   before the commit. A `./local-action` path needs no pin: it is this repository's own
   code, already fixed by the commit under test.

Run it directly, or via tests/test_workflow_policy.py which also proves it bites:

    scripts/check_workflow_policy.py [WORKFLOW_DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Permission values that grant no more than read access.
READ_ONLY_VALUES = {"read", "none"}

#: The comment that makes a write grant deliberate rather than inherited.
JUSTIFICATION = re.compile(r"#\s*permissions-justification:\s*\S")

#: Ways a job pulls down something another workflow run produced.
ARTIFACT_CONSUMERS = re.compile(
    r"actions/download-artifact|listWorkflowRunArtifacts|"
    r"downloadArtifact|/actions/artifacts",
    re.IGNORECASE,
)

#: A full commit SHA, which is the only immutable way to name an action's code.
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

#: The container equivalent, for a `docker://` step.
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _triggers(document: dict) -> set[str]:
    """The workflow's `on:` keys.

    YAML 1.1 resolves a bare `on:` to the boolean True, which is why this looks
    for both spellings -- reading only `document["on"]` silently sees no triggers
    at all on a perfectly ordinary workflow file.
    """
    raw = document.get("on", document.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(key) for key in raw}
    return set()


def _grants_write(permissions) -> bool:
    """Does this `permissions:` block grant anything above read?"""
    if permissions is None:
        return False
    if isinstance(permissions, str):
        return permissions.strip().lower() not in READ_ONLY_VALUES
    if isinstance(permissions, dict):
        return any(
            str(value).strip().lower() not in READ_ONLY_VALUES
            for value in permissions.values()
        )
    return False


def _runners(document: dict) -> list[tuple[str, object]]:
    """(job name, runs-on value) for every job that declares one."""
    found = []
    for name, job in (document.get("jobs") or {}).items():
        if isinstance(job, dict) and "runs-on" in job:
            found.append((str(name), job["runs-on"]))
    return found


def _is_self_hosted(runs_on) -> bool:
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [str(item) for item in runs_on]
    elif isinstance(runs_on, dict):  # `runs-on: {group: ..., labels: [...]}`
        raw = runs_on.get("labels") or []
        labels = [str(raw)] if isinstance(raw, str) else [str(item) for item in raw]
        if runs_on.get("group"):
            labels.append(str(runs_on["group"]))
    else:
        return False
    return any("self-hosted" in label.lower() for label in labels)


def _action_references(document: dict) -> list[tuple[str, str]]:
    """(job name, `uses:` value) for every action and reusable workflow referenced.

    Both spellings count: a step's `uses:`, and a job-level `uses:` calling a
    reusable workflow. The second one runs a whole file someone else controls.
    """
    found: list[tuple[str, str]] = []
    for name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str):
            found.append((str(name), job["uses"]))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                found.append((str(name), step["uses"]))
    return found


def _is_pinned(uses: str) -> bool:
    """Is this reference immutable?

    A path beginning `./` is this repository's own code, already fixed by the
    commit under test. A container is pinned by image digest, everything else by
    the full 40-character commit SHA -- a short SHA is not enough, because it is
    a prefix, and a prefix can be collided with deliberately.
    """
    reference = uses.strip()
    if reference.startswith("./"):
        return True
    _, separator, revision = reference.partition("@")
    if not separator:
        return False
    if reference.startswith("docker://"):
        return bool(IMAGE_DIGEST.fullmatch(revision))
    return bool(COMMIT_SHA.fullmatch(revision))


def _jobs_without_justification(document: dict, text: str) -> list[str]:
    """Job names whose block carries no permissions-justification comment.

    Comments do not survive YAML parsing, so this reads the raw text: a job's
    block runs from its key line to the next key at the same indentation.
    """
    job_names = [str(name) for name in (document.get("jobs") or {})]
    lines = text.splitlines()

    starts: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z0-9_.\-]+):\s*(#.*)?$", line)
        if match and match.group(2) in job_names and match.group(1):
            starts.setdefault(match.group(2), index)

    missing = []
    for name in job_names:
        start = starts.get(name)
        if start is None:
            missing.append(name)
            continue
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                end = index
                break
        block = "\n".join(lines[start:end])
        if not JUSTIFICATION.search(block):
            missing.append(name)
    return missing


def check_workflow(path: Path) -> list[str]:
    """Every policy violation in one workflow file."""
    text = path.read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return [f"{path.name}: is not parseable YAML ({exc.__class__.__name__})"]
    if not isinstance(document, dict):
        return [f"{path.name}: top level is not a mapping"]

    violations: list[str] = []
    triggers = _triggers(document)

    for job, runs_on in _runners(document):
        if _is_self_hosted(runs_on):
            violations.append(
                f"{path.name}: job {job!r} uses a self-hosted runner. Public CI runs on "
                f"GitHub-hosted runners against synthetic fixtures; a self-hosted runner "
                f"is a machine that can reach the private financial corpus."
            )

    if "pull_request_target" in triggers:
        violations.append(
            f"{path.name}: triggers on pull_request_target, which runs with the base "
            f"repository's token and secrets against a fork's code. Use pull_request."
        )

    if "workflow_run" in triggers and ARTIFACT_CONSUMERS.search(text):
        violations.append(
            f"{path.name}: a workflow_run trigger that consumes artifacts executes a "
            f"fork's build output with the base repository's permissions. Do the work in "
            f"the untrusted pull_request run instead."
        )

    if _grants_write(document.get("permissions")):
        unjustified = _jobs_without_justification(document, text)
        if unjustified:
            violations.append(
                f"{path.name}: grants more than read at the top level, which applies to "
                f"every job including ones added later. Scope write to the job that needs "
                f"it, or add a '# permissions-justification: <why>' comment in each job "
                f"(missing on: {', '.join(sorted(unjustified))})."
            )

    for job, uses in _action_references(document):
        if not _is_pinned(uses):
            violations.append(
                f"{path.name}: job {job!r} references {uses!r}, which is not pinned to a "
                f"full commit SHA. A tag is mutable by whoever owns it, so a retargeted "
                f"tag runs new code in a job that has already checked this repository "
                f"out. Pin the 40-character SHA and name the version in a comment."
            )

    return violations


def check(workflow_dir: Path = WORKFLOW_DIR) -> list[str]:
    """Every violation across a workflow directory. No workflows means no violations."""
    if not workflow_dir.is_dir():
        return []
    violations: list[str] = []
    for path in sorted(workflow_dir.iterdir()):
        if path.suffix in (".yml", ".yaml") and path.is_file():
            violations.extend(check_workflow(path))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workflow_dir", nargs="?", type=Path, default=WORKFLOW_DIR)
    args = parser.parse_args(argv)

    violations = check(args.workflow_dir)
    if violations:
        print("workflow policy check FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print(f"OK: {args.workflow_dir} violates no workflow policy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
