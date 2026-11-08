# Единый реестр требований

Статус означает указанную проверку; функциональные PASS не означают экспертную точность. Обязательные внешние предпосылки и устная репетиция не подменены тестами.

| ID | Источник | Требование | Приоритет | Реализация | Проверка | Свидетельство | Статус |
|---|---|---|---|---|---|---|---|
| R01 | Guide p1–2; user history | Editable versioned criteria/types/scales/weights/NA | P0 | cqa/rubric.py;domain.py;store.py;web editor | Tests + real changed criterion | evidence/night-v4/gui-workflow.json; evidence/night-v4/custom-rule-protocol.json | PASS |
| R02 | Checklist p1 | 8 official criteria and exact IDs | P0 | rubric.py;exporting.py | Exact-ID tests + official export | evidence/night-v4/final/verification.json; tests/test_core.py; tests/test_http.py; tests/test_audit_v2.py; tests/test_quality_v3.py; tests/test_night_v4.py | PASS |
| R03 | Checklist p1 | Contact: language greeting and name in first operator answer | P0 | prompts.py;domain.py | Controlled greeting/name mutations | evidence/night-v4/metamorphic-report.json | PASS |
| R04 | Checklist p1/3 | Discovery: clarify missing essentials; NA if sufficient initially | P0 | prompts.py | Frozen synthetic targets, no human claim | evidence/quality-v3/benchmark-report.json | PASS |
| R05 | Checklist p1/3 | Identity confirmation before disclosure/protected action | P0 | prompts.py;domain.py | Role/time-order cases | evidence/quality-v3/benchmark-report.json | PASS |
| R06 | Checklist p1/2 | Consultation0/5/10/15/20; NA only before request | P0 | rubric.py;prompts.py | Discrete scales and15/20/0/5 boundaries | evidence/quality-v3/benchmark-report.json | PASS |
| R07 | Checklist p1 | Empathy: specific reaction when complaint expressed | P0 | prompts.py | Neutral/complaint pairs | evidence/quality-v3/benchmark-report.json | PASS |
| R08 | Checklist p1/2 | Dialogue0/4/7/10; first-response3min excluded | P0 | rubric.py;prompts.py | Reask/wait/incoherence cases | evidence/quality-v3/benchmark-report.json | PASS |
| R09 | Checklist p1/3 | Closure: summary/help plus goodbye; conditionalNA | P0 | prompts.py | Farewell-only and interruption cases | evidence/quality-v3/benchmark-report.json | PASS |
| R10 | Checklist p1/3 | Critical: insult/privatepaid/confidential; separate flag | P0 | prompts.py;quality.py | Attribution, quotes, injection, total unchanged | evidence/night-v4/metamorphic-report.json | PASS |
| R11 | Checklist p2 | Deterministic NA redistribution and2decimal rounding | P0 | domain.aggregate Decimal | Hand-computed totals andNA tests | evidence/night-v4/final/verification.json; tests/test_core.py; tests/test_http.py; tests/test_audit_v2.py; tests/test_quality_v3.py; tests/test_night_v4.py | PASS |
| R12 | Guide p2–3 | RU/KZ/EN; correct operator/bot/system/client roles | P0 | domain.py;providers | Actual language cases + wrong-speaker rejection | evidence/quality-v3/protocol.json; evidence/night-v4/local-pilot.json | PASS |
| R13 | Guide p3 | Preserve externalIDs/time and full-source1-based turn numbering | P0 | domain.py;exporting.py | Import/export bounds tests | evidence/night-v4/final/verification.json; tests/test_core.py; tests/test_http.py; tests/test_audit_v2.py; tests/test_quality_v3.py; tests/test_night_v4.py | PASS |
| R14 | User §4/10 | Evidence exact text/role; no invented source | P0 | materialize_evidence;validate_decisions | Invalid reference, duplicateJSON, quote tests | evidence/night-v4/browser-verification.json; evidence/night-v4/local-long.json | PASS |
| R15 | User §4/10 | Transcript instructions cannot override grading rules | P0 | prompts.py;schema | Real injection pairs | evidence/night-v4/metamorphic-report.json | PASS |
| R16 | Guide p2 | Appeal/comment/review without deleting original | P0 | store.py;server.py;UI | Real UI appeal + immutable-source test | evidence/night-v4/gui-workflow.json | PASS |
| R17 | Appendix p1–3 | Official DEV65/PROD10 exact set andresults paths | P0 | exporting.py;CLI | 65DEV check; PROD count fixture | docs/submission.md; docs/evaluation.md; evidence/night-v4/preservation.json | BLOCKED |
| R18 | Appendix p3 | Error is explicit status,error,emptycriteria,nullscore | P0 | providers;server;CLI | Unavailable/invalid/truncation tests | evidence/night-v4/final/verification.json; tests/test_core.py; tests/test_http.py; tests/test_audit_v2.py; tests/test_quality_v3.py; tests/test_night_v4.py | PASS |
| R19 | User §5 | Reproducible metrics preserve missing/error denominators | P0 | evaluate.py;quality_report.py | Harness regression and versioned reports | evidence/quality-v3/benchmark-report.json; evidence/quality-v3/paired-comparison.json | PASS |
| R20 | User §5 | No leakage between related cases; no tuning on holdout | P0 | frozen protocols andsource snapshots | Manifest/source/hash verification | evidence/quality-v3/protocol.json; evidence/quality-v3/candidate-freeze.json | PASS |
| R21 | User §5 | Human accuracy only on independent expert labels | P0 | diagnostics.human checks | Human GT unavailable | docs/submission.md; docs/evaluation.md; evidence/night-v4/preservation.json | BLOCKED |
| R22 | User history | At least30 own examples actually processed | P0 | quality-v3 protocol40 | v3 inventory40/40; new metamorphic cases | evidence/quality-v3/benchmark-report.json | PASS |
| R23 | User §6 | Useful innovations backed by actual evidence | P0 | typed rubric;impact map;blind recheck | UI differences andcontract tests | evidence/quality-v3/browser-restored-access.json; evidence/night-v4/gui-workflow.json | PASS |
| R24 | User §7 | Local trial max45min; hardware/license/resource/time measured | P0 | night-v4 local protocol | Pilot andsame-source cloud comparison | evidence/night-v4/local-decision.json; evidence/night-v4/local-pilot.json; evidence/night-v4/cloud-pilot.json; evidence/night-v4/local-long.json | PASS |
| R25 | User §7 | Cloud remains functional; no broken local option inUI | P0 | provider availability andsettings | Local unavailable/accepted UI checks | evidence/night-v4/browser-verification.json | PASS |
| R26 | User §8 | Freedom Bank style based on current official reference | P0 | web/styles.css andindex.html | bank-reference.json andscreenshots | evidence/night-v4/bank-reference.json; docs/night-v4/design.md | PASS |
| R27 | User §8 | Keep CQA identity; no implication of officialbank service | P0 | UI independent-project notice | Visual review | evidence/night-v4/ui-result-desktop.png | PASS |
| R28 | User §8/10 | Desktop/mobile,keyboard,contrast andallcorestates | P0 | web layout/dialog/status | Browser measured overflow/focus/contrast | evidence/night-v4/browser-verification.json; evidence/night-v4/ui-mobile-375.png | PASS |
| R29 | User §10 | New/changed/paraphrased transcripts affect only intended rules | P0 | night metamorphic cases | Real model outputs andfrozen targets | evidence/night-v4/metamorphic-report.json; evidence/night-v4/gui-workflow.json | PASS |
| R30 | User §10 | Invalid/empty/longinputs andprovider failures | P0 | domain.py;server.py;tests | Negative contract tests andUIerrors | evidence/night-v4/clean-launch.json; evidence/night-v4/browser-verification.json | PASS |
| R31 | User §11 | Clean launch +.env.example withoutsecrets | P0 | configuration;README;isolatedclone | Fresh-data startup andHTTPhealth | evidence/night-v4/clean-launch.json; .env.example | PASS |
| R32 | User §12 | Ordinarypush toauthorizedrepo; noforce/historyrewrite | P0 | origin=NuraliKuzhagaliev/data | RemoteSHA/readback/CI | evidence/night-v4/publication-preflight.json | NOT_RUN |
| R33 | User §12 | Exclude credentials/rawtranscripts/signedURLs/weights | P0 | gitignore andexplicit publication manifest | Content/privacy/secret scan + stagedtree | evidence/night-v4/publication-preflight.json | PASS |
| R34 | Guide p4 | Demo max5min; separate spoken vs technical verification | P0 | demo guide andexisting5slides | Technical UI trace; spoken rehearsal unavailable | docs/demo.md; evidence/night-v4/browser-verification.json | NOT_RUN |
| R35 | Guide p4/Appendix p1 | Control last hour includesprocessing andpush | P0 | submission instructions | RealPROD unavailable; static readinessonly | docs/submission.md; docs/evaluation.md; evidence/night-v4/preservation.json | BLOCKED |
| R36 | User §1/9 | Preserve userchanges,settings,failures andcurrentcontext | P0 | baseline andstate.json;AGENTS preserved | SHA andimmutable histories | evidence/night-v4/preservation.json | PASS |
| R37 | User §3/12 | CheckGitHub access beforefinalphase | P0 | GitHub connector metadata;ls-remote | Emptyrepo/admin/push verified | evidence/night-v4/baseline.json | PASS |
| R38 | User §6/13 | No winner/best-market/100%unqualifiedclaims | P0 | reports andREADME | Claim/evidence review | README.md; docs/evaluation.md | PASS |
