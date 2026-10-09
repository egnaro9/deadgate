"""D3's narrowing: can the masked exit status reach anything at all?

Built against a COMPLETE hand-labelled census of all 125 D3 HIGH findings on the 275-repo
corpus, which measured 51.2% false. These rules remove 35 of the 64 false findings and
lose ZERO of the 61 true ones, taking HIGH to about 35% false.

Every rule here was evaluated against those labels BEFORE being written into the detector,
and three versions were discarded because the evaluation showed they cost true positives:

  1. "only printed" matched `echo "digest=${d}" >> "$GITHUB_OUTPUT"`, which is the single
     most important true shape in the corpus. It would have suppressed 11 true findings.
  2. "output goes nowhere" missed `export V=$(...)` and `for f in $(...)` as consumption,
     losing 2.
  3. The assignment pattern matched an inline ENV PREFIX: in
     `WINEDEBUG=-all timeout 300 make check | tee x.log` it read WINEDEBUG as the captured
     variable, found it unused, and suppressed a masked test failure. Losing 2. This one
     survived the simulation and was caught only by running the real detector against the
     labels, because the simulation applied the assignment check to fewer lines.

The bash semantics below were established by RUNNING bash, not by reasoning about it.
"""
import textwrap

import yaml

from deadgate.detectors import _masked_status_can_matter, scan_workflow


def _high(run: str) -> list:
    doc = yaml.safe_load(textwrap.dedent(f"""
        on: [pull_request]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - name: step
                run: |
{textwrap.indent(run.strip(), " " * 18)}
        """))
    return [f for f in scan_workflow(doc) if f.detector == "D3" and f.severity == "HIGH"]


# --- the shapes that must STAY high -----------------------------------------

def test_a_masked_value_reaching_github_output_stays_high():
    assert _high('echo "digest=$(sha256sum f | cut -d\' \' -f1)" >> "$GITHUB_OUTPUT"')


def test_an_echo_that_redirects_is_exporting_not_printing():
    """Guards discarded rule 1. An echo with a redirect is not print-only."""
    run = 'd=$(docker inspect x | jq -r .digest)\necho "digest=${d}" >> "$GITHUB_OUTPUT"\nexit 0'
    assert _high(run), "a value echoed INTO $GITHUB_OUTPUT escapes the step"


def test_an_inline_env_prefix_is_not_a_capture():
    """Guards discarded rule 3, the one the simulation missed.

    WINEDEBUG= here sets an environment variable for the command; it does not capture the
    pipeline. Reading it as a capture made it look unused and suppressed a masked test
    failure.
    """
    # exit 1, not exit 0: `decides` looks for a NON-ZERO exit, so a fixture ending in
    # `exit 0` is MEDIUM for an unrelated reason and would pass this test vacuously.
    assert _high('WINEDEBUG=-all timeout 300 make check | tee win32_input.log\nexit 1')


def test_a_test_command_piped_into_tee_stays_high():
    assert _high('uv run pytest -q tests/ | tee result.txt\nexit 1')


def test_a_substitution_feeding_a_loop_counts_as_consumption():
    """Guards discarded rule 2."""
    assert _high('for f in $(cat built.txt | sed "s/x/y/"); do echo "$f"; done\nexit 1')


def test_an_if_with_a_numeric_test_stays_high():
    """`set -e` is SUSPENDED inside an if-condition. Verified by running bash:

        $ SIZE=""; set -e; if [ "$SIZE" -gt 100 ]; then echo big; else echo ok; fi; echo $?
        [: : integer expression expected
        ok
        0

    The size check passes silently on an empty value, so this is a real finding. The first
    census pass labelled this shape FALSE by reasoning that -e would catch it, and that
    cost two labels.
    """
    assert _high('SIZE=$(du -sk dist | cut -f1)\nif [ "$SIZE" -gt 100 ]; then exit 1; fi')


# --- the shapes that must NOT be high ---------------------------------------

def test_a_pipeline_whose_output_goes_nowhere_is_not_high():
    """`_d3_severity` reads `exit 1` from ANYWHERE in the step, so one exit at the bottom
    used to promote every log-extraction pipeline above it."""
    assert not _high('grep -E "warn" logs/install.log | tail -60\nexit 1')


def test_a_captured_value_never_used_again_is_not_high():
    assert not _high('DMG="$(ls dl/*.dmg | head -1)"\nexit 1')


def test_a_captured_value_that_is_only_printed_is_not_high():
    assert not _high('SCORE=$(cat r.json | jq -r .score)\necho "score: $SCORE"\nexit 1')


def test_a_bare_test_statement_fails_loudly_so_is_not_high():
    """Verified by running bash: a bare `test x = y` that fails exits the step under -e,
    so an empty captured value surfaces rather than passing silently."""
    assert not _high('actual="$(sha256sum f | cut -d\' \' -f1)"\ntest "$actual" = "$expected"')


def test_a_default_applied_at_the_use_site_is_not_high():
    assert not _high('words="$(wc -w < f | tr -d \' \')"\nif [ "${words:-0}" -gt 300 ]; then exit 1; fi')


# --- the predicate itself ---------------------------------------------------

def test_predicate_rejects_a_line_that_consumes_nothing():
    assert not _masked_status_can_matter("ls -la /tmp | head -10", "ls -la /tmp | head -10")


def test_predicate_accepts_a_redirect():
    assert _masked_status_can_matter("cmd | jq . > out.json", "cmd | jq . > out.json")


# ---------------------------------------------------------------------------
# Two bugs found while censusing D3's HIGH stratum.
#
# BUG 1: the whole line was split on its LAST pipe, so only the outermost pipeline was
# ever judged. In
#     echo "batch=$(grep -Po '...' list.txt | python3 -c '...')" | tee "$GITHUB_OUTPUT"
# that is `echo ... | tee`, whose head cannot fail. `_upstream_can_fail` then returned
# True anyway on the grounds that "the outer head holds a substitution we already judged
# benign above" -- which it had not: that loop returns False only for benign
# substitutions and falls through for every other kind. Four of the 125 census findings
# were this, all false.
#
# BUG 2: `break` after the first finding per step. Blamed for hiding a real inner
# pipeline behind a benign outer one, but that was bug 1 choosing the outer. With
# candidates ordered innermost-first and each judged against its own head, the first
# qualifying pipeline is the guilty one. Removing the break as well was tried and
# measured: it added roughly 31 unlabelled findings to the loudest tier, so it was put
# back.

from deadgate.detectors import _pipeline_candidates, _segment_head_can_fail, _strip_substitutions


def test_candidates_include_the_pipeline_inside_a_substitution():
    line = 'echo "batch=$(grep -Po \'x\' list.txt | python3 -c \'y\')" | tee "$GITHUB_OUTPUT"'
    segs = [s for s, _ in _pipeline_candidates(line)]
    assert any("grep" in s and "python3" in s for s in segs), segs
    assert any("tee" in s and "grep" not in s for s in segs), "the outer pipeline too"


def test_the_inner_candidate_comes_first():
    """Order matters, because one finding is reported per step. Innermost first means the
    guilty pipeline is the one reported."""
    line = 'x="$(sha256sum f | cut -d\' \' -f1)" | tee log'
    segs = [s for s, _ in _pipeline_candidates(line)]
    assert "sha256sum" in segs[0], segs


def test_strip_substitutions_leaves_the_outer_command():
    assert _strip_substitutions('echo "a=$(cmd | filt)" | tee f').strip() == 'echo "a=" | tee f'


def test_an_echo_head_holding_only_benign_substitutions_cannot_fail():
    """OSGeo/grass. `uname` cannot meaningfully fail and neither can echo, so the awk
    pipeline masks nothing. The old guard refused to call any head benign once it held a
    substitution, which is what reported this."""
    assert not _segment_head_can_fail('echo "$(uname -s)"-"$(uname -m)" | awk \'{print tolower($0)}\'')


def test_an_echo_head_holding_a_FAILING_substitution_can_fail():
    """The other direction, so the fix cannot become a blanket mute."""
    assert _segment_head_can_fail('echo "$(curl -s https://x | jq -r .v)" | tee f')


def test_the_four_census_false_positives_are_gone():
    """Each of these was a D3 HIGH in the census and each was hand-labelled false."""
    for line in (
        'arch="$(echo "$(uname -s)"-"$(uname -m)" | awk \'{print tolower($0)}\')"',
        "printf 'New java files: %s' \"$new_java\" | tee \"$GITHUB_STEP_SUMMARY\"",
    ):
        assert not _high(line + "\nexit 1"), line


def test_a_step_still_reports_only_one_finding():
    """The break stays. Two masked pipelines in one step report once, not twice."""
    run = 'a=$(curl -s u1 | jq -r .x)\nb=$(curl -s u2 | jq -r .y)\necho "$a$b" >> "$GITHUB_OUTPUT"'
    assert len(_high(run)) == 1
