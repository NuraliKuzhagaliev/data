"""Run bounded reproducible checks; keep model and browser evidence separate."""
import hashlib
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", default="evidence")
    evidence = (ROOT / parser.parse_args().evidence_dir).resolve()
    if not evidence.is_relative_to(ROOT):
        parser.error("Evidence directory must stay inside the project")
    evidence.mkdir(parents=True, exist_ok=True)
    checks = []
    commands = [("python_compile", [sys.executable,"-m","compileall","-q","cqa","tools"]),
                ("unit_http", [sys.executable,"-m","unittest","discover","-s","tests","-v"])]
    node = shutil.which("node")
    if node:
        commands.append(("javascript_syntax", [node,"--check","web/app.js"]))
    else:
        checks.append({"name":"javascript_syntax","status":"NOT_RUN","reason":"node unavailable"})
    helper = Path.home()/".agents/skills/project-bootstrap/scripts/bootstrap.py"
    if helper.exists():
        commands.append(("bootstrap_structure_only", [sys.executable,str(helper),"verify","--project",str(ROOT)]))
    else:
        checks.append({"name":"bootstrap_structure_only","status":"NOT_RUN","reason":"installed helper unavailable"})
    for name, command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        log = evidence/(name+".log")
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        checks.append({"name":name,"status":"PASS" if result.returncode==0 else "FAIL","exit_code":result.returncode,"command":command,"evidence":str(log.relative_to(ROOT))})
    paths = sorted([*ROOT.glob("cqa/*.py"),*ROOT.glob("web/*"),*ROOT.glob("tests/*.py")])
    report = {"checked_at":datetime.now(timezone.utc).isoformat(),"checks":checks,
              "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()},
              "scope":"CODE_AND_CONTRACTS_ONLY; live AI, human accuracy and browser runs have separate evidence"}
    (evidence/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"checks":[{"name":x["name"],"status":x["status"]} for x in checks],"report":str((evidence/"verification.json").relative_to(ROOT))}))
    return 1 if any(c["status"]=="FAIL" for c in checks) else 0


if __name__ == "__main__": raise SystemExit(main())
