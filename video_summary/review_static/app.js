"use strict";

// Local-only source review. Keep server data out of HTML and navigation URLs.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { catalog: null, events: [], eventId: null, candidateId: null, candidate: null, playback: null, saveInProgress: false, dirty: false };
  const kinds = { meal_body: "실제 식사", play_action: "놀이·활동", action_result: "행동의 결과·반응", interview: "인터뷰", meeting: "사람과의 만남", other: "기타" };
  const bases = { visual: "직접 본 화면", speech: "발화", inferred: "추정 (미확인)" };
  const stages = { setup: "준비·도입", action: "행동", body: "본 장면", reaction: "반응", payoff: "결과", outcome: "결과", closure: "마무리", bridge: "연결", transition: "전환" };
  const sources = { osmo: "오즈모", action_camera: "액션 카메라", iphone: "아이폰", phone: "휴대폰", shared: "공유받은 미디어", still: "스틸 사진", image: "사진", unknown: "기기 미확인" };
  const flags = { no_human_observation: "사람의 관찰 기록 없음", missing_visual_evidence: "실제 화면 근거 확인 필요", inferred_only: "추정 근거만 있음", speech_boundary_cut: "후보가 발화 중간에서 잘립니다. 핵심 구간을 확인하세요.", source_fingerprint_mismatch: "원본 변경 여부 확인 필요", observed_core_not_fully_in_candidate: "기록한 핵심 행동이 이 후보 안에 모두 들어오지 않습니다.", stale_or_invalid_observation: "이전 관찰 기록을 다시 확인해야 합니다.", meal_body_needs_visual_review: "실제 음식·식사 행동이 나오는지 화면 확인 필요", no_transcript_evidence: "저장된 발화 근거 없음", phone_without_transcript_visual_review: "휴대폰 영상의 화면 확인 필요 (전사 없음)", invalid_transcript_evidence: "기존 자막의 시간 정보에 오류가 있습니다.", observation_needs_transcript_review: "자막 시간 오류로 관찰 근거 재검증이 보류되었습니다." };
  const video = $("source-video");
  function node(tag, className, content) { const item = document.createElement(tag); if (className) item.className = className; if (content !== undefined && content !== null) item.textContent = String(content); return item; }
  function time(value) { const seconds = Math.max(0, Number(value) || 0); const hours = Math.floor(seconds / 3600); const minutes = Math.floor((seconds % 3600) / 60); const remainder = (seconds % 60).toFixed(1).padStart(4, "0"); return `${hours ? `${hours}:` : ""}${String(minutes).padStart(2, "0")}:${remainder}`; }
  function rangeText(start, end) { return `${time(start)} – ${time(end)}`; }
  function precise(value) { return Math.min(candidateDuration(state.candidate), Math.max(0, Math.round(Number(value) * 1000) / 1000)); }
  function stamp(value) { return value ? String(value).replace("T", " ") : "정보 없음"; }
  function safeMedia(value, prefix) { if (typeof value !== "string") return null; try { const url = new URL(value, location.origin); return url.origin === location.origin && url.pathname.startsWith(prefix) ? url.pathname + url.search : null; } catch (_) { return null; } }
  function badge(label, type) { return node("span", `badge ${type || "neutral"}`, label); }
  function alertStatus(message, error = false) { const target = $("status"); target.textContent = message; target.className = `status${error ? " error" : ""}`; target.hidden = false; }
  function candidateWarnings(candidate) { const result = Array.isArray(candidate.evidence?.flags) ? [...candidate.evidence.flags] : []; if (candidate.capture_time_confidence === "low") result.push("촬영 시각 신뢰도 낮음"); return [...new Set(result)]; }
  function eventCounts(event) { return event.candidates.reduce((counts, candidate) => { counts.selected += candidate.selected ? 1 : 0; counts.omitted += candidate.selected ? 0 : 1; counts.warnings += candidateWarnings(candidate).length ? 1 : 0; return counts; }, { selected: 0, omitted: 0, warnings: 0 }); }
  function allowedToDiscard() { if (state.saveInProgress) { alertStatus("저장이 끝난 뒤 이동하거나 새로고침하세요."); return false; } return !state.dirty || window.confirm("아직 저장하지 않은 구간과 메모가 있습니다. 변경 내용을 버리고 이동할까요?"); }
  function eventKey(event) { return `${event.day_key}/${event.event_id}`; }
  function reviewedRanges(candidate, action) { return candidate.reviewed_ranges?.[action] || []; }
  function evidence(candidate) { return candidate.evidence || {}; }
  function candidateDuration(candidate) { return Math.max(Number(candidate.source_duration) || 0, Number(candidate.end) || 0); }
  function candidateRange(candidate, context = false) {
    const record = evidence(candidate);
    const proposed = context ? record.context_range : record.suggested_core_range;
    const maximum = candidateDuration(candidate);
    const start = Math.max(0, Number(proposed?.start ?? candidate.start) - (context && !proposed ? 4 : 0));
    const end = Math.min(maximum, Number(proposed?.end ?? candidate.end) + (context && !proposed ? 4 : 0));
    return { start, end };
  }
  function selectedRange() { return { start: Number($("range-start").value), end: Number($("range-end").value) }; }
  function validateRange() { const range = selectedRange(); const maximum = state.candidate ? candidateDuration(state.candidate) : 0; return Number.isFinite(range.start) && Number.isFinite(range.end) && range.start >= 0 && range.end > range.start && range.end <= maximum + 0.001; }
  function renderRange() {
    const range = selectedRange(); const valid = validateRange(); $("range-duration").textContent = valid ? `${(range.end - range.start).toFixed(2)}초 · 원본 길이 ${time(candidateDuration(state.candidate))}` : "시작 < 끝, 원본 길이 안의 구간을 입력하세요."; $("range-duration").classList.toggle("invalid", !valid);
    $("speech-cue-list").querySelectorAll("input").forEach((input) => { input.disabled = !valid || Number(input.dataset.start) < range.start - 0.001 || Number(input.dataset.end) > range.end + 0.001; if (input.disabled) input.checked = false; });
  }
  function appendMeta(label, value) { $("source-meta").append(node("dt", "", label), node("dd", "", value)); }
  function renderEvidence(candidate) {
    const record = evidence(candidate); const observations = Array.isArray(record.observations) ? record.observations : [];
    const visual = Array.isArray(record.confirmed_visual_kinds) ? record.confirmed_visual_kinds : [];
    $("evidence-status").textContent = visual.length ? "화면 관찰 기록 있음" : observations.length ? "관찰 기록 있음" : "미검토";
    $("evidence-status").className = `badge ${observations.length ? "reviewed" : "warning"}`;
    $("evidence-list").replaceChildren();
    (record.classifications || []).forEach((claim) => { const item = node("div", "evidence-item"); item.append(badge("자동 추정", "neutral"), document.createTextNode(`${kinds[claim.kind] || claim.kind || "분류"}${claim.reason ? ` · ${claim.reason}` : ""}`)); $("evidence-list").append(item); });
    observations.forEach((observation) => { const item = node("div", "evidence-item"); item.append(badge(`사람 기록 · ${bases[observation.basis] || observation.basis}`, observation.basis === "inferred" ? "warning" : "reviewed"), document.createTextNode(observation.description || kinds[observation.kind] || observation.kind)); if (observation.core_range) item.append(node("div", "timecode", rangeText(observation.core_range.start, observation.core_range.end))); $("evidence-list").append(item); });
    if (!observations.length) $("evidence-list").append(node("p", "evidence-warning", "실제 식사·놀이·결과가 나오는지 아직 확인되지 않았습니다."));
    candidateWarnings(candidate).forEach((flag) => $("evidence-list").append(node("p", "evidence-warning", flags[flag] || flag)));
    $("speech-cue-list").replaceChildren();
    (record.transcript_cues || []).forEach((cue, index) => { const label = node("label", "cue-label"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.value = cue.cue_id; checkbox.id = `cue-${index}`; checkbox.dataset.start = cue.start; checkbox.dataset.end = cue.end; label.append(checkbox, node("span", "", `${rangeText(cue.start, cue.end)} · ${cue.text}`)); $("speech-cue-list").append(label); });
    if (!(record.transcript_cues || []).length) $("speech-cue-list").append(node("p", "muted", "연결할 발화가 없습니다. 화면 관찰 또는 추정으로 기록하세요."));
  }
  function stopPlayback() { video.pause(); state.playback = null; }
  function sourceFailure(message) {
    stopPlayback(); const target = $("playback-error"); target.replaceChildren(node("span", "", message || "이 브라우저에서 원본 코덱을 재생하지 못했습니다. HEVC/MOV는 Safari나 QuickTime에서 확인할 수 있습니다."));
    const source = safeMedia(state.candidate?.media_url, "/media/");
    if (source) { target.append(document.createTextNode(" ")); const link = node("a", "", "원본 파일 열기"); link.href = source; link.target = "_blank"; link.rel = "noopener"; target.append(link); }
    target.hidden = false; $("video-placeholder").hidden = true;
  }
  function selectCandidate(candidate, force = false) {
    if (!force && !allowedToDiscard()) return;
    stopPlayback(); state.candidate = candidate; state.candidateId = candidate.candidate_id; state.dirty = false;
    const range = candidateRange(candidate); $("range-start").value = range.start; $("range-end").value = range.end; $("range-start").max = candidateDuration(candidate); $("range-end").max = candidateDuration(candidate); $("review-reason").value = ""; $("record-observation").checked = false; $("observation-kind").value = "other"; $("observation-basis").value = "visual"; $("speech-cues").hidden = true;
    $("clip-name").textContent = candidate.clip_name || candidate.clip_id; $("source-badge").textContent = sources[candidate.source_kind] || candidate.source_kind || "원본";
    $("source-meta").replaceChildren(); appendMeta("원본 구간", rangeText(candidate.start, candidate.end)); appendMeta("정렬 기준 시각", stamp(candidate.sequence_at || candidate.captured_at)); appendMeta("기록된 촬영 시각", stamp(candidate.captured_at)); appendMeta("시각 근거", `${candidate.sequence_source || "정보 없음"} · 신뢰도 ${candidate.capture_time_confidence || "미확인"} · ${candidate.capture_time_basis || ""}`); appendMeta("현재 편집안", candidate.selected ? `채택 · ${candidate.speed || 1}배속` : "미채택");
    $("transcript").textContent = candidate.transcript || "이 구간에 저장된 전사가 없습니다. 말이 없다는 사실은 장면이 중요하지 않다는 뜻이 아닙니다.";
    $("selection-reason").textContent = [candidate.reason && `채택 근거: ${candidate.reason}`, candidate.reviewed_inclusion_reason && `필수 포함 규칙: ${candidate.reviewed_inclusion_reason}`, candidate.exclusion_reason && `제외 사유: ${candidate.exclusion_reason}`].filter(Boolean).join("\n") || "현재 편집안의 상세 선택 근거가 없습니다.";
    for (const action of ["include", "exclude"]) reviewedRanges(candidate, action).forEach((rule) => { $("selection-reason").textContent += `\n저장된 ${action === "include" ? "필수 포함" : "제외"} 규칙: ${rangeText(rule.start || 0, rule.end ?? candidateDuration(candidate))} · ${rule.reason || ""}`; });
    renderEvidence(candidate); renderRange();
    $("playback-error").hidden = true; $("playback-range").textContent = rangeText(range.start, range.end); $("video-placeholder").textContent = "재생 버튼을 눌러 원본 구간을 확인하세요."; $("video-placeholder").hidden = false;
    const source = safeMedia(candidate.media_url, "/media/");
    video.removeAttribute("poster"); const frame = safeMedia(candidate.frame_url, "/frame/"); if (frame) video.poster = frame;
    if (source) { if (video.getAttribute("src") !== source) { video.src = source; video.load(); } $("play-candidate").disabled = false; $("play-context").disabled = false; } else { video.removeAttribute("src"); video.load(); $("play-candidate").disabled = true; $("play-context").disabled = true; sourceFailure("이 원본을 찾을 수 없거나 검토 서버에서 제공할 수 없습니다. 원본 연결을 확인하세요."); }
    document.querySelectorAll(".candidate-card").forEach((card) => { const active = card.dataset.candidateId === candidate.candidate_id; card.classList.toggle("active", active); card.setAttribute("aria-pressed", String(active)); });
  }
  function renderCandidates(event) {
    $("candidate-list").replaceChildren();
    event.candidates.forEach((candidate) => {
      const card = node("button", "candidate-card"); card.type = "button"; card.dataset.candidateId = candidate.candidate_id; card.setAttribute("aria-pressed", "false"); card.setAttribute("aria-label", `${candidate.clip_name || candidate.clip_id}, ${rangeText(candidate.start, candidate.end)}, ${candidate.selected ? "채택" : "미채택"}`);
      const thumb = node("div", "candidate-thumb"); const frame = safeMedia(candidate.frame_url, "/frame/");
      if (frame) { const img = node("img"); img.src = frame; img.alt = ""; img.loading = "lazy"; img.addEventListener("error", () => img.remove()); thumb.append(img); } else thumb.append(node("span", "", "원본 구간"));
      thumb.append(badge(candidate.selected ? "편집안 채택" : "미채택", candidate.selected ? "selected" : "omitted"));
      const content = node("div", "candidate-content"); const timing = node("div", "candidate-time"); timing.append(node("span", "", rangeText(candidate.start, candidate.end)), node("span", "", `${Math.max(0, candidate.end - candidate.start).toFixed(1)}초`));
      content.append(timing, node("div", "candidate-name", candidate.clip_name || candidate.clip_id), node("p", "candidate-transcript", candidate.transcript || "전사 없음 · 화면의 의미를 확인하세요."));
      const labels = node("div", "candidate-flags"); labels.append(badge(stages[candidate.story_stage] || candidate.story_stage || "미분류", "neutral")); if (candidate.reviewed_inclusion_reason || reviewedRanges(candidate, "include").length) labels.append(badge("필수 포함 규칙", "reviewed")); if (reviewedRanges(candidate, "exclude").length) labels.append(badge("제외 규칙", "excluded")); if (candidate.exclusion_reason) labels.append(badge("분석 시 제외 대상", "omitted")); if ((evidence(candidate).observations || []).length) labels.append(badge("사람 기록", "reviewed")); if (candidateWarnings(candidate).length) labels.append(badge("확인 필요", "warning")); content.append(labels); card.append(thumb, content); card.addEventListener("click", () => selectCandidate(candidate)); $("candidate-list").append(card);
    });
  }
  function selectEvent(event, force = false) {
    if (!force && !allowedToDiscard()) return;
    state.eventId = eventKey(event); $("empty-state").hidden = true; $("event-workspace").hidden = false; $("event-title").textContent = event.title || event.event_id; $("event-kicker").textContent = `${event.day_key} · EVENT REVIEW`; const counts = eventCounts(event); $("event-summary").textContent = `${event.candidates.length}개 후보 · 채택 ${counts.selected} · 미채택 ${counts.omitted}`;
    renderCandidates(event); const candidate = event.candidates.find((item) => item.candidate_id === state.candidateId) || event.candidates[0]; if (candidate) selectCandidate(candidate, true); renderEventList();
  }
  function visibleEvents() {
    const search = $("search").value.trim().toLocaleLowerCase(); const day = $("day-filter").value; const filter = $("status-filter").value;
    return state.events.filter((event) => { if (day && event.day_key !== day) return false; const counts = eventCounts(event); if (filter === "attention" && !counts.warnings) return false; if (filter === "omitted" && !counts.omitted) return false; if (filter === "selected" && !counts.selected) return false; if (!search) return true; return [event.title, event.event_id, ...event.candidates.flatMap((candidate) => [candidate.clip_name, candidate.transcript, candidate.location, candidate.reason, candidate.exclusion_reason, candidate.reviewed_inclusion_reason, ...(candidate.roles || []), ...reviewedRanges(candidate, "include").map((rule) => rule.reason), ...reviewedRanges(candidate, "exclude").map((rule) => rule.reason), ...((candidate.evidence?.observations || []).map((observation) => observation.description))])].filter(Boolean).join(" ").toLocaleLowerCase().includes(search); });
  }
  function renderEventList() {
    const events = visibleEvents(); $("event-list").replaceChildren(); $("visible-count").textContent = `${events.length}개`;
    let lastDay = null;
    events.forEach((event) => { if (event.day_key !== lastDay) { $("event-list").append(node("h3", "day-heading", event.day_key)); lastDay = event.day_key; } const counts = eventCounts(event); const button = node("button", `event-item${eventKey(event) === state.eventId ? " active" : ""}`); button.type = "button"; button.setAttribute("aria-current", eventKey(event) === state.eventId ? "true" : "false"); button.append(node("div", "event-item-title", event.title || event.event_id), node("div", "event-item-meta", `${event.candidates.length}개 후보 · 후보 길이 합 ${event.candidates.length ? time(event.candidates.reduce((sum, item) => sum + Math.max(0, item.end - item.start), 0)) : ""}`)); const countLine = node("div", "event-item-counts"); countLine.append(node("span", "", `채택 ${counts.selected}`), node("span", "", `미채택 ${counts.omitted}`)); if (counts.warnings) countLine.append(node("span", "attention", `확인 ${counts.warnings}`)); button.append(countLine); button.addEventListener("click", () => selectEvent(event)); $("event-list").append(button); });
    if (!events.length) $("event-list").append(node("p", "empty-events", "조건에 맞는 이벤트가 없습니다. 날짜나 검색 조건을 바꿔보세요."));
  }
  function renderHistory() {
    $("history-list").replaceChildren(); $("revision-label").textContent = `검토 버전 ${String(state.catalog.revision ?? 0).slice(0, 8)}`;
    (state.catalog.history || []).forEach((revision) => { const item = node("li", "history-item"); const date = node("time", "", stamp(revision.created_at)); if (revision.created_at) date.dateTime = revision.created_at; item.append(date, node("div", "", `#${String(revision.revision).slice(0, 8)} · ${{ include: "필수 포함", exclude: "제외", evidence: "관찰 기록" }[revision.action] || revision.action || "변경"}`)); const details = node("div"); details.append(node("p", "history-item-title", `${revision.clip_name || revision.clip_id || "원본"} · ${rangeText(revision.start, revision.end)}`), node("p", "history-item-reason", revision.reason || revision.summary || "")); item.append(details); $("history-list").append(item); });
    if (!(state.catalog.history || []).length) $("history-list").append(node("li", "history-empty", "아직 저장한 검토 기록이 없습니다. 원본을 확인하고 첫 구간을 기록해보세요."));
  }
  function renderCatalog(catalog) {
    state.catalog = catalog; state.events = (catalog.days || []).flatMap((day) => (day.events || []).map((event) => ({ ...event, day_key: event.day_key || day.day_key, candidates: event.candidates || [] }))).filter((event) => event.candidates.length);
    $("project-name").textContent = typeof catalog.project === "string" ? catalog.project : catalog.project?.name || "로컬 프로젝트"; document.title = `${$("project-name").textContent} · 이벤트 검토`;
    $("event-count").textContent = state.events.length; $("selected-count").textContent = state.events.reduce((sum, event) => sum + eventCounts(event).selected, 0); $("warning-count").textContent = state.events.reduce((sum, event) => sum + eventCounts(event).warnings, 0);
    const selectedDay = $("day-filter").value; $("day-filter").replaceChildren(new Option("모든 날짜", "")); [...new Set(state.events.map((event) => event.day_key))].forEach((day) => $("day-filter").add(new Option(day, day))); $("day-filter").value = selectedDay;
    const notes = [];
    if (catalog.pending_scan) notes.push("촬영 시각 등 스캔 설정이 변경되었습니다. 먼저 scan을 다시 실행한 뒤 analyze → plan을 실행하세요.");
    if (catalog.pending_reanalysis) notes.push("검토 규칙이 저장되어 있습니다. 분석(analyze)과 계획(plan)을 다시 실행해야 편집안에 반영됩니다.");
    if (catalog.plan_stale) notes.push("현재 편집안이 최신 분석과 일치하지 않을 수 있습니다. 채택 표시는 기존 편집안을 기준으로 합니다.");
    $("pipeline-note").textContent = notes.join(" ") || "검토 내용을 저장해도 영상은 자동으로 렌더되지 않습니다. 원본과 현재 편집안은 그대로 두고, 다음 분석·계획에 적용할 규칙을 저장합니다.";
    $("catalog-warnings").textContent = (catalog.warnings || []).map((warning) => typeof warning === "string" ? warning : warning.message || JSON.stringify(warning)).join("\n"); $("catalog-warnings").hidden = !(catalog.warnings || []).length;
    renderHistory(); renderEventList();
    const event = state.events.find((item) => eventKey(item) === state.eventId) || visibleEvents()[0] || state.events[0];
    if (event) selectEvent(event, true); else { stopPlayback(); state.candidate = null; $("event-workspace").hidden = true; $("empty-state").hidden = false; $("empty-state").querySelector("h2").textContent = "검토할 원본 구간이 없습니다."; $("empty-state").querySelector("p").textContent = "프로젝트의 스캔·분석 결과와 원본 연결을 확인한 뒤 새로고침하세요."; }
  }
  async function fetchCatalog() {
    $("refresh").disabled = true;
    try { const response = await fetch("/api/review", { cache: "no-store", credentials: "same-origin" }); const payload = await response.json(); if (!response.ok) throw new Error(payload.error || `검토 정보를 불러오지 못했습니다 (${response.status}).`); renderCatalog(payload); }
    catch (error) { alertStatus(`${error.message} 검토 서버가 실행 중인지 확인하세요.`, true); if (!state.catalog) { $("empty-state").querySelector("h2").textContent = "검토 정보를 불러오지 못했습니다."; $("empty-state").querySelector("p").textContent = "터미널의 검토 서버 주소로 다시 접속하거나 새로고침하세요."; } }
    finally { $("refresh").disabled = false; }
  }
  async function startPlayback(context) {
    if (!state.candidate) return;
    const range = context ? candidateRange(state.candidate, true) : selectedRange();
    if (!context && !validateRange()) { alertStatus("재생할 시작·끝 구간을 먼저 확인하세요.", true); return; }
    state.playback = { ...range, started: false }; $("playback-range").textContent = `${context ? "앞뒤 맥락" : "선택 구간"} · ${rangeText(range.start, range.end)}`; $("video-placeholder").hidden = true; $("playback-error").hidden = true;
    if (video.readyState >= 1) await playReadyRange(); else video.load();
  }
  async function playReadyRange() {
    const pending = state.playback; if (!pending || pending.started) return; pending.started = true;
    try { video.currentTime = pending.start; await video.play(); } catch (error) { if (error.name !== "AbortError") sourceFailure(error.name === "NotAllowedError" ? "브라우저가 재생을 차단했습니다. 영상의 재생 버튼을 직접 눌러주세요." : null); }
  }
  async function saveReview(event) {
    event.preventDefault(); if (state.saveInProgress || !state.candidate || !state.catalog) return;
    if (!validateRange()) { alertStatus("원본 길이 안에서 시작보다 큰 끝 시간을 입력하세요.", true); return; }
    const reason = $("review-reason").value.trim(); if (!reason) { $("review-reason").focus(); alertStatus("선택 이유나 확인한 내용을 입력하세요.", true); return; }
    if ([...reason].length > 160) { $("review-reason").focus(); alertStatus("선택 이유는 160자 이내로 입력하세요.", true); return; }
    const action = event.submitter?.value || "include"; const candidate = state.candidate; const range = selectedRange();
    const payload = { expected_revision: state.catalog.revision, action, clip_id: candidate.clip_id, start: range.start, end: range.end, reason };
    if (action === "evidence" || $("record-observation").checked) {
      const basis = $("observation-basis").value; const sourceCueIds = [...$("speech-cue-list").querySelectorAll("input:checked")].map((item) => item.value);
      if (basis === "speech" && !sourceCueIds.length) { alertStatus("발화 근거로 기록하려면 근거가 된 발화를 선택하세요.", true); return; }
      const context = candidateRange(candidate, true);
      payload.observation = { clip_id: candidate.clip_id, source_fingerprint: evidence(candidate).source_fingerprint, kind: $("observation-kind").value, basis, description: reason, core_range: range, context_range: { start: Math.min(context.start, range.start), end: Math.max(context.end, range.end) }, source_cue_ids: basis === "speech" ? sourceCueIds : [] };
    }
    state.saveInProgress = true; $("review-form").querySelectorAll("button,input,select,textarea").forEach((item) => { item.disabled = true; });
    try {
      const response = await fetch("/api/review", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Review-Token": state.catalog.csrf_token }, body: JSON.stringify(payload) });
      const updated = await response.json(); if (!response.ok) throw new Error(updated.error || (response.status === 409 ? "현재 상태와 요청이 충돌해 저장하지 못했습니다. 메모를 보관하고 최신 검토 상태를 확인하세요." : `저장하지 못했습니다 (${response.status}).`));
      state.dirty = false; renderCatalog(updated); alertStatus(action === "evidence" ? "관찰 내용을 저장했습니다. 원본이나 영상은 변경하지 않았습니다." : "검토 규칙을 저장했습니다. 분석(analyze) → 계획(plan)을 다시 실행하면 적용됩니다. 영상은 자동으로 렌더하지 않습니다.");
    } catch (error) { alertStatus(error.message, true); }
    finally { state.saveInProgress = false; $("review-form").querySelectorAll("button,input,select,textarea").forEach((item) => { item.disabled = false; }); if (state.candidate) renderRange(); }
  }
  Object.entries(kinds).forEach(([key, label]) => $("observation-kind").add(new Option(label, key))); Object.entries(bases).forEach(([key, label]) => $("observation-basis").add(new Option(label, key)));
  $("search").addEventListener("input", renderEventList); $("day-filter").addEventListener("change", renderEventList); $("status-filter").addEventListener("change", renderEventList);
  $("refresh").addEventListener("click", () => { if (allowedToDiscard()) fetchCatalog(); });
  $("play-candidate").addEventListener("click", () => startPlayback(false)); $("play-context").addEventListener("click", () => startPlayback(true));
  $("set-start").addEventListener("click", () => { if (video.readyState && state.candidate) { $("range-start").value = precise(video.currentTime); state.dirty = true; renderRange(); } });
  $("set-end").addEventListener("click", () => { if (video.readyState && state.candidate) { $("range-end").value = precise(video.currentTime); state.dirty = true; renderRange(); } });
  $("range-start").addEventListener("input", renderRange); $("range-end").addEventListener("input", renderRange);
  $("review-form").addEventListener("input", () => { state.dirty = true; }); $("review-form").addEventListener("change", () => { state.dirty = true; }); $("review-form").addEventListener("submit", saveReview);
  $("observation-basis").addEventListener("change", () => { $("speech-cues").hidden = $("observation-basis").value !== "speech"; });
  video.addEventListener("loadedmetadata", playReadyRange); video.addEventListener("error", () => { if (video.getAttribute("src")) sourceFailure(); });
  video.addEventListener("timeupdate", () => { if (state.playback && video.currentTime >= state.playback.end) { video.pause(); video.currentTime = state.playback.end; state.playback = null; } });
  video.addEventListener("play", () => { $("video-placeholder").hidden = true; if (!state.playback && state.candidate) { const range = selectedRange(); if (validateRange()) { state.playback = { ...range, started: true }; if (video.currentTime < range.start || video.currentTime >= range.end) video.currentTime = range.start; } } });
  window.addEventListener("beforeunload", (event) => { if (state.dirty || state.saveInProgress) { event.preventDefault(); event.returnValue = ""; } });
  fetchCatalog();
})();
