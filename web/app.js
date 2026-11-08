"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="cqa-token"]').content;
const state = {conversations: [], evaluations: [], rubrics: [], selected: null, conversation: null, evaluation: null, config: {}, providerOptions: {options:[]}, job: null, busy: false};
const roles = {operator: "ОПЕРАТОР", client: "КЛИЕНТ", bot: "БОТ", system: "СОБЫТИЕ"};
const answerText = x => x === "YES" ? "Да" : x === "NO" ? "Нет" : x;
let noticeTimer, appealCriterion, editorRubric;

function node(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "text") element.textContent = value;
    else if (key in element) element[key] = value;
    else element.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function toast(message, error = false) {
  const dialog = document.querySelector("dialog[open]");
  if (error && dialog) {
    let alert = dialog.querySelector("[data-dialog-error]");
    if (!alert) { alert = node("div", {className:"form-error",role:"alert","data-dialog-error":"true"}); dialog.querySelector(".dialog-actions").before(alert); }
    alert.textContent = message; alert.scrollIntoView({block:"nearest"}); $("notice").hidden = true; return;
  }
  clearTimeout(noticeTimer); $("notice").textContent = message; $("notice").className = error ? "error" : ""; $("notice").hidden = false;
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, error ? 15000 : 5000);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-CQA-Token": token}, body: JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function action(fn) { return async event => { try { await fn(event); } catch (error) { toast(error.message, true); } }; }
function openDialog(id) { $(id).querySelectorAll("[data-dialog-error]").forEach(e=>e.remove()); $(id).showModal(); }
function closeDialog(id) { $(id).close(); }
function score(value) { return value === null || value === undefined ? "—" : value.toLocaleString("ru-RU", {maximumFractionDigits: 2}); }
function rubric() { return state.rubrics.find(x => x.id === $("rubric-select").value) || state.rubrics[0]; }
function groupOf(c) { return c.dataset_group_id || (c.source === "synthetic" ? "SYNTHETIC" : "UPLOAD"); }
function filtered() {
  const search = $("search").value.trim().toLowerCase(), language = $("language").value, channel = $("channel").value, dataset = $("dataset").value;
  return state.conversations.filter(c => c.conversation_id.toLowerCase().includes(search) && (dataset === "all" || groupOf(c) === dataset) && (language === "all" || c.language === language) && (channel === "all" || c.channel === channel));
}
function latestFor(id) { return state.evaluations.find(e => e.conversation_id === id); }
function renderLibrary() {
  const list = filtered(); $("count").textContent = list.length === state.conversations.length ? String(list.length) : `${list.length} / ${state.conversations.length}`;
  $("conversation-list").replaceChildren(...list.map(c => {
    const evaluation = latestFor(c.id);
    return node("button", {className: "conversation-item" + (state.selected === c.id ? " selected" : ""), onclick: action(() => selectConversation(c.id)), "aria-pressed": String(state.selected === c.id)}, [
      node("span", {className: "conversation-id"}, [c.conversation_id, node("span", {className: "mini-score", text: evaluation ? (evaluation.provenance.is_mock ? "MOCK" : score(evaluation.original_summary.total)) : "↗"})]),
      node("span", {className: "conversation-sub"}, [node("span", {text: `${c.language} · ${c.channel === "voice call" ? "Звонок" : "Чат"}`}), node("span", {text: `${c.turn_count} репл.`})])
    ]);
  }));
  if (!list.length) $("conversation-list").append(node("p", {className: "small muted", text: state.conversations.length ? "Нет совпадений" : "Диалоги появятся после загрузки"}));
  $("batch").textContent = `Оценить набор (${list.length})`;
  $("batch").disabled = !list.length || state.busy;
}
async function loadData() {
  const [conversations, evaluations, rubrics, config, providerOptions] = await Promise.all([api("/api/conversations"), api("/api/evaluations"), api("/api/rubrics"), api("/api/config"), api("/api/provider-options")]);
  Object.assign(state, {conversations, evaluations, rubrics, config, providerOptions});
  const currentDataset = $("dataset").value;
  const groups = [...new Set(conversations.map(groupOf))].sort();
  $("dataset").replaceChildren(node("option", {value:"all",text:"Все наборы"}), ...groups.map(g => node("option", {value:g,text:g === "SYNTHETIC" ? "Учебные стресс-сценарии" : g === "UPLOAD" ? "Загруженные диалоги" : "Набор " + g})));
  if (groups.includes(currentDataset)) $("dataset").value = currentDataset;
  const current = $("rubric-select").value;
  $("rubric-select").replaceChildren(...rubrics.map((r, i) => node("option", {value: r.id, text: `${r.name}${i === 0 && rubrics.length > 1 ? " · новая" : ""}`})));
  if (rubrics.some(r => r.id === current)) $("rubric-select").value = current;
  renderConnection(); renderLibrary();
}
async function openImported(result) {
  await loadData();
  const first = state.conversations.find(c => c.id === result.ids[0]);
  if (!first) throw new Error("Импорт не вернул сохранённый диалог");
  $("dataset").value = groupOf(first); $("search").value=""; $("language").value="all"; $("channel").value="all";
  await selectConversation(first.id);
}
function cloudReady(provider) {
  if (provider === "groq") return state.config.key_present && state.config.free_plan_confirmed;
  if (provider === "gemini") return state.config.gemini_key_present && state.config.gemini_free_plan_confirmed;
  return state.providerOptions.options.some(p => p.name === provider && p.available);
}
function renderConnection() {
  const ready = cloudReady(state.config.provider);
  $("connection").textContent = ["groq","gemini"].includes(state.config.provider) ? (ready ? (state.config.provider === "gemini" ? "Gemini · Free Tier" : "Groq · Free Plan") : "AI · нужен ключ и Free Tier") : (ready ? "Локальная9B · работает на ноутбуке" : "Локальная модель не запущена");
  $("connection").className = "status-pill" + (ready ? " ready" : "");
  $("settings").textContent = ready ? "Настройки AI" : "Подключить AI";
  $("hosted-banner").hidden = !state.config.hosted && !state.config.deployment_setup;
  $("hosted-banner").textContent = state.config.deployment_setup ? "Новые ключи для Render: сохраните Gemini и Groq по очереди в «Настройках AI», подтвердив бесплатный тариф. Текущее подключение ноутбука не меняется." : "Демо-сеанс хранит данные временно. Скачайте результаты перед выходом; анализ передаёт текст выбранному внешнему API.";
  $("logout").hidden = !state.config.hosted;
  $("logout").textContent = state.config.public_access ? "Новый сеанс" : "Выйти";
  $("dev").hidden = !!state.config.hosted || !!state.config.deployment_setup;
}
async function selectConversation(id) {
  state.selected = id; state.conversation = await api("/api/conversations/" + encodeURIComponent(id)); state.evaluation = null;
  $("empty").hidden = true; $("detail").hidden = false;
  $("detail-id").textContent = state.conversation.conversation_id; $("detail-title").textContent = state.conversation.conversation_id;
  $("detail-meta").textContent = `${state.conversation.language} · ${state.conversation.channel === "voice call" ? "Звонок" : "Чат"} · ${state.conversation.transcript.length} реплик`;
  $("turn-count").textContent = `${state.conversation.transcript.length} реплик`;
  $("conversation-warnings").replaceChildren(...state.conversation.warnings.map(w => node("p", {className: "warning", text: w})));
  $("transcript").replaceChildren(...state.conversation.transcript.map((t, i) => node("article", {className: "turn " + t.speaker, id: "turn-" + t.id}, [
    node("div", {className: "turn-meta"}, [node("b", {text: roles[t.speaker]}), node("span", {text: t.timestamp || "без времени"}), node("span", {className: "turn-num", text: "#" + (i + 1)})]), node("div", {className: "turn-text", text: t.text})])));
  $("audio").replaceChildren();
  if (state.conversation.audio_url) $("audio").append(node("a", {className: "audio-link", href: state.conversation.audio_url, target: "_blank", rel: "noopener noreferrer", text: "Открыть запись звонка ↗"}));
  renderLibrary(); renderHistory(); renderEvaluation();
  const latest = latestFor(id);
  if (latest) await showEvaluation(latest.id);
}
function jump(turnId) {
  const target = $("turn-" + turnId);
  if (!target) return;
  document.querySelectorAll(".turn.highlight").forEach(e => e.classList.remove("highlight"));
  target.classList.add("highlight"); target.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest"});
}
function renderHistory() {
  const history = state.evaluations.filter(e => e.conversation_id === state.selected);
  $("history").replaceChildren(...(!state.evaluation ? [node("option", {value: "", text: history.length ? "Выберите сохранённую оценку" : "Нет оценок"})] : []), ...history.map(e => node("option", {value: e.id, text: `${e.provenance.is_mock ? "MOCK" : e.provenance.model} · ${new Date(e.created_at).toLocaleTimeString("ru-RU")}`})));
  if (state.evaluation) $("history").value = state.evaluation.id;
}
async function showEvaluation(id) { state.evaluation = await api("/api/evaluations/" + encodeURIComponent(id)); if (state.rubrics.some(r => r.id === state.evaluation.rubric.id)) $("rubric-select").value = state.evaluation.rubric.id; renderHistory(); renderEvaluation(); }
function answerClass(c, answer) { return answer === "N/A" ? " na" : c.kind === "flag" && answer === "YES" ? " flag" : answer === "NO" && c.kind !== "flag" ? " no" : ""; }
function renderEvaluation() {
  const evaluation = state.evaluation;
  renderQuality(evaluation);
  $("mock-banner").hidden = !evaluation?.provenance.is_mock;
  $("total-score").textContent = score(evaluation?.final_summary.total);
  $("critical-score").textContent = !evaluation ? "—" : evaluation.final_summary.gross_error ? "Обнаружены" : "Не обнаружены";
  $("critical-score").className = evaluation?.final_summary.gross_error ? "attention" : "";
  $("attention-score").textContent = evaluation ? (evaluation.quality?.review_count ?? evaluation.final_criteria.filter(c => c.ambiguous).length) : "—";
  $("original-score").textContent = !evaluation ? "Оценка ещё не выполнена" : evaluation.appeals.some(a => a.status === "resolved") ? `Исходная ${evaluation.provenance.is_mock ? "MOCK" : "AI"}-оценка: ${score(evaluation.original_summary.total)}` : evaluation.provenance.is_mock ? "Условная тестовая оценка (MOCK)" : "Автоматическая оценка";
  $("export-json").disabled = $("export-csv").disabled = !evaluation;
  $("rubric-version").textContent = evaluation ? evaluation.rubric.id.slice(-8) : "Версия " + (rubric()?.id.slice(-8) || "—");
  $("sections").replaceChildren(...(evaluation?.final_summary.sections || []).map(s => node("div", {className: "section-score"}, [node("span", {text: s.name}), node("span", {text: `${score(s.score)} / 100`})])));
  const criteria = evaluation?.rubric.criteria || rubric()?.criteria || [];
  $("criteria-list").replaceChildren(...criteria.map((c, i) => {
    const decision = evaluation?.final_criteria.find(d => d.criterion_id === c.id);
    const d = node("details", {id: "criterion-" + c.id, className: "criterion", open: i === 0 && !!decision}, [node("summary", {}, [node("span", {className: "criterion-number", text: String(i + 1).padStart(2, "0")}), node("span", {className: "criterion-title", text: c.name}), node("span", {className: "criterion-weight", text: c.kind === "flag" ? "флаг" : "вес " + c.weight}), node("span", {className: "answer" + answerClass(c, decision?.answer), text: decision ? answerText(decision.answer) : "—"})])]);
    const body = node("div", {className: "criterion-body"});
    if (!decision) body.append(node("p", {className: "muted", text: c.instruction}));
    else {
      const contribution = evaluation.final_summary.contributions.find(x => x.criterion_id === c.id);
      if (contribution) body.append(node("p", {className: "small muted", text: decision.answer === "N/A" ? `Вес ${c.weight} исключён и перераспределён между применимыми критериями` : `${c.kind === "numeric" ? `${decision.answer} / ${Math.max(...c.levels.map(l => l.score))} · ` : ""}По весу: ${score(contribution.raw_points)} / ${c.weight}. После N/A: ${score(contribution.contribution)} / ${score(contribution.effective_weight)} балла`}));
      body.append(node("p", {text: decision.rationale}));
      if (decision.answer === "N/A") body.append(node("p", {className: "muted", text: "N/A: " + decision.na_reason}));
      if (decision.reviewed) body.append(node("div", {className: "review-history", text: `Решение проверяющего: ${answerText(decision.original_answer)} → ${answerText(decision.answer)}. ${decision.review_reason}`}));
      if (decision.ambiguous) body.append(node("span", {className: "attention", text: "● Неоднозначно · рекомендуется пересмотр"}));
      for (const limitation of decision.limitations) body.append(node("p", {className: "small muted", text: limitation}));
      for (const evidence of decision.evidence) body.append(node("button", {type: "button", className: "evidence-button", onclick: () => jump(evidence.turn_id)}, [node("span", {text: `Реплика #${Number(evidence.turn_id.slice(1))} · ${roles[evidence.speaker]}`}), "«" + evidence.quote + "» ↗"]));
      body.append(node("button", {type: "button", className: "coverage-link", onclick: () => jump(decision.coverage.start), text: `Проверен участок #${Number(decision.coverage.start.slice(1))}–#${Number(decision.coverage.end.slice(1))} ↗`}));
      body.append(node("p", {className: "small muted", text: `Дословный текст и роль проверены кодом. Смысловое обоснование — решение ${decision.reviewed ? "проверяющего" : "модели"}.`}));
      const appeals = evaluation.appeals.filter(a => a.criterion_id === c.id);
      for (const appeal of appeals) {
        const box = node("div", {className: "review-box"}, [node("b", {text: appeal.status === "open" ? "Апелляция ожидает проверки" : "Решение сохранено"}), node("p", {text: appeal.comment})]);
        if (appeal.status === "resolved") box.append(node("p", {className: "small", text: `${appeal.decision === "confirm" ? "Подтверждено" : "Изменено"}: ${answerText(appeal.new_answer)}. ${appeal.reason}`}));
        else {
          const select = node("select", {"aria-label": "Ответ проверяющего"}, optionsFor(c)); select.value = decision.answer;
          const reason = node("input", {placeholder: "Обоснование решения проверяющего", "aria-label": "Обоснование решения проверяющего", maxLength: 4000});
          const refs = node("input", {placeholder: "Например: 2, 4", "aria-label": "Номера реплик для решения", value: decision.evidence.map(e => Number(e.turn_id.slice(1))).join(", ")});
          const resolve = async change => { await api(`/api/appeals/${appeal.id}/resolve`, {decision: change ? "change" : "confirm", new_answer: select.value, reason: reason.value, evidence_turns: refs.value.split(",").map(x => Number(x.trim()))}); await showEvaluation(evaluation.id); toast("Решение сохранено. Итог пересчитан"); };
          box.append(node("label", {text: "Ответ проверяющего"}), select, node("label", {text: "Причина"}), reason, node("label", {text: "Номера исходных реплик (для изменения)"}), refs, node("button", {type: "button", className: "quiet", text: "Подтвердить оценку", onclick: action(() => resolve(false))}), " ", node("button", {type: "button", className: "primary", text: "Изменить оценку", onclick: action(() => resolve(true))}));
        }
        body.append(box);
      }
      if (!appeals.some(a => a.status === "open")) body.append(node("button", {className: "text-button", text: "Оспорить оценку ↗", onclick: () => { appealCriterion = c.id; $("appeal-context").textContent = `${c.name} · текущий ответ: ${answerText(decision.answer)}`; $("appeal-comment").value = ""; openDialog("appeal-dialog"); }}));
    }
    d.append(body); return d;
  }));
  $("provenance").replaceChildren();
  if (evaluation) {
    const p = evaluation.provenance;
    $("provenance").append(node("p", {text: `${p.is_mock ? "MOCK" : "AI"} · ${p.provider} / ${p.model} · ${Math.round(p.latency_ms / 1000)} с · ${p.input_tokens ?? "?"} + ${p.output_tokens ?? "?"} токенов`}), node("p", {text: evaluation.rubric.name}), node("p", {text: p.cost_note || "Стоимость не измерена"}), node("code", {text: `Рубрика ${p.rubric_hash.slice(0, 10)} · промпт ${p.prompt_hash.slice(0, 10)} · код ${p.code_hash.slice(0, 10)}`}));
  }
}
function optionsFor(c) {
  const values = c.kind === "numeric" ? c.levels.map(l => String(l.score)) : ["YES", "NO"];
  if (c.na_allowed) values.push("N/A");
  return values.map(value => node("option", {value, text: answerText(value)}));
}
function renderQuality(evaluation) {
  const report = evaluation?.quality;
  $("quality-panel").hidden = !report;
  $("recheck").disabled = state.busy || !evaluation || !!evaluation.provenance.is_mock;
  $("quality-priority").replaceChildren(); $("quality-comparison").replaceChildren();
  if (!report) return;
  $("quality-summary").textContent = report.review_count ? `Критериев с поводом для пересмотра: ${report.review_count}` : "Выберите критерий по его влиянию на итог или запустите повторную проверку";
  const makeRow = item => {
    const open = () => { const section = $("criterion-" + item.criterion_id); if (section) { section.open = true; section.scrollIntoView({behavior: "smooth", block: "center"}); } };
    const title = node("button", {type:"button",className:"quality-row",onclick:open}, [node("span", {text:item.name}), node("b", {text:item.gross_error ? "Грубое нарушение" : `до ${score(item.max_score_change)} балла`})]);
    return node("div", {className:"quality-item"}, [title, node("p", {className:"small muted",text:item.reasons.length ? item.reasons.join(" · ") : `Текущий ответ: ${answerText(item.answer)}`}), node("p", {className:"small",text:"Если изменить только этот ответ: " + item.alternatives.map(a=>`${answerText(a.answer)} → ${score(a.total)}`).join(" · ")})]);
  };
  const visible = report.items.slice(0,3);
  $("quality-priority").append(...visible.map(makeRow));
  if (report.items.length > 3) $("quality-priority").append(node("details", {className:"quality-more"}, [node("summary", {text:`Все критерии (${report.items.length})`}), ...report.items.slice(3).map(makeRow)]));
  const comparison = evaluation.comparison;
  if (comparison) {
    const box = node("div", {className:"quality-comparison" + (comparison.changed_criteria ? " has-differences" : "")});
    if (["incompatible", "source_unavailable"].includes(comparison.status)) box.append(node("p", {text:"Сравнение недоступно: исходная оценка или её версия не совпадают"}));
    else {
      box.append(node("b", {text:comparison.changed_criteria ? `Перепроверка: расхождений ${comparison.changed_criteria}` : "Две автоматические оценки совпали"}), node("p", {className:"small",text:`Исходный AI: ${score(comparison.original_total)} · повторный AI: ${score(comparison.repeated_total)}`}));
      for (const change of comparison.changes) box.append(node("p", {className:"small",text:`${evaluation.rubric.criteria.find(c=>c.id===change.criterion_id)?.name || change.criterion_id}: ${answerText(change.before)} → ${answerText(change.after)}`}));
      box.append(node("p", {className:"small muted",text:comparison.note}));
    }
    $("quality-comparison").append(box);
  }
}
function setBusy(value) { state.busy = value; $("evaluate").disabled = $("mock-run").disabled = value; for (const id of ["rubric-select", "edit-rubric", "nav-rubric"]) $(id).disabled = value; $("recheck").disabled = value || !state.evaluation || !!state.evaluation.provenance.is_mock; renderLibrary(); }
async function startEvaluation(ids, mock = false) {
  if (!ids.length) throw new Error("Сначала загрузите диалоги");
  if (!mock && !cloudReady(state.config.provider)) { await settings(); return; }
  if (!state.selected) await selectConversation(ids[0]);
  setBusy(true);
  try {
    const job = await api("/api/evaluate", {conversation_ids: ids, rubric_id: rubric().id, provider: mock ? "mock" : state.config.provider, model: state.config.model});
    await monitorJob(job);
  } finally { setBusy(false); }
}
async function monitorJob(job) {
    state.job = job; $("job-progress").hidden = false;
    while (true) {
      const current = await api("/api/jobs/" + job.id); state.job = current;
      $("job-title").textContent = current.is_mock ? "MOCK · проверка интерфейса" : current.status === "running" ? "AI анализирует диалоги…" : current.status === "interrupted" ? "Пакет прерван; сохранённые исходы доступны" : "Пакет завершён";
      $("job-count").textContent = `${current.completed} / ${current.total}`; $("job-bar").max = current.total; $("job-bar").value = current.completed;
      const errors = current.outcomes.filter(o => o.status === "error");
      $("job-errors").textContent = errors.length ? `${errors.length} ошибок. ${errors[errors.length - 1].conversation_id}: ${errors[errors.length - 1].error}` : "";
      const resumable = !current.is_mock && current.rubric_id && current.status !== "running" && (errors.length || current.completed < current.total);
      $("resume-job").hidden = $("resume-note").hidden = !resumable;
      if (current.preserved_successes) $("job-count").textContent += ` · сохранено: ${current.preserved_successes}`;
      if (current.status !== "running") {
        await loadData(); renderHistory();
        const outcome = current.outcomes.find(o => o.internal_id === state.selected && o.status === "ok");
        if (outcome) await showEvaluation(outcome.evaluation_id);
        $("submission").disabled = current.is_mock || current.completed !== current.total;
        toast(errors.length ? `Завершено с ошибками: ${errors.length}. Ошибки сохраняются для официальной выгрузки` : "Оценки сохранены", !!errors.length); break;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
}
async function restoreLatestJob() {
  const job = await api("/api/latest-job");
  if (!job) return;
  const members = job.outcomes.map(o => state.conversations.find(c => c.id === o.internal_id)).filter(Boolean);
  const groups = [...new Set(members.map(groupOf))];
  if (groups.length === 1) { $("dataset").value = groups[0]; renderLibrary(); }
  const first = job.outcomes.find(o => o.status === "ok") || job.outcomes[0];
  if (first && state.conversations.some(c => c.id === first.internal_id)) await selectConversation(first.internal_id);
  setBusy(job.status === "running");
  try { await monitorJob(job); } finally { setBusy(false); }
}
async function download(path, filename) {
  const response = await fetch(path);
  if (!response.ok) { const data = await response.json(); throw new Error(data.error); }
  const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = node("a", {href: url, download: filename}); a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function settings() {
  state.providerOptions = await api("/api/provider-options");
  $("provider").replaceChildren(...state.providerOptions.options.map(p => node("option",{value:p.name,text:p.label})));
  if (!state.providerOptions.options.some(p => p.name === state.config.provider)) $("provider").append(node("option",{value:state.config.provider,text:"Сохранённая локальная модель сейчас недоступна",disabled:true}));
  $("provider").value = state.config.provider; $("model").value = state.config.model; $("team-name").value = state.config.team_name || "";
  $("api-key").value = ""; $("key-status").textContent = state.config.key_present ? "· уже сохранён" : ""; $("free-plan").checked = state.config.free_plan_confirmed;
  openDialog("settings-dialog"); await models(false);
}
async function models(change = true) {
  const provider = $("provider").value; $("groq-settings").hidden = !!state.config.hosted || !["groq","gemini"].includes(provider);
  $("managed-ai-note").hidden = !state.config.hosted;
  const gemini = provider === "gemini";
  $("key-label").textContent = gemini ? "Ключ Gemini" : "Ключ Groq";
  $("api-key").placeholder = gemini ? "AIza…" : "gsk_…";
  $("key-status").textContent = (gemini ? state.config.gemini_key_present : state.config.key_present) ? "· уже сохранён" : "";
  $("free-plan").checked = gemini ? !!state.config.gemini_free_plan_confirmed : !!state.config.free_plan_confirmed;
  $("free-plan-text").textContent = gemini ? "Мой проект использует Gemini Free Tier; платный биллинг не подключён" : "Мой аккаунт использует Groq Free Plan; платный биллинг не включён";
  $("provider-data-notice").textContent = gemini ? "Выбранный транскрипт отправляется Google. На Free Tier содержимое может использоваться для улучшения продуктов. Тариф проекта подтверждаете вы." : "Внешний API получает выбранный транскрипт. При лимите сохраняется явная ошибка. Тариф аккаунта подтверждаете вы.";
  $("key-link").href = gemini ? "https://aistudio.google.com/apikey" : "https://console.groq.com/keys";
  $("key-link").textContent = gemini ? "Открыть ключи Gemini ↗" : "Открыть ключи Groq ↗";
  if (change) $("api-key").value = "";
  if (change) $("model").value = provider === "gemini" ? "gemini-3.5-flash" : provider === "groq" ? "openai/gpt-oss-120b" : "";
  const result = await api("/api/models?provider=" + provider);
  $("model-options").replaceChildren(...result.models.filter(m => !m.remote).map(m => node("option", {value: m.name})));
  $("models-status").textContent = ["groq","gemini"].includes(provider) ? "Кандидаты Free Tier; доступ и точные квоты проверяются реальным вызовом" : result.models.length ? "Локальный сервер ответил; выберите загруженную модель" : "Локальный сервер пока недоступен";
  if (!["groq","gemini"].includes(provider) && change && result.models[0]) $("model").value = result.models[0].name;
}
function editorField(c, property, label, tag = "input", attrs = {}) {
  const input = node(tag, {value: c[property] ?? "", "data-field": property, ...attrs});
  return node("label", {}, [label, input]);
}
function renderEditor() {
  $("rubric-editor").replaceChildren(...editorRubric.criteria.map((c, i) => {
    const kind = node("select", {"data-field": "kind"}, [node("option", {value: "binary", text: "Да / Нет"}), node("option", {value: "numeric", text: "Числовая шкала"}), node("option", {value: "flag", text: "Флаг без баллов"})]); kind.value = c.kind;
    const na = node("input", {type: "checkbox", checked: c.na_allowed, "data-field": "na_allowed"});
    const levels = (c.levels || [{score: 0, description: "Не выполнено"}, {score: 10, description: "Выполнено"}]).map(x => `${x.score} | ${x.description}`).join("\n");
    const content = node("div", {className: "editor-grid"}, [editorField(c, "id", "Код", "input", {required: true}), editorField(c, "name", "Название", "input", {required: true}), editorField(c, "section", "Секция"), node("label", {}, ["Тип ответа", kind]), editorField(c, "weight", "Вес", "input", {type: "number", min: 0, max: 10000, step: "any", required: true}), node("label", {className: "check"}, [na, "Разрешён N/A"]), node("div", {className: "span2"}, [editorField(c, "instruction", "Правило оценки", "textarea", {rows: 3, required: true})]), node("div", {className: "span2"}, [editorField(c, "na_rule", "Условие N/A", "textarea", {rows: 2})]), node("label", {className: "span2"}, ["Ступени числовой шкалы: балл | описание (каждая с новой строки)", node("textarea", {"data-field": "levels_text", rows: 4, value: levels})])]);
    return node("details", {className: "rubric-row", "data-index": i, open: i === 0}, [node("summary", {text: `${String(i + 1).padStart(2, "0")} · ${c.name} · вес ${c.weight}`}), content, node("button", {type: "button", className: "text-button", text: "Удалить критерий", onclick: () => { collectEditor(); editorRubric.criteria.splice(i, 1); renderEditor(); }})]);
  }));
}
function collectEditor() {
  document.querySelectorAll(".rubric-row").forEach((row, i) => {
    const c = editorRubric.criteria[i]; const get = prop => row.querySelector(`[data-field="${prop}"]`);
    for (const property of ["id", "name", "section", "kind", "instruction", "na_rule"]) c[property] = get(property).value;
    c.weight = Number(get("weight").value); c.na_allowed = get("na_allowed").checked;
    if (c.kind === "numeric") c.levels = get("levels_text").value.split("\n").filter(x => x.trim()).map(line => { const index = line.indexOf("|"); if (index < 0) throw new Error("Шкала: балл | описание"); return {score: Number(line.slice(0, index).trim()), description: line.slice(index + 1).trim()}; });
    else delete c.levels;
  });
}
function editRubric() { editorRubric = structuredClone(rubric()); $("rubric-name").value = editorRubric.name; renderEditor(); openDialog("rubric-dialog"); }

document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
for (const id of ["import", "empty-import"]) $(id).addEventListener("click", () => openDialog("import-dialog"));
for (const id of ["settings", "nav-settings"]) $(id).addEventListener("click", action(settings));
for (const id of ["edit-rubric", "nav-rubric"]) $(id).addEventListener("click", action(editRubric));
$("nav-dialogues").addEventListener("click", () => $("search").focus());
for (const id of ["search", "language", "channel", "dataset"]) $(id).addEventListener(id === "search" ? "input" : "change", renderLibrary);
$("rubric-select").addEventListener("change", () => { state.evaluation = null; renderHistory(); renderEvaluation(); });
$("history").addEventListener("change", action(() => $("history").value ? showEvaluation($("history").value) : Promise.resolve()));
$("evaluate").addEventListener("click", action(() => startEvaluation([state.selected])));
$("mock-run").addEventListener("click", action(() => startEvaluation([state.selected], true)));
$("batch").addEventListener("click", action(() => startEvaluation(filtered().map(c => c.id))));
$("recheck").addEventListener("click", action(async () => {
  if (!state.evaluation || state.busy) return;
  if (!cloudReady(state.config.provider)) { await settings(); return; }
  const source = state.evaluation.id;
  setBusy(true);
  try { toast("Новая AI-проверка по исходной рубрике; исходная оценка сохраняется"); await monitorJob(await api(`/api/evaluations/${source}/recheck`, {provider:state.config.provider,model:state.config.model})); }
  finally { setBusy(false); }
}));
$("resume-job").addEventListener("click", action(async () => {
  if (!state.job || state.busy) return;
  if (!cloudReady(state.config.provider)) { await settings(); return; }
  setBusy(true); $("resume-job").hidden = true;
  try { await monitorJob(await api(`/api/jobs/${state.job.id}/resume`, {provider: state.config.provider, model: state.config.model})); }
  finally { setBusy(false); }
}));
$("provider").addEventListener("change", action(() => models(true)));
$("api-key").addEventListener("input", () => { if ($("api-key").value.trim()) $("free-plan").checked = false; });
$("dev").addEventListener("click", action(async () => { const result = await api("/api/import-dev", {}); await openImported(result); toast(`DEV загружен: ${result.imported} записей`); }));
$("upload-file").addEventListener("change", action(async () => { const file = $("upload-file").files[0]; if (!file) return; const maximum = state.config.hosted ? 2_000_000 : 8_000_000; if (file.size > maximum) throw new Error(`Максимальный размер файла — ${state.config.hosted ? 2 : 8} МБ`); $("transcript-input").value = (await file.text()).replace(/^\uFEFF/, ""); }));
$("import-form").addEventListener("submit", action(async event => { event.preventDefault(); let payload = $("transcript-input").value.trim(); if (!payload) throw new Error("Добавьте транскрипт"); if (/^[\[{]/.test(payload) && !/^\[\d\d:/.test(payload)) { try { payload = JSON.parse(payload); } catch { throw new Error("Повреждённый JSON. Проверьте скобки и запятые"); } } const result = await api("/api/import", payload); closeDialog("import-dialog"); await openImported(result); toast(`Импортировано диалогов: ${result.imported}`); }));
$("settings-form").addEventListener("submit", action(async event => {
  event.preventDefault(); const provider = $("provider").value;
  const payload = {provider, model: $("model").value.trim(), team_name: $("team-name").value.trim()};
  if (!state.config.hosted && provider === "gemini") Object.assign(payload, {gemini_key: $("api-key").value.trim(), gemini_free_plan_confirmed: $("free-plan").checked});
  if (!state.config.hosted && provider === "groq") Object.assign(payload, {key: $("api-key").value.trim(), free_plan_confirmed: $("free-plan").checked});
  state.config = await api("/api/config", payload); $("api-key").value = ""; closeDialog("settings-dialog"); renderConnection(); toast("Подключение сохранено. Доступ будет проверен при оценке");
}));
$("rubric-form").addEventListener("submit", action(async event => { event.preventDefault(); collectEditor(); editorRubric.name = $("rubric-name").value.trim(); const saved = await api("/api/rubrics", editorRubric); await loadData(); $("rubric-select").value = saved.id; state.evaluation = null; renderHistory(); renderEvaluation(); closeDialog("rubric-dialog"); toast("Новая версия сохранена и выбрана для следующей оценки"); }));
$("add-criterion").addEventListener("click", action(() => { collectEditor(); editorRubric.criteria.push({id: "custom_" + Date.now(), name: "Новый критерий", section: "Общие", kind: "binary", weight: 10, instruction: "Укажите наблюдаемое правило оценки", na_allowed: false, na_rule: "", positive_evidence_roles: ["operator"]}); renderEditor(); }));
$("appeal-form").addEventListener("submit", action(async event => { event.preventDefault(); await api("/api/appeals", {evaluation_id: state.evaluation.id, criterion_id: appealCriterion, comment: $("appeal-comment").value}); closeDialog("appeal-dialog"); await showEvaluation(state.evaluation.id); toast("Апелляция сохранена. Проверяющий может принять отдельное решение"); }));
$("export-json").addEventListener("click", action(() => download(`/api/evaluations/${state.evaluation.id}/export`, "cqa-evaluation.json")));
$("export-csv").addEventListener("click", action(() => download(`/api/evaluations/${state.evaluation.id}/export?format=csv`, "cqa-evaluation.csv")));
$("submission").addEventListener("click", () => openDialog("submission-dialog"));
$("submission-form").addEventListener("submit", action(async event => { event.preventDefault(); if (!state.config.team_name) throw new Error("Укажите название команды в настройках AI"); await download(`/api/jobs/${state.job.id}/submission?dataset=${$("submission-dataset").value}&team=${encodeURIComponent(state.config.team_name)}`, "results.json"); closeDialog("submission-dialog"); toast("Формат проверен; файл скачан. Commit и push выполняются отдельно"); }));
$("sample").addEventListener("click", action(async () => { const raw = {conversation_id: "synthetic-demo-001", source: "synthetic", language: "RU", transcript: [{timestamp: "00:00", speaker: "client", text: "Здравствуйте! Где посмотреть общий список тарифов? Я не нахожу его на сайте, это неудобно."}, {timestamp: "00:05", speaker: "operator", text: "Здравствуйте! Меня зовут Анна. Понимаю, что искать неудобно. Общий список доступен в разделе «Документы», пункт «Тарифы». Личные данные для общего списка не нужны."}, {timestamp: "00:30", speaker: "client", text: "Спасибо, нашёл."}, {timestamp: "00:35", speaker: "operator", text: "Рада помочь! Итак, общий список находится в разделе «Документы» → «Тарифы». Если потребуется ещё помощь, обращайтесь. До свидания!"}]}; const result = await api("/api/import", raw); await openImported(result); toast("Учебный синтетический диалог. Это не Ground Truth"); }));
$("logout").addEventListener("click", action(async () => {
  await fetch("/auth/logout", {method:"POST", headers:{"X-CQA-Token":token}});
  location.href = "/login";
}));
loadData().then(async () => { await restoreLatestJob(); if (state.config.hosted && !state.selected && state.conversations.length) await selectConversation(state.conversations[0].id); if (state.config.deployment_setup && (!state.config.key_present || !state.config.gemini_key_present)) await settings(); }).catch(error => toast(error.message, true));
