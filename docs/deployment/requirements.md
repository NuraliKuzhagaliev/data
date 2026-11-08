# Проверки размещения и подачи

Продолжение единого [реестра проекта](../night-v4/requirements.md). PASS относится к указанной области проверки; точность относительно экспертов отдельно BLOCKED.

| ID | Требование | Статус | Свидетельство |
|---|---|---|---|
| D01 | Use confirmed Render workspace and Free plan only | PASS | docs/deployment/render-service.json |
| D02 | Use only new Gemini/Groq keys from separate form; preserve main connection | PASS | .bootstrap/authorization-deploy.md; evidence/deployment/live-summary.json |
| D03 | Access code, isolated workspaces, CSRF/Origin, credential protection | PASS | tests/test_hosting.py; evidence/deployment/external-boundary.json |
| D04 | Live HTTPS service matches published application source | PASS | evidence/deployment/release.json |
| D05 | Real new Gemini/Groq calls; no prepared successful outputs | PASS | evidence/deployment/live-protocol.json; evidence/deployment/live-summary.json |
| D06 | Exact source links and deterministic score; real export/audit reopened | PASS | evidence/deployment/groq-live.json; evidence/deployment/gemini-live.json |
| D07 | Observed browser workflow and managed-key UI | PASS | evidence/deployment/browser-verification.json |
| D08 | Hosted workspace375/1440 override actually applied | NOT_RUN | evidence/deployment/browser-verification.json |
| D09 | Application source published, source CI all four environments | PASS | evidence/deployment/ci-source.json |
| D10 | Application text, judge guide, DEV and private access pack | PASS | submission/APPLICATION.md; submission/README.md; evidence/deployment/submission-manifest.json |
| D11 | Actual PROD10 and push before absolute stop code | BLOCKED | docs/submission.md |
| D12 | Accuracy against independent human GT | BLOCKED | docs/evaluation.md |
