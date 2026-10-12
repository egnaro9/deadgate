"""D3 must read pipefail off the shell line, not only out of the run: body.

Found 2026-10-12 by running deadgate across the author's own repositories: it flagged a
step in agent-graph whose `shell: bash -eo pipefail {0}` line carried a comment explaining
that it was there precisely to stop tee masking the CLI's exit status. A detector that
cannot see the fix it recommends reports the fixed step forever.
"""

import textwrap

import yaml

from deadgate.detectors import scan_workflow

PIPE = 'python -m tool run | tee run.out'


def _d3(workflow: str) -> list:
    doc = yaml.safe_load(textwrap.dedent(workflow))
    return [f for f in scan_workflow(doc) if f.detector == "D3"]


def test_pipefail_on_the_step_shell_line_is_honoured():
    assert not _d3(f"""
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - name: smoke
                shell: bash -eo pipefail {{0}}
                run: {PIPE}
        """)


def test_pipefail_in_the_job_defaults_is_honoured():
    assert not _d3(f"""
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            defaults:
              run:
                shell: bash -euo pipefail {{0}}
            steps:
              - name: smoke
                run: {PIPE}
        """)


def test_pipefail_in_the_workflow_defaults_is_honoured():
    assert not _d3(f"""
        on: [push]
        defaults:
          run:
            shell: bash -o pipefail -e {{0}}
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - name: smoke
                run: {PIPE}
        """)


def test_a_bare_bash_shell_line_does_not_count():
    """`shell: bash` is still `bash -e {0}`: no pipefail, so the finding stands."""
    assert _d3(f"""
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - name: smoke
                shell: bash
                run: {PIPE}
        """)


def test_a_step_shell_without_pipefail_is_not_rescued_by_a_job_default_with_it():
    """The step's own shell line wins over the job default, as it does on GitHub."""
    assert _d3(f"""
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            defaults:
              run:
                shell: bash -eo pipefail {{0}}
            steps:
              - name: smoke
                shell: bash
                run: {PIPE}
        """)
