"""Isolated UI recovery rehearsal. One explicitly simulated failure, real AI otherwise.

Never use this fixture server to process organizer submissions.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa.server import Application, create_server
from cqa.providers import ProviderError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate-outage", action="store_true", required=True)
    args = parser.parse_args()
    app = Application(data_dir=ROOT/".local/audit-v2/ui", config_path=ROOT/".local/provider.json")
    original = app.provider
    state = {"failed_once": any(r.get("provenance", {}).get("error_code") == "diagnostic_simulated_outage" for r in app.store._list("failed_attempts"))}
    class DiagnosticProvider:
        def __init__(self, real): self.real = real
        def evaluate(self, rubric, conversation, mode="evidence"):
            if conversation["conversation_id"] == "ui-audit-pending" and not state["failed_once"]:
                state["failed_once"] = True
                raise ProviderError("ДИАГНОСТИКА: однократно смоделирован сбой провайдера; это не реальный отказ Groq", code="diagnostic_simulated_outage")
            return self.real.evaluate(rubric, conversation, mode)
    app.provider = lambda kind, model: DiagnosticProvider(original(kind, model))
    server = create_server(app, port=8766)
    print("DIAGNOSTIC ONLY: http://127.0.0.1:8766/; simulated outage for ui-audit-pending; real free API otherwise", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); app.store.close()


if __name__ == "__main__": main()
