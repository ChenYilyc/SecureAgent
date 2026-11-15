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
let adminSequence = 0;
let workspaceSequence = 0;

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
  const requestUser = user;
  const headers = new Headers(options.headers);
  if (user && options.method && options.method !== "GET") {
    headers.set("x-wb-csrf", user.csrf);
  }
  const response = await fetch(base + "/api/" + route, {...options, headers, cache: "no-store"});
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401 && requestUser && user === requestUser) showLogin();
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
  adminSequence++;
  workspaceSequence++;
  byId("admin-panel").hidden = true;
  byId("admin-user-form").reset();
  byId("admin-project-form").reset();
  byId("admin-project-details").open = false;
  byId("admin-users").replaceChildren();
  byId("admin-projects").replaceChildren();
  byId("admin-create-grants").replaceChildren();
  byId("admin-user-submit").disabled = true;
  byId("workspace").hidden = true;
  byId("identity").hidden = true;
  byId("signin").hidden = false;
  for (const id of ["register", "preview", "results", "project"]) byId(id).replaceChildren();
  byId("question").value = "";
  byId("file").value = "";
}

function selectedGrants(container) {
  const grants = [];
  for (const row of container.querySelectorAll(".wb-grant-row")) {
    const checkbox = row.querySelector("input");
    if (checkbox.checked) grants.push([checkbox.value, row.querySelector("select").value]);
  }
  return Object.fromEntries(grants);
}

function projectLabel(id, name, state) {
  const label = name ? id + " · " + name : id;
  return state === "archived" ? label + "（已归档，只读）" : label;
}

function renderGrantChoices(container, data, grants) {
  container.replaceChildren();
  const selected = new Map(Object.entries(grants));
  for (const item of data.projects) {
    const row = element("div", null, "wb-grant-row");
    const label = element("label");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.value = item.project_id;
    checkbox.checked = selected.has(item.project_id);
    label.append(checkbox, element("span", projectLabel(item.project_id, item.display_name, item.state)));

    const role = element("select");
    role.setAttribute("aria-label", item.project_id + " 项目岗位");
    for (const [code, name] of Object.entries(data.roles)) role.add(new Option(name, code));
    role.value = selected.get(item.project_id) || "employee";
    role.disabled = !checkbox.checked;
    checkbox.onchange = () => { role.disabled = !checkbox.checked; };
    row.append(label, role);
    container.append(row);
  }
}

function accountActions(item, account, data) {
  const editor = element("details");
  editor.append(element("summary", "编辑工程岗位"));
  editor.ontoggle = () => {
    if (!editor.open || editor.querySelector("form")) return;
    const form = element("form", null, "stack");
    form.method = "post";
    const fieldset = element("fieldset", null, "wb-grant-fieldset");
    const choices = element("div", null, "wb-grant-list");
    fieldset.append(element("legend", account.login + " 的工程岗位"), choices);
    renderGrantChoices(choices, data, account.project_roles);
    const save = element("button", "保存岗位");
    save.type = "submit";
    form.append(fieldset, element("p", "取消勾选会移出对应工程，账号仍可登录。", "hint"), save);
    form.onsubmit = handle(async () => {
      const operator = user;
      await api("admin/users/memberships", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          login: account.login,
          expected_revision: account.access_revision,
          project_roles: selectedGrants(choices)
        })
      });
      if (user !== operator) return;
      await refreshAdmin();
      if (user === operator) notify("已保存 " + account.login + " 的工程岗位。");
    });
    editor.append(form);
  };

  const remove = element("button", "删除账号", "link wb-delete");
  remove.type = "button";
  remove.setAttribute("aria-label", "删除账号 " + account.login);
  remove.onclick = handle(async () => {
    const message = "删除业务账号“" + account.login + "”？全部工程授权和登录会话将撤销，用户名和历史记录将保留。";
    if (!confirm(message)) return;
    const operator = user;
    const query = new URLSearchParams({login: account.login, expected_revision: String(account.access_revision)});
    await api("admin/users?" + query, {method: "DELETE"});
    if (user !== operator) return;
    await refreshAdmin();
    if (user === operator) notify("已删除业务账号 " + account.login + "。");
  });
  item.append(editor, remove);
}

function projectAction(item, action) {
  const archive = action === "archive";
  const label = archive ? "归档工程" : "删除工程";
  const button = element("button", label, archive ? "link" : "link wb-delete");
  button.type = "button";
  button.setAttribute("aria-label", label + " " + item.project_id);
  button.onclick = handle(async () => {
    const operator = user;
    const effect = archive
      ? "成员仍可查阅资料，上传和审批将关闭。"
      : "该工程将从工作台移除，全部工程成员授权将撤销。原件、操作记录和编号将保留。";
    if (!confirm(label + "“" + item.display_name + "”（" + item.project_id + "）？" + effect)) return;
    const query = new URLSearchParams({expected_revision: String(item.lifecycle_revision)});
    const route = "admin/projects/" + encodeURIComponent(item.project_id) + (archive ? "/archive" : "");
    await api(route + "?" + query, {method: archive ? "POST" : "DELETE"});
    if (user !== operator) return;
    if (operator.projects.includes(item.project_id)) {
      await openWorkspace(project);
    } else {
      await refreshAdmin();
    }
    if (user?.login === operator.login) notify(item.project_id + (archive ? " 已归档。" : " 已删除。"));
  });
  return button;
}

function renderProjects(projects) {
  const list = byId("admin-projects");
  list.replaceChildren();
  for (const item of projects) {
    const row = element("li", null, "hit");
    const heading = element("div", null, "section-heading");
    const archived = item.state === "archived";
    heading.append(element("strong", item.project_id + " · " + item.display_name),
                   element("span", archived ? "已归档 · 只读" : "进行中", "status"));
    const actions = element("div", null, "actions");
    if (!archived) actions.append(projectAction(item, "archive"));
    actions.append(projectAction(item, "delete"));
    row.append(heading, actions);
    list.append(row);
  }
}

async function refreshAdmin(preferredProject = "") {
  const operator = user;
  if (!operator || !operator.is_company_admin) return;
  const sequence = ++adminSequence;
  const data = await api("admin/users");
  if (sequence !== adminSequence || user !== operator) return;

  renderProjects(data.projects);
  const choices = byId("admin-create-grants");
  const draft = selectedGrants(choices);
  if (preferredProject && !Object.hasOwn(draft, preferredProject)) {
    draft[preferredProject] = "employee";
  }
  renderGrantChoices(choices, data, draft);
  const hasProjects = data.projects.length > 0;
  byId("admin-user-submit").disabled = !hasProjects;
  byId("admin-project-hint").hidden = hasProjects;
  if (!hasProjects) byId("admin-project-details").open = true;

  const list = byId("admin-users");
  list.replaceChildren();
  for (const account of data.users) {
    const item = element("li", null, "hit");
    const state = account.is_active ? "已启用" : "已停用";
    item.append(element("strong", account.login + " · " + state));
    const grants = Object.entries(account.project_roles).map(
      ([code, role]) => code + " · " + data.roles[role]
    );
    if (account.is_company_admin) grants.unshift("公司管理员");
    item.append(element("p", grants.join(" / ") || "尚未分配项目岗位", "hint"));
    if (!account.is_company_admin) accountActions(item, account, data);
    list.append(item);
  }
}

async function openWorkspace(preferredProject = null) {
  const sequence = ++workspaceSequence;
  const profile = await api("me");
  if (sequence !== workspaceSequence) return;
  const requested = preferredProject || urlState.get("project");
  const next = profile.projects.includes(requested) ? requested : (profile.projects[0] || null);
  const changed = user?.login !== profile.login || project !== next ||
    user?.project_roles[next] !== profile.project_roles[next] ||
    user?.project_states[next] !== profile.project_states[next];
  user = profile;
  if (changed) {
    previewSequence++;
    searchSequence++;
    for (const id of ["register", "preview", "results"]) byId(id).replaceChildren();
    byId("file").value = "";
    byId("question").value = "";
  }
  byId("signin").hidden = true;
  byId("identity").hidden = false;
  byId("workspace").hidden = user.projects.length === 0;
  byId("who").textContent = user.login + " · " + user.role_label;
  const operator = user;
  byId("admin-panel").hidden = !operator.is_company_admin;
  if (operator.is_company_admin) {
    try {
      await refreshAdmin();
    } catch (err) {
      if (user === operator) notify(err.message, true);
    }
    if (user !== operator) return;
  }
  const select = byId("project");
  select.replaceChildren();
  if (user.projects.length === 0) {
    project = null;
    if (!user.is_company_admin) notify("当前账号尚未分配项目岗位。");
    return;
  }
  for (const id of user.projects) {
    select.append(new Option(projectLabel(id, user.project_names[id], user.project_states[id]), id));
  }
  project = next;
  if (requested && requested !== project) notify("该工程已不可访问，已切换到 " + project + "。");
  select.value = project;
  await refresh();
  if (sequence !== workspaceSequence || user !== operator) return;
  if (!preferredProject && urlState.get("project") === project && urlState.has("document") && urlState.has("revision")) {
    await preview(urlState.get("document"), urlState.get("revision"), urlState.get("section"));
  }
}

function badge(status) {
  return element("span", statusNames[status] || status, "status " + status);
}

async function refresh() {
  const currentUser = user;
  const requestedProject = project;
  const role = user.project_roles[requestedProject];
  const archived = user.project_states[requestedProject] === "archived";
  byId("archived-note").hidden = !archived;
  byId("receiving").hidden = archived || role !== "clerk";
  byId("panels").prepend(byId(role === "employee" ? "question-panel" : "document-panel"));
  byId("qa-mode").textContent = user.qa_projects.includes(requestedProject)
    ? "有候选条款时，DeepSeek 会核对问题和相关现行原文。"
    : "按相关性列出本项目的现行条款。";
  byId("upload-hint").textContent = "Word、文本 PDF 或 Markdown，每份限 " +
    (user.upload_limit / (1024 * 1024)) + " MiB。";
  const rows = await api(encodeURIComponent(requestedProject) + "/versions");
  if (requestedProject !== project || currentUser !== user) return;
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
  if (user.project_states[requestedProject] === "active" &&
      user.project_roles[requestedProject] === "reviewer" && result.status === "draft") {
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

byId("admin-refresh").onclick = handle(() => refreshAdmin());

byId("admin-project-form").onsubmit = handle(async () => {
  const operator = user;
  const result = await post("admin/projects", {
    project_id: byId("admin-project-id").value.trim(),
    display_name: byId("admin-project-name").value.trim()
  });
  if (user !== operator) return;
  byId("admin-project-form").reset();
  byId("admin-project-details").open = false;
  await refreshAdmin(result.project_id);
  if (user === operator) notify("已创建工程 " + result.project_id + "。");
});

byId("admin-user-form").onsubmit = handle(async () => {
  const operator = user;
  const choices = byId("admin-create-grants");
  const grants = selectedGrants(choices);
  if (Object.keys(grants).length === 0) throw new Error("请至少勾选一个所属工程。");
  const result = await post("admin/users", {
    login: byId("admin-user-login").value.trim(),
    password: byId("admin-user-password").value,
    project_roles: grants
  });
  if (user !== operator) return;
  byId("admin-user-password").value = "";
  byId("admin-user-login").value = "";
  choices.replaceChildren();
  await refreshAdmin();
  if (user === operator) notify("已创建用户 " + result.login + "。");
});

byId("refresh").onclick = handle(() => openWorkspace(project));
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
  if (!file || file.size > user.upload_limit) throw new Error(byId("upload-hint").textContent);
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
  for (const message of result.notices || []) box.append(element("p", message, "hint"));
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
    box.append(element("h3", "候选条款"));
    if (!result.results.length) box.append(element("p", "当前项目没有检索到相关现行条款。", "hint"));
    for (const row of result.results) box.append(citationHit(row));
  }
});

openWorkspace().catch(err => {
  showLogin();
  if (err.message !== "请先登录。") notify(err.message, true);
});
