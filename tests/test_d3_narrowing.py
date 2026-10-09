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

from deadgate.detectors import scan_workflow


def _high(run: str) -> list:
    """Historically "is this HIGH". With the tiers collapsed it is "is this reported".

    The severity model was removed after all three D3 strata were censused: HIGH 30.9%
    false, LOW 31.6%, MEDIUM 55.6%. HIGH's interval overlapped the merged non-HIGH tier,
    LOW was indistinguishable from HIGH, and the one real separation was MEDIUM being
    WORSE, an artefact of severity short-circuiting on the workflow's trigger. So these
    tests now assert presence, not tier."""
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
    return [f for f in scan_workflow(doc) if f.detector == "D3"]


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











# --- the predicate itself ---------------------------------------------------





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


# ---------------------------------------------------------------------------
# Shell quoting in the substitution scanner.
#
# The scanner counted `$(` and `)` with no idea of quotes or escapes. An escaped paren
# inside a quoted regex closed the substitution early:
#
#   version=$(grep -o 'LLVM_VERSION_\(MAJOR\|MINOR\|PATCH\) [0-9]\+' f | cut -d ' ' -f 2)
#
# gave a body of `grep -o 'LLVM_VERSION_\(MAJOR\|MINOR\|PATCH\` and an outer remainder of
# `version= [0-9]\+' f | cut -d ' ' -f 2)`, so the reported FILTER was `PATCH\`. That was
# survivable while the scan only produced a benign/not-benign hint, and stopped being
# survivable in 0.1.8, which began reporting the segment itself.
#
# Found by reading the reported segments of real findings, not by a test.

from deadgate.detectors import _substitution_bodies, _substitution_spans

LLVM = r"""version=$(grep -o 'LLVM_VERSION_\(MAJOR\|MINOR\|PATCH\) [0-9]\+' f | cut -d ' ' -f 2)"""


def test_an_escaped_paren_in_a_quoted_regex_does_not_close_the_substitution():
    bodies = _substitution_bodies(LLVM)
    assert len(bodies) == 1, bodies
    assert bodies[0].startswith("grep -o") and bodies[0].endswith("-f 2"), bodies[0]


def test_the_outer_text_is_what_is_left_after_the_whole_substitution():
    assert _strip_substitutions(LLVM).strip() == "version="


def test_the_reported_filter_is_the_real_one_not_regex_debris():
    filters = [f for _, f in _pipeline_candidates(LLVM)]
    assert filters == ["cut"], filters


def test_single_quotes_make_everything_literal():
    """No escape processing inside single quotes, so a lone backslash is just a character."""
    assert _substitution_bodies(r"""echo '$(not a substitution)'""") == []


def test_a_substitution_inside_double_quotes_is_still_found():
    assert _substitution_bodies('echo "$(date | cut -c1-3)"') == ["date | cut -c1-3"]


def test_nested_substitutions_come_innermost_first():
    bodies = _substitution_bodies('a=$(echo "$(uname -s)" | tr a-z A-Z)')
    assert bodies[0] == "uname -s", bodies


def test_a_single_quote_inside_double_quotes_is_not_a_quote_opener():
    """An apostrophe in ordinary prose must not swallow the rest of the line.

    Without double-quote tracking the `'` in "it's" starts single-quote mode and the
    substitution after it is never seen.
    """
    assert _substitution_bodies("""echo "it's $(date | cut -c1-3)" """) == ["date | cut -c1-3"]


def test_strip_removes_whole_substitutions_not_nested_pieces():
    """Only TOP-LEVEL spans are removed. Removing nested ones separately would delete
    the same characters twice and leave fragments of the outer command behind."""
    assert _strip_substitutions('a=$(echo "$(uname -s)" | tr a-z A-Z) | tee f').strip() == "a= | tee f"


def test_an_escaped_dollar_does_not_open_a_substitution():
    r"""`\$(` is a literal dollar followed by a paren, not a command substitution.

    This is what the backslash branch of the scanner is for. The escaped-quote case does
    NOT discriminate, because `$(` is recognised inside double quotes anyway, so dropping
    escape handling changes nothing there: a mutation removing the branch survived against
    that test and only died against this one.
    """
    assert _substitution_bodies(r"""echo \$(ls | head -1)""") == []
    # and the unescaped form still is one
    assert _substitution_bodies("""echo $(ls | head -1)""") == ["ls | head -1"]


def test_a_substitution_body_is_parsed_fresh():
    r"""Inside double quotes a single quote is literal, but inside `$( ... )` nested in
    double quotes it quotes again, because the body is parsed as new shell code.

    Treating the body as still double-quoted let the `)` in a quoted sed expression close
    the span early, reporting `touched="/.*#\1#p' f | sort -u)"` as the segment.
    """
    line = """touched="$(sed -nE 's#^(packages/[^/]+)/.*#\\1#p' f | sort -u)" """
    assert _strip_substitutions(line).strip() == 'touched=""'


def test_process_substitution_parens_are_matched_too():
    r"""`<( ... )` takes a paren. Ignoring it let its closing paren close the enclosing
    `$(`, losing everything after the first one."""
    line = """count_new_js=$(comm -1 -2 <(git diff --name-only a b) <(gh pr view 1 --json files | jq -r '.f'))"""
    assert _strip_substitutions(line).strip() == "count_new_js="


def test_a_process_substitution_is_not_reported_as_a_command_substitution():
    """Only `$( )` bodies are returned; `<( )` is matched for paren balance only."""
    bodies = _substitution_bodies("diff <(sort a | uniq) <(sort b)")
    assert bodies == [], bodies


# ---------------------------------------------------------------------------
# `cmd | grep -q PATTERN` is a content assertion, not a masked status.
#
# Demonstrable rather than likely. When the head fails it emits nothing, grep matches
# nothing and exits 1, so the pipeline fails exactly when the head does:
#
#     $ nosuchcommand 2>/dev/null | grep -q arm64 ; echo $?
#     1
#
# All 6 findings of this shape in D3's 153-finding MEDIUM census were false, as were the
# ones in the HIGH census, and no labelled true finding has it.
#
# This also corrected the corpus. b3_pipe_masked_exit, the canonical D3 broken fixture,
# was `curl -s https://example.test/health | grep -q ok`, which is NOT a D3 defect: the
# finding's own text says "a failure upstream of the pipe passes" and that is false here.
# The real hazard in that line is `curl -s` without `-f`, which does not fail on HTTP 500.
# The fixture now carries a genuine masking and the grep -q form moved to corpus/good/.

from deadgate.detectors import _tail_is_a_content_assertion


def test_a_grep_q_tail_is_an_assertion():
    for seg in ("file ./build/cli | grep -q arm64",
                "nm retroarch | grep -q -- '-lpulse'",
                "cmd | grep -sq pattern",
                "cmd | grep --quiet pattern"):
        assert _tail_is_a_content_assertion(seg), seg


def test_a_plain_grep_tail_is_not_an_assertion():
    """Without -q the output is the point, so the masked status still matters."""
    for seg in ("cmd | grep pattern", "cmd | grep -E 'a|b'", "cmd | grep -v skip"):
        assert not _tail_is_a_content_assertion(seg), seg


def test_a_grep_q_pipeline_is_not_reported():
    assert not _high("file ./build/cli | grep -q arm64\nexit 1")


def test_the_same_pipeline_without_q_is_still_reported():
    """The counter-case, so this cannot become a blanket mute on grep."""
    assert _high('v=$(file ./build/cli | grep arm64)\necho "v=$v" >> "$GITHUB_OUTPUT"\nexit 1')
