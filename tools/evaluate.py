import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cqa.metrics import diagnostics

parser = argparse.ArgumentParser()
parser.add_argument("--predictions", required=True)
parser.add_argument("--truth", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--tolerance", type=float, default=1)
args = parser.parse_args()
read = lambda p: json.loads(Path(p).read_text(encoding="utf-8-sig"))
result = diagnostics(read(args.predictions), read(args.truth), args.tolerance)
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"output": args.output, "label_source": result["label_source"], "runs": len(result["runs"])}))
