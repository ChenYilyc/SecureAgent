"use strict";

const base = "/workbench";
const byId = id => document.getElementById(id);
const statusNames = {draft: "草稿", effective: "现行", superseded: "历史版"};
const eventNames = {
  "received:draft": "提交草稿",
  "received:effective": "登记现行版",
  "received:superseded": "登记历史版",
  approved: "审批通过",
  superseded: "转为历史版"
};
const urlState = new URLSearchParams(location.search);
let user = null;
let project = null;
let previewSequence = 0;
let searchSequence = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notify(message = "", error = false) {
  byId("notice").textContent = message;
  byId("notice").classList.toggle("error", error);
}

async function api(route, options = {}) {
  const headers = new Headers(options.headers);
  if (user && options.method && options.method !== "GET") {
    headers.set("x-wb-csrf", user.csrf);
  }
  const response = await fetch(base + "/api/" + route, {...options, headers, cache: "no-store"});
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401 && user) showLogin();
    throw new Error(typeof value.detail === "string" ? value.detail : "请求未通过校验。");
  }
  return value;
}

function post(route, value, headers = {}) {
  return api(route, {
    method: "POST", headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(value)
  });
}

function handle(action) {
  return async event => {
    event.preventDefault();
    const button = event.submitter || (event.currentTarget.tagName === "BUTTON" ? event.currentTarget : null);
    if (button) button.disabled = true;
    try {
      notify();
      await action(event);
    } catch (err) {
      notify(err.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  };
}

function showLogin() {
  user = null;
  project = null;
  previewSequence++;
  searchSequence++;
  byId("workspace").hidden = true;
  byId("identity").hidden = true;
  byId("signin").hidden = false;
  for (const id of ["register", "preview", "results", "project"]) byId(id).replaceChildren();
  byId("question").value = "";
  byId("file").value = "";
}

async function openWorkspace() {
  user = await api("me");
  byId("signin").hidden = true;
  byId("identity").hidden = false;
  byId("workspace").hidden = false;
  byId("who").textContent = user.login + " · " + user.role_label;
  const select = byId("project");
  select.replaceChildren();
  for (const id of user.projects) {
    const name = user.project_names[id];
    select.append(new Option(typeof name === "string" ? id + " · " + name : id, id));
  }
  project = user.projects.includes(urlState.get("project")) ? urlState.get("project") : user.projects[0];
  select.value = project;
  byId("receiving").hidden = user.role !== "clerk";
  byId("panels").prepend(byId(user.role === "employee" ? "question-panel" : "document-panel"));
  await refresh();
  if (urlState.get("project") === project && urlState.has("document") && urlState.has("revision")) {
    await preview(urlState.get("document"), urlState.get("revision"), urlState.get("section"));
  }
}

function badge(status) {
  return element("span", statusNames[status] || status, "status " + status);
}

async function refresh() {
  const requestedProject = project;
  byId("qa-mode").textContent = user.qa_projects.includes(requestedProject)
    ? "有候选条款时，DeepSeek 会核对问题和相关现行原文。"
    : "本项目在本机检索，按相关性列出现行条款。";
  const rows = await api(encodeURIComponent(requestedProject) + "/versions");
  if (requestedProject !== project || !user) return;
  const list = byId("register");
  list.replaceChildren();
  byId("listnote").textContent = rows.length + " 个版本";
  for (const row of rows) {
    const button = element("button", null, "item");
    const meta = element("span", null, "meta");
    meta.append(element("span", row.document_id + " · " + row.revision), badge(row.status));
    button.append(meta, element("strong", row.title));
    button.onclick = handle(() => preview(row.document_id, row.revision));
    list.append(button);
  }
}

async function preview(doc, revision, focus) {
  const sequence = ++previewSequence;
  const requestedProject = project;
  const route = [project, doc, revision].map(encodeURIComponent).join("/");
  const result = await api(route + "/preview");
  if (sequence !== previewSequence || !user) return;

  const box = byId("preview");
  box.replaceChildren();
  const meta = element("div", null, "meta");
  meta.append(element("span", [project, doc, revision].join(" / ")), badge(result.status));
  box.append(meta, element("h3", result.title, "preview-title"));

  const clauses = element("div", null, "clauses");
  let page;
  for (const section of result.sections) {
    const active = focus === (section.fragment_id || section.section);
    const item = element("div", null, active ? "clause focus" : "clause");
    item.append(element("strong", "§" + section.section + " " + section.section_title));
    item.append(element("p", section.text, "excerpt"), element("small", section.location));
    clauses.append(item);
    if (active) page = section.source_page;
  }
  box.append(clauses);

  const actions = element("div", null, "actions");
  const original = element("a", result.extension === ".docx" ? "下载 Word 原件" : "打开归档原件");
  original.href = base + "/api/" + route + "/original" + (page ? "#page=" + page : "");
  original.target = "_blank";
  original.rel = "noopener";
  actions.append(original);
  if (user.role === "reviewer" && result.status === "draft") {
    const approve = element("button", "批准并发布");
    approve.onclick = handle(async () => {
      const previous = result.current ? " 当前版 " + result.current + " 将转为历史版。" : "";
      if (!confirm("将 " + doc + " " + revision + " 发布为现行版本？" + previous)) return;
      await post(route + "/approve", {sha256: result.sha256, expected_current: result.current});
      if (requestedProject !== project || !user) return;
      await refresh();
      if (sequence !== previewSequence) return;
      await preview(doc, revision);
      byId("results").replaceChildren();
      notify("已批准，现行条款索引已更新。");
    });
    actions.append(approve);
  }
  box.append(actions);
  if (result.events.length) {
    const history = element("details");
    history.append(element("summary", "操作记录（UTC）"));
    for (const event of result.events) {
      history.append(element("p", [event.created_utc, event.actor, eventNames[event.action] || event.action].join(" · "), "hint"));
    }
    box.append(history);
  }
}

byId("login").onsubmit = handle(async () => {
  await post("login", {login: byId("account").value, password: byId("password").value}, {"x-wb-login": "1"});
  byId("password").value = "";
  await openWorkspace();
});

byId("logout").onclick = handle(async () => {
  await post("logout", {});
  showLogin();
  notify("已退出。");
});

byId("refresh").onclick = handle(refresh);
byId("project").onchange = handle(async () => {
  project = byId("project").value;
  previewSequence++;
  searchSequence++;
  byId("question").value = "";
  byId("preview").replaceChildren();
  byId("results").replaceChildren();
  await refresh();
});

byId("file").onchange = () => {
  const file = byId("file").files[0];
  const match = file?.name.match(/^(.+)_(R\d+)\.(docx|pdf|md)$/i);
  if (match) {
    byId("document").value = match[1];
    byId("revision").value = match[2].toUpperCase();
  }
};

byId("upload").onsubmit = handle(async () => {
  const requestedProject = project;
  const file = byId("file").files[0];
  if (!file || file.size > 8 * 1024 * 1024) throw new Error("请选择不超过 8 MiB 的原件。");
  const params = new URLSearchParams({
    document: byId("document").value.trim(), revision: byId("revision").value.trim(),
    format: file.name.split(".").pop()
  });
  const result = await api(encodeURIComponent(project) + "/upload?" + params, {
    method: "POST", body: file, headers: {"Content-Type": "application/octet-stream"}
  });
  if (requestedProject !== project || !user) return;
  await refresh();
  if (requestedProject !== project || !user) return;
  await preview(result.document_id, result.revision);
  notify(result.created ? "草稿已登记，可以核对条款和原件。" : "该版本及原件已登记，未重复创建。");
});

function citationHit(row) {
  const hit = element("div", null, "hit");
  hit.append(element("p", row.text, "excerpt"));
  const link = element("a", [row.document_id, row.revision, "§" + row.section, row.location].join(" · "));
  link.href = row.preview_url;
  link.onclick = handle(async () => {
    await preview(row.document_id, row.revision, row.fragment_id || row.section);
    byId("preview").scrollIntoView({block: "nearest"});
  });
  hit.append(link);
  return hit;
}

byId("search").onsubmit = handle(async () => {
  const sequence = ++searchSequence;
  const box = byId("results");
  box.replaceChildren();
  const result = await post(encodeURIComponent(project) + "/ask", {question: byId("question").value});
  if (sequence !== searchSequence) return;
  if (result.mode === "answer") {
    for (const answer of result.answers) {
      const group = element("div", null, "answer-part");
      group.append(element("h3", answer.question));
      if (!answer.citations.length) {
        group.append(element("p", "依据不足，现行候选条款未能完整回答这一项。", "hint"));
      }
      for (const row of answer.citations) group.append(citationHit(row));
      box.append(group);
    }
  } else {
    box.append(element("h3", "本地检索结果"));
    if (!result.results.length) box.append(element("p", "当前项目没有检索到相关现行条款。", "hint"));
    for (const row of result.results) box.append(citationHit(row));
  }
});

openWorkspace().catch(err => {
  showLogin();
  if (err.message !== "请先登录。") notify(err.message, true);
});
