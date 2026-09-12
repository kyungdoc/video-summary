"use strict";

// Dependency-free DOM double: tests state and requests, not browser layout/codecs.
// Run with: node --test tests/review_frontend.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticRoot = path.join(__dirname, "..", "video_summary", "review_static");
const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
const source = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
const flush = () => new Promise(setImmediate);

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toLowerCase();
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.readyState = 1;
    this.currentTime = 0;
    this.textContent = "";
    this.paused = true;
    this.classList = {
      toggle: (name, enabled) => {
        const values = new Set(this.className.split(" ").filter(Boolean));
        const add = enabled === undefined ? !values.has(name) : enabled;
        if (add) values.add(name);
        else values.delete(name);
        this.className = [...values].join(" ");
      },
    };
  }

  set innerHTML(_) { throw new Error("Do not interpret catalog data as HTML"); }
  set outerHTML(_) { throw new Error("Do not interpret catalog data as HTML"); }

  append(...children) {
    for (const child of children) {
      this.children.push(child);
      child.parent = this;
    }
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = value;
    if (name === "id") this.id = value;
  }

  getAttribute(name) {
    return name === "src" ? this.src || null : this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "src") this.src = "";
  }

  addEventListener(name, handler) {
    (this.listeners[name] ??= []).push(handler);
  }

  async fire(name, event = {}) {
    for (const handler of this.listeners[name] || []) await handler(event);
  }

  querySelectorAll(selector) {
    const selectors = selector.split(",");
    const matches = (element) => selectors.some((part) => {
      if (part === "input:checked") return element.tagName === "input" && element.checked;
      if (part.startsWith(".")) return element.className.split(" ").includes(part.slice(1));
      return element.tagName === part;
    });
    const result = [];
    const walk = (element) => {
      for (const child of element.children) {
        if (matches(child)) result.push(child);
        walk(child);
      }
    };
    walk(this);
    return result;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  add(option) { this.append(option); if (!this.value) this.value = option.value; }
  focus() { this.focused = true; }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
  }
  pause() { this.paused = true; }
  load() { this.loads = (this.loads || 0) + 1; }
  async play() { this.paused = false; await this.fire("play"); }
}

function parseMarkup() {
  const root = new Element();
  const nodes = {};
  const stack = [root];
  const voidTags = new Set(["meta", "link", "input", "img", "br", "hr", "source"]);
  for (const [, close, tag, attributes] of html.matchAll(/<(\/)?([a-z][\w-]*)\b([^>]*)>/gi)) {
    if (close) {
      for (let index = stack.length - 1; index > 0; index--) {
        if (stack[index].tagName === tag.toLowerCase()) {
          stack.length = index;
          break;
        }
      }
      continue;
    }
    const element = new Element(tag);
    for (const [, key, value] of attributes.matchAll(/([\w-]+)="([^"]*)"/g)) {
      element.setAttribute(key, value);
    }
    if (element.id) nodes[element.id] = element;
    stack.at(-1).append(element);
    if (!voidTags.has(element.tagName)) stack.push(element);
  }
  return { root, nodes };
}

function fixture() {
  const candidate = {
    candidate_id: "candidate-1", clip_id: "clip-1", clip_name: "phone/meal.MOV",
    source_kind: "phone", start: 2.000123, end: 6.001456,
    source_duration: 10.123456, selected: false, transcript: "식사 장면",
    captured_at: "2026-09-01T09:00:00+09:00", capture_time_confidence: "high",
    sequence_at: "2026-09-01T08:30:00+09:00", sequence_source: "trusted_anchor",
    media_url: "/media/clip-1", story_stage: "body",
    evidence: {
      source_fingerprint: "fingerprint-1",
      suggested_core_range: { start: 2.000123, end: 6.005456 },
      context_range: { start: 0, end: 10.123456 },
      flags: ["meal_body_needs_visual_review"], observations: [],
      classifications: [{ kind: "meal_body", reason: "food word", basis: "inferred" }],
      transcript_cues: [{ cue_id: "cue-1", start: 2.000123, end: 6.005456, text: "잘 먹겠습니다" }],
    },
  };
  const catalog = {
    project: "검토 테스트", revision: "revision-1", csrf_token: "test-csrf",
    days: [{ day_key: "2026-09-01", events: [{ event_id: "event-1", title: "식사", candidates: [candidate] }] }],
    history: [], warnings: [],
  };
  return { candidate, catalog };
}

function response(payload, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}

async function openReview(catalog, onPost) {
  const { root, nodes } = parseMarkup();
  const requests = [];
  const context = {
    document: {
      getElementById: (id) => nodes[id],
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => { const element = new Element("#text"); element.textContent = text; return element; },
      querySelectorAll: (selector) => root.querySelectorAll(selector),
    },
    window: { confirm: () => true, addEventListener() {} },
    location: { origin: "http://127.0.0.1:8800" },
    Option: function Option(text, value) { const element = new Element("option"); element.textContent = text; element.value = value; return element; },
    URL,
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (options.method === "POST" && onPost) return onPost(JSON.parse(options.body));
      return response(catalog);
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "review_static/app.js" });
  await flush();
  return {
    nodes, root, requests,
    submit: (action) => nodes["review-form"].fire("submit", { preventDefault() {}, submitter: { value: action } }),
    posts: () => requests.filter((request) => request.options.method === "POST"),
  };
}

test("all JavaScript IDs exist and catalog content is never parsed as HTML", () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size);
  for (const [, id] of source.matchAll(/\$\("([^"]+)"\)/g)) assert(ids.includes(id), id);
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(/);
  assert.doesNotMatch(html, /\sstyle=|<script[^>]+src="https?:|<link[^>]+href="https?:/);
});

test("defaults preserve exact suggested core and fall back to exact original range", async () => {
  const { candidate, catalog } = fixture();
  let review = await openReview(catalog);
  assert.equal(Number(review.nodes["range-start"].value), candidate.evidence.suggested_core_range.start);
  assert.equal(Number(review.nodes["range-end"].value), candidate.evidence.suggested_core_range.end);
  assert.equal(review.nodes["range-start"].getAttribute("step"), "any");
  delete candidate.evidence.suggested_core_range;
  review = await openReview(catalog);
  assert.equal(Number(review.nodes["range-start"].value), candidate.start);
  assert.equal(Number(review.nodes["range-end"].value), candidate.end);
});

test("source and context playback seek to start and pause at their own end", async () => {
  const { candidate, catalog } = fixture();
  const { nodes } = await openReview(catalog);
  const video = nodes["source-video"];
  await nodes["play-candidate"].fire("click");
  assert.equal(video.currentTime, candidate.evidence.suggested_core_range.start);
  video.currentTime = 6.1;
  await video.fire("timeupdate");
  assert(video.paused);
  assert.equal(video.currentTime, candidate.evidence.suggested_core_range.end);
  await nodes["play-context"].fire("click");
  assert.equal(video.currentTime, 0);
  video.currentTime = 11;
  await video.fire("timeupdate");
  assert(video.paused);
  assert.equal(video.currentTime, candidate.source_duration);
  video.currentTime = candidate.source_duration;
  await nodes["set-end"].fire("click");
  assert(Number(nodes["range-end"].value) <= candidate.source_duration);
});

test("include sends exact source coordinates, CSRF and expected revision without implicit observation", async () => {
  const { candidate, catalog } = fixture();
  const review = await openReview(catalog);
  review.nodes["review-reason"].value = "가족이 실제로 식사하는 장면";
  await review.submit("include");
  assert.equal(review.posts().length, 1);
  const request = review.posts()[0];
  assert.equal(request.url, "/api/review");
  assert.equal(request.options.headers["X-Review-Token"], "test-csrf");
  assert.equal(request.options.credentials, "same-origin");
  const action = JSON.parse(request.options.body);
  assert.equal(action.expected_revision, "revision-1");
  assert.equal(action.action, "include");
  assert.equal(action.clip_id, candidate.clip_id);
  assert.equal(action.start, candidate.evidence.suggested_core_range.start);
  assert.equal(action.end, candidate.evidence.suggested_core_range.end);
  assert(!Object.hasOwn(action, "observation"));
});

test("speech observations require selected in-range cue IDs", async () => {
  const { catalog } = fixture();
  const review = await openReview(catalog);
  review.nodes["review-reason"].value = "식사 발화";
  review.nodes["observation-basis"].value = "speech";
  await review.submit("evidence");
  assert.equal(review.posts().length, 0);
  assert.match(review.nodes.status.textContent, /발화를 선택/);
  const cue = review.nodes["speech-cue-list"].querySelector("input");
  assert.equal(cue.disabled, false);
  cue.checked = true;
  await review.submit("evidence");
  const action = JSON.parse(review.posts()[0].options.body);
  assert.equal(action.observation.basis, "speech");
  assert.deepEqual(action.observation.source_cue_ids, ["cue-1"]);
  assert.equal(action.observation.source_fingerprint, "fingerprint-1");
  review.nodes["range-start"].value = 3;
  await review.nodes["range-start"].fire("input");
  const updatedCue = review.nodes["speech-cue-list"].querySelector("input");
  assert(updatedCue.disabled);
  assert.equal(updatedCue.checked, false);
});

test("untrusted filenames and descriptions remain literal and remote media URLs are rejected", async () => {
  const { candidate, catalog } = fixture();
  const injection = '<img src=x onerror="window.pwned=true">';
  catalog.project = injection;
  candidate.clip_name = injection;
  candidate.transcript = injection;
  candidate.media_url = "https://untrusted.example/media/private";
  candidate.frame_url = "https://untrusted.example/frame/private";
  candidate.evidence.observations = [{ basis: "inferred", description: injection }];
  const { nodes, root, requests } = await openReview(catalog);
  assert.equal(nodes["project-name"].textContent, injection);
  assert.equal(nodes["clip-name"].textContent, injection);
  assert.equal(nodes.transcript.textContent, injection);
  assert.equal(root.querySelectorAll("img").length, 0);
  assert.equal(nodes["source-video"].getAttribute("src"), null);
  assert.equal(nodes["playback-error"].hidden, false);
  assert.equal(nodes["playback-error"].querySelectorAll("a").length, 0);
  assert(requests.every((request) => request.url === "/api/review"));
  assert.doesNotMatch(nodes["evidence-status"].textContent, /화면 관찰/);
});

test("a 409 preserves the actual conflict message and unsaved form", async () => {
  const { catalog } = fixture();
  const reason = "이 구간을 포함하고 싶습니다";
  const backendMessage = "반대되는 제외 규칙과 겹칩니다. 기존 규칙을 먼저 확인하세요.";
  const review = await openReview(catalog, () => response({ error: backendMessage }, 409));
  review.nodes["review-reason"].value = reason;
  await review.submit("include");
  assert.equal(review.nodes.status.textContent, backendMessage);
  assert.equal(review.nodes["review-reason"].value, reason);
  assert.equal(review.nodes["save-include"].disabled, false);
});

test("save in progress prevents event changes and refresh requests", async () => {
  const { candidate, catalog } = fixture();
  const other = { ...candidate, candidate_id: "candidate-2", clip_id: "clip-2", clip_name: "other.MOV" };
  catalog.days[0].events.push({ event_id: "event-2", title: "다음 이벤트", candidates: [other] });
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const review = await openReview(catalog, () => pending);
  review.nodes["review-reason"].value = "식사 보존";
  const save = review.submit("include");
  await flush();
  const buttons = review.nodes["event-list"].querySelectorAll("button");
  await buttons[1].fire("click");
  await review.nodes.refresh.fire("click");
  assert.equal(review.nodes["clip-name"].textContent, candidate.clip_name);
  assert.equal(review.requests.length, 2);
  finish(response(catalog));
  await save;
  assert.equal(review.nodes["save-include"].disabled, false);
});

test("search includes pending include/exclude rules and plan/exclusion reasons", async () => {
  const { candidate, catalog } = fixture();
  candidate.reason = "라운지에서 만난 친구";
  candidate.exclusion_reason = "개인정보 보호";
  candidate.reviewed_inclusion_reason = "동물원 관찰";
  candidate.reviewed_ranges = {
    include: [{ start: 2, end: 3, reason: "뽑기 결과 보존" }],
    exclude: [{ start: 4, end: 5, reason: "탈의 장면 제외" }],
  };
  const review = await openReview(catalog);
  for (const query of ["만난 친구", "개인정보", "동물원", "뽑기", "탈의"]) {
    review.nodes.search.value = query;
    await review.nodes.search.fire("input");
    assert.equal(review.nodes["visible-count"].textContent, "1개", query);
  }
  review.nodes.search.value = "이 문장은 존재하지 않습니다";
  await review.nodes.search.fire("input");
  assert.equal(review.nodes["visible-count"].textContent, "0개");
});

test("unsupported source playback yields a visible fallback without a loading overlay", async () => {
  const { catalog } = fixture();
  const { nodes } = await openReview(catalog);
  await nodes["source-video"].fire("error");
  assert.equal(nodes["playback-error"].hidden, false);
  assert.equal(nodes["video-placeholder"].hidden, true);
  assert.equal(nodes["playback-error"].querySelector("a").href, "/media/clip-1");
});
