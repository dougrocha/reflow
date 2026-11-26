#!/usr/bin/env bash
# Verify that a pooled conversion (--jobs N) gives what --jobs 1 gives. See README.md.
#
#   run.sh            the gate (must pass) and the open findings, if any (must still reproduce)
#   run.sh selftest   also show each model property failing on its broken variant
#
# Exit 0: the gate passed and every open finding (the FINDINGS list) reproduced as recorded.
# Needs: mise (Java from the repo's mise.toml) and the repo's dev Python (hypothesis).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
COMMON="$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)"
PY="${REFLOW_PYTHON:-$(dirname "$COMMON")/.venv/bin/python}"   # the main checkout's venv
N="${TLA_N:-2}"

TLA_VERSION=1.7.4
TLA_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR="${XDG_CACHE_HOME:-$HOME/.cache}/reflow/tla/tla2tools-$TLA_VERSION.jar"
if [[ ! -f "$JAR" ]]; then
    mkdir -p "$(dirname "$JAR")"
    curl -fsSL -o "$JAR.part" \
        "https://github.com/tlaplus/tlaplus/releases/download/v$TLA_VERSION/tla2tools.jar"
    mv "$JAR.part" "$JAR"
fi
echo "$TLA_SHA256  $JAR" | sha256sum -c --quiet - || { echo "tla2tools.jar checksum mismatch: $JAR"; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/reflow_verify_XXXXXX")"   # no dot: TLC 1.7.4 cuts -config at the first one
if [[ -n "${KEEP_OUTPUT:-}" ]]; then echo "tool output kept in $WORK"; else trap 'rm -rf "$WORK"' EXIT; fi
fail=0
# Open findings, "ID INVARIANT TEST" each: a property the code is known to break. Empty when
# every property is in the gate. See README.md, "Findings".
FINDINGS=()
export HYPOTHESIS_STORAGE_DIRECTORY="$WORK/hypothesis"   # keep .hypothesis/ out of the repo

# tlc NAME BREAK INVARIANT... : run TLC; output in $WORK/NAME.out; status 0 = no error.
tlc() {
    local name=$1 brk=$2; shift 2
    {
        echo "SPECIFICATION Spec"
        echo "CONSTANTS N = $N  Break = \"$brk\""
        for inv in "$@"; do echo "INVARIANT $inv"; done
        echo "CHECK_DEADLOCK TRUE"
    } > "$WORK/$name.cfg"
    # Run from the model's folder with a relative spec name: TLC 1.7.4 does not find the
    # -config file when the spec is given by an absolute path. mise reads the repo's mise.toml
    # from any folder below it.
    (cd "$HERE/model" && mise exec -- java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC \
        -workers auto -metadir "$WORK/states_$name" -config "$WORK/$name.cfg" \
        ParallelConvert.tla) > "$WORK/$name.out" 2>&1
}


# tlc_result NAME INV : "pass", "violated" (TLC reported INV violated) or "error" (anything else).
tlc_result() {
    local out="$WORK/$1.out"
    if grep -q "^Model checking completed. No error has been found." "$out"; then echo pass
    elif [[ -n "$2" ]] && grep -q "^Error: Invariant $2 is violated" "$out"; then echo violated
    else echo error; fi
}

# The counterexample in short: the document TLC chose, then the steps (action names).
trace() {
    awk '/^State 1:/ {s=1}
         /^State 2:/ {s=2}
         s==1 && /^\/\\ (seeded|bad|linkPages|tagPages|tagged|overCap|pooledPlan|scanRaises|walkRaises|failAt|sharedPool) =/ {doc = doc " " substr($0, 4) ";"}
         /^State [0-9]+: </ && !/^State 1:/ {split($0, a, /[<> ]+/); steps = steps (n++ ? " -> " : "") a[3]}
         END {print "    document:" doc; print "    steps:   " steps}' "$1"
}

tool_error() {
    echo "ERROR: $1 did not run to a verdict; its output:"
    grep -v "^Computed" "$2" | tail -15 | sed 's/^/    /'
    fail=1
}

# pyt SELECTOR : the real-code checks; output in $WORK/pytest_<n>.out, path in $PYOUT.
pyt_n=0
pyt() {
    pyt_n=$((pyt_n + 1)); PYOUT="$WORK/pytest_$pyt_n.out"
    (cd "$REPO" && timeout 900 "$PY" -m pytest -o pythonpath=src -o faulthandler_timeout=300 -q -p no:cacheprovider -p no:logging \
        "$HERE/checks/test_real_code.py" -k "$1") > "$PYOUT" 2>&1
}

echo "== gate: model (TLC $TLA_VERSION, N = $N)"
tlc gate none TypeOK SameAsSerial SameOrderAndImages F1SameFailureAndLinks \
    F2SameTagEvidence ReaderOneCaller StageQuiet PublishedWhole WitnessJoined || true
case "$(tlc_result gate "")" in
    pass) grep -E "^[0-9]+ states generated|^Finished in" "$WORK/gate.out" | sed 's/^/    /' ;;
    *)  if grep -qE "^Error: (Invariant .* is violated|Deadlock reached)" "$WORK/gate.out"; then
            echo "FAIL: the model breaks a property the code must keep:"
            grep -E "^Error: (Invariant|Deadlock)" "$WORK/gate.out" | sed 's/^/    /'
            trace "$WORK/gate.out"
            fail=1
        else tool_error TLC "$WORK/gate.out"; fi ;;
esac

echo "== gate: real code (hypothesis)"
if pyt "not finding"; then
    tail -1 "$PYOUT" | sed 's/^/    /'
elif grep -q "Failing test case" "$PYOUT"; then
    echo "FAIL: the real code breaks a property it must keep:"
    grep -E "^FAILED|^E  " "$PYOUT" | head -20 | sed 's/^/    /'
    fail=1
else tool_error pytest "$PYOUT"; fi

# Open findings: each must still fail, in the model and in the real code. When one no
# longer does, the code was fixed (or the check broke): see README.md, "Findings".
for f in "${FINDINGS[@]}"; do
    read -r id inv test <<< "$f"
    echo "== open finding $id"
    tlc "$id" none "$inv" || true
    case "$(tlc_result "$id" "$inv")" in
        violated) echo "  model ($inv violated):"; trace "$WORK/$id.out" ;;
        pass) echo "CHANGED: $id no longer fails in the model ($inv holds)."; fail=1 ;;
        *) tool_error TLC "$WORK/$id.out" ;;
    esac
    if pyt "$test"; then
        echo "CHANGED: $id no longer fails in the real code ($test passes)."
        fail=1
    elif grep -q "Failing test case" "$PYOUT"; then
        echo "  real code ($test):"
        grep -A1 "Failing test case" "$PYOUT" | grep "d=" | head -1 | sed 's/^E *d=/    /'
        grep -E "^E +(AssertionError|assert)" "$PYOUT" | head -1 | sed 's/^E */    /'
    else tool_error pytest "$PYOUT"; fi
done

if [[ "${1:-}" == "selftest" ]]; then
    echo "== selftest: each model property fails on its broken variant"
    for b in "extAsCompleted SameOrderAndImages" "noFinish ReaderOneCaller" \
             "noCancelWait StageQuiet" "noAbandon StageQuiet" "noImageFinish PublishedWhole" "restoreNoImages PublishedWhole" \
             "triedFirst F1SameFailureAndLinks" "triedFirst F2SameTagEvidence" \
             "witAsCompleted SameOrderAndImages" "noWitJoin WitnessJoined"; do
        read -r brk inv <<< "$b"
        tlc "self_${brk}_$inv" "$brk" "$inv" || true
        case "$(tlc_result "self_${brk}_$inv" "$inv")" in
            violated) echo "    Break=$brk violates $inv, as it must" ;;
            pass) echo "SELFTEST FAIL: Break=$brk does not violate $inv"; fail=1 ;;
            *) tool_error TLC "$WORK/self_${brk}_$inv.out" ;;
        esac
    done
fi

if [[ $fail -eq 0 ]]; then
    if [[ ${#FINDINGS[@]} -eq 0 ]]; then echo "OK: the gate passed; no open findings"
    else echo "OK: the gate passed; the open findings reproduce as recorded"; fi
fi
exit $fail
