#!/usr/bin/env python3
# The crossing check's stand-in for `reflow convert-many` and `reflow audit` (crossing-mark.ts). It is
# instant, and the page says which publication it came from: audit n writes "crossing fixture audit n".
# Conversion copies $CROSSING_FIXTURE/template/<slug> unchanged. Every call is logged to calls.log, so
# the check can tell that this converter ran and not a real one.
import json, os, shutil, sys, time

from reflow.progress_lines import OK, START, status_line   # PYTHONPATH is the checkout's src (childEnv)

R = os.environ["CROSSING_FIXTURE"]


def bump(name):
    p = os.path.join(R, name)
    n = int(open(p).read()) + 1 if os.path.exists(p) else 1
    open(p, "w").write(str(n))
    return n


with open(os.path.join(R, "calls.log"), "a") as log:
    log.write(json.dumps({"t": time.time(), "pid": os.getpid(), "argv": sys.argv[1:3]}) + "\n")
if sys.argv[1] == "audit":
    n = bump("audit.count")
    print(json.dumps({"filename": os.path.basename(sys.argv[2]), "passed": False, "findings": [
        {"id": "CROSSING-FIXTURE", "title": f"crossing fixture audit {n}", "wcag": "1.3.1", "status": "fail",
         "detail": "written by the crossing fixture", "pages": []}]}), flush=True)
    sys.exit(1)
assert sys.argv[1] == "convert-many"
args = sys.argv[2:sys.argv.index("--max-docs")]
outs = [args[i + 1] for i in range(0, len(args), 2)]
bump("convert.count")
for out in outs:
    print(status_line(START, out, ""), flush=True)
for out in outs:
    tmp = out + ".crossingtmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(os.path.join(R, "template", os.path.basename(out)), tmp)
    shutil.rmtree(out, ignore_errors=True)
    os.rename(tmp, out)
    print(status_line(OK, out, ""), flush=True)
