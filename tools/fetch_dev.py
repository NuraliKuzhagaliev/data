"""Read the signed DEV link from the supplied PDF; never log or persist it."""
import argparse
import json
import sys
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from collections import Counter
from pathlib import Path


def main():
    from pypdf import PdfReader
    parser = argparse.ArgumentParser()
    parser.add_argument("--guide", required=True)
    parser.add_argument("--output")
    parser.add_argument("--group", default="DEV")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    reader = PdfReader(args.guide)
    links = [str(ref.get_object().get("/A", {}).get("/URI", ""))
             for page in reader.pages for ref in page.get("/Annots", [])]
    url = next(link for link in links if "group_id=" in link)
    parsed = urlsplit(url)
    query = [(k, args.group if k == "group_id" else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    try:
        with urllib.request.urlopen(url, timeout=40) as response:
            payload = json.loads(response.read(12_000_001))
        if payload.get("isSuccess") is not True:
            raise ValueError("API did not report success")
        records = payload["data"]["records"]
        if args.expected_count is not None and len(records) != args.expected_count:
            raise ValueError("Unexpected record count")
        if len({r["conversation_id"] for r in records}) != len(records):
            raise ValueError("Duplicate source IDs")
        target = Path(args.output or (".local/dev.json" if args.group == "DEV" else ".local/prod.json"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "PASS", "records": len(records),
            "languages": dict(Counter(r.get("language") for r in records)),
            "channels": dict(Counter(r.get("channel") for r in records)),
            "record_keys": list(records[0]) if records else [],
            "message_keys": list(records[0]["transcript"][0]) if records else [],
            "output": str(target)}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "code", None)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
