"""Read-only discovery of loopback AI servers; prints model metadata only."""
import json
import urllib.request

for url in ("http://127.0.0.1:11434/api/tags", "http://127.0.0.1:1234/api/v1/models"):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.load(response)
        print(json.dumps({"url": url, "status": "AVAILABLE", "models": data}))
    except Exception as exc:
        print(json.dumps({"url": url, "status": "UNAVAILABLE", "type": type(exc).__name__}))
