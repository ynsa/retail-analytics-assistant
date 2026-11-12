// Local test UI (spec 40). Plain JavaScript, no framework, no build step.
// The page only shows what the server sends. Every rule (user scope, SQL guard, typed
// confirmation, report owner) runs on the server, in the same session as the CLI.

const state = {
  users: [],
  user: null,
  conversationId: null,
  busy: false,
  selectedTrace: null,
  tab: "conversations",
};

const EXAMPLES = [
  "What was the revenue of my brands last month?",
  "Top 5 products by revenue in Q3 2026",
  "Create a report for Q3 2026 with action items, as an HTML file.",
];

const $ = (id) => document.getElementById(id);

// --- small helpers ---------------------------------------------------------------------------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(
      child instanceof Node ? child : document.createTextNode(String(child)),
    );
  }
  return node;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `The server answered ${response.status}.`);
  }
  return data;
}

function userQuery() {
  return `user=${encodeURIComponent(state.user.id)}`;
}

function shortDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const day = date.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });
  const time = date.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${day} ${time}`;
}

function clockText(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function durationText(seconds) {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage can be off. The page works without it.
  }
}

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function scrollToEnd() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

// --- 1. start screen -------------------------------------------------------------------------
async function showStart() {
  state.user = null;
  state.conversationId = null;
  history.replaceState(null, "", "/");
  $("chat").hidden = true;
  $("start").hidden = false;
  const list = $("user-list");
  list.replaceChildren();
  const last = recall("opsfleet.user");
  for (const user of state.users) {
    const input = el("input", {
      type: "radio",
      name: "user",
      value: user.id,
      checked: user.id === last,
    });
    const role = user.role === "ceo" ? "CEO" : "Brand manager";
    list.append(
      el(
        "label",
        { className: "user-option" },
        input,
        el(
          "span",
          {},
          el("span", { className: "user-option-name", text: firstName(user) }),
          el("span", {
            className: "user-option-meta",
            text: `${role} · ${user.scope}`,
          }),
        ),
      ),
    );
  }
  $("start-button").disabled = !list.querySelector("input:checked");
}

function firstName(user) {
  return user.name.split(" (")[0];
}

async function startConversation(userId) {
  const result = await api("/api/conversations", { body: { user: userId } });
  remember("opsfleet.user", userId);
  state.user = result.user;
  await openChat(result.conversation_id, []);
}

// --- 2. chat ---------------------------------------------------------------------------------
async function openChat(conversationId, turns, pending = null) {
  state.conversationId = conversationId;
  state.selectedTrace = null;
  history.replaceState(null, "", `#/${state.user.id}/${conversationId}`);
  $("start").hidden = true;
  $("chat").hidden = false;
  renderHeader();
  renderMessages(turns, pending);
  clearSteps();
  setBusy(false);
  await Promise.all([refreshConversations(), refreshReports()]);
  const answers = turns.filter((t) => t.trace_id);
  if (answers.length) selectAnswer(answers[answers.length - 1].trace_id);
  $("ask-input").focus();
}

async function openConversation(conversationId, focusReport = null) {
  const data = await api(
    `/api/conversations/${encodeURIComponent(conversationId)}?${userQuery()}`,
  );
  await openChat(data.id, data.turns, data.pending);
  if (focusReport) flashReport(focusReport);
}

function renderHeader() {
  const user = state.user;
  $("user-name").textContent = firstName(user);
  const chips = [el("span", { className: "chip role", text: roleText(user) })];
  if (user.role === "ceo")
    chips.push(el("span", { className: "chip", text: "All brands" }));
  for (const brand of user.brands)
    chips.push(el("span", { className: "chip", text: brand }));
  $("user-chips").replaceChildren(...chips);
  $("conversation-id").textContent = state.conversationId;
}

function roleText(user) {
  return user.role === "ceo" ? "CEO" : "Brand manager";
}

function renderMessages(turns, pending) {
  const box = $("messages");
  box.replaceChildren();
  if (!turns.length) box.append(emptyState());
  for (const turn of turns) {
    box.append(userMessage(turn.question));
    box.append(answerMessage(turn));
  }
  if (pending) box.append(confirmMessage(pending));
  scrollToEnd();
}

function emptyState() {
  const examples = EXAMPLES.map((text) =>
    el("button", {
      className: "example",
      type: "button",
      text,
      onclick: () => {
        $("ask-input").value = text;
        $("ask-input").focus();
      },
    }),
  );
  return el(
    "div",
    { className: "empty", id: "empty" },
    el("p", { text: `Ask a question about the sales of ${scopeText()}.` }),
    el("div", { className: "examples" }, examples),
  );
}

function scopeText() {
  return state.user.role === "ceo"
    ? "all brands"
    : state.user.brands.join(" and ");
}

function userMessage(text) {
  return el(
    "div",
    { className: "message user" },
    el("div", { className: "bubble", text }),
  );
}

function answerMessage(view) {
  const wrap = el("div", {
    className: "message",
    dataset: { trace: view.trace_id || "" },
  });
  fillAnswer(wrap, view);
  return wrap;
}

function fillAnswer(wrap, view) {
  wrap.replaceChildren();
  if (view.confirmation) {
    wrap.append(confirmCard(view.confirmation));
    return;
  }
  const answer = el("div", { className: "answer" });
  if (view.label) {
    answer.append(
      el("span", {
        className: `label ${view.label.tone}`,
        text: view.label.text,
      }),
    );
  }
  const body = el("div", { className: "answer-body" });
  // answer_html comes from the server: Markdown with raw HTML, links and images turned off.
  body.innerHTML = view.answer_html;
  answer.append(body);
  if (view.delete_outcome === "deleted" && view.batch_id) {
    answer.append(undoLine(view.batch_id));
  }
  const actions = el("div", { className: "answer-actions" });
  if (view.report_id && view.final) {
    // The report card replaces the chat text of the report. A link shows the text again.
    const slot = el("div", { dataset: { report: view.report_id } });
    answer.append(slot);
    loadReportCard(slot, view.report_id, view.totals).then((loaded) => {
      if (!loaded) return;
      body.hidden = true;
      actions.prepend(
        el("button", {
          className: "link",
          type: "button",
          text: "Show the chat text",
          onclick: (event) => {
            body.hidden = !body.hidden;
            event.currentTarget.textContent = body.hidden
              ? "Show the chat text"
              : "Hide the chat text";
          },
        }),
      );
    });
  }
  if (view.cost_line) {
    const suffix = view.final ? "" : " · updating the memory…";
    answer.append(
      el("div", { className: "answer-meta", text: view.cost_line + suffix }),
    );
  }
  if (view.trace_id) {
    actions.append(
      el("button", {
        className: "link",
        type: "button",
        text: "Show the steps",
        onclick: () => selectAnswer(view.trace_id),
      }),
    );
  }
  answer.append(actions);
  wrap.append(answer);
  if (state.selectedTrace && state.selectedTrace === view.trace_id) {
    answer.classList.add("selected");
  }
}

function undoLine(batchId) {
  const line = el("div", { className: "answer-actions" });
  const link = el("button", {
    className: "link",
    type: "button",
    text: "Undo",
    onclick: async () => {
      link.disabled = true;
      try {
        const outcome = await api(`${conversationPath()}/undo`, {
          body: { user: state.user.id, batch_id: batchId },
        });
        line.replaceChildren(
          el("span", {
            className: `note ${outcome.kind === "message" ? "ok" : "error"}`,
            text: outcome.text,
          }),
        );
        refreshReports();
      } catch (error) {
        link.disabled = false;
        line.append(
          el("span", { className: "note error", text: ` ${error.message}` }),
        );
      }
    },
  });
  line.append(
    link,
    el("span", { className: "note", text: "Undo works for 30 days." }),
  );
  return line;
}

function conversationPath() {
  return `/api/conversations/${encodeURIComponent(state.conversationId)}`;
}

// --- 4. delete confirmation ------------------------------------------------------------------
function confirmMessage(confirmation) {
  return el("div", { className: "message" }, confirmCard(confirmation));
}

function confirmCard(confirmation) {
  const expected = confirmation.expected_text;
  const many =
    confirmation.count === 1
      ? "This report"
      : `These ${confirmation.count} reports`;
  const input = el("input", {
    className: "confirm-input",
    type: "text",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": `Type ${expected} to confirm`,
  });
  const confirmButton = el("button", {
    className: "button primary",
    type: "submit",
    text: "Confirm",
    disabled: true,
  });
  const cancelButton = el("button", {
    className: "button outline",
    type: "button",
    text: "Cancel",
  });
  const result = el("div", { className: "confirm-result" });
  const hint = el("p", {
    className: "confirm-hint",
    text: "Valid for this list only. A new message cancels it.",
  });
  const form = el(
    "form",
    { className: "confirm-row" },
    input,
    confirmButton,
    cancelButton,
  );
  // Confirm works only for the exact text. The server checks the text again.
  input.addEventListener("input", () => {
    confirmButton.disabled = input.value !== expected;
  });
  const finish = (view) => {
    form.remove();
    hint.remove();
    result.replaceChildren(answerMessage(view));
    refreshReports();
    refreshConversations();
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (input.value !== expected) return;
    lockConfirm(form, true);
    try {
      finish(
        await api(`${conversationPath()}/confirm`, {
          body: { user: state.user.id, text: input.value },
        }),
      );
    } catch (error) {
      lockConfirm(form, false);
      result.replaceChildren(
        el("p", { className: "note error", text: error.message }),
      );
    }
  });
  cancelButton.addEventListener("click", async () => {
    lockConfirm(form, true);
    try {
      finish(
        await api(`${conversationPath()}/cancel`, {
          body: { user: state.user.id },
        }),
      );
    } catch (error) {
      lockConfirm(form, false);
      result.replaceChildren(
        el("p", { className: "note error", text: error.message }),
      );
    }
  });
  const rows = confirmation.rows.map((row) =>
    el("li", {}, `${row.title} · ${row.report_id} · ${row.created}`),
  );
  const notFound = confirmation.not_found.length
    ? el("p", {
        className: "note",
        text: `Not found: ${confirmation.not_found.join(", ")}`,
      })
    : null;
  return el(
    "div",
    { className: "confirm-card", dataset: { pending: "true" } },
    el("p", { text: `${many} will be deleted:` }),
    el("ol", {}, rows),
    notFound,
    el(
      "p",
      {},
      "Type ",
      el("span", { className: "kbd", text: expected }),
      " to confirm. You can undo the delete for 30 days.",
    ),
    form,
    hint,
    result,
  );
}

function lockConfirm(form, locked) {
  for (const control of form.querySelectorAll("input, button"))
    control.disabled = locked;
}

function closeOpenConfirmations() {
  for (const card of document.querySelectorAll(
    ".confirm-card[data-pending='true']",
  )) {
    const form = card.querySelector("form");
    if (!form) continue;
    lockConfirm(form, true);
    card.dataset.pending = "false";
    card.append(
      el("p", {
        className: "note",
        text: "The new message cancels this delete.",
      }),
    );
  }
}

// --- 5. report card --------------------------------------------------------------------------
async function loadReportCard(slot, reportId, totals) {
  try {
    const report = await api(
      `/api/reports/${encodeURIComponent(reportId)}?${userQuery()}`,
    );
    slot.replaceChildren(reportCard(report, totals));
    return true;
  } catch (error) {
    slot.replaceChildren(el("p", { className: "note", text: error.message }));
    return false;
  }
}

function reportCard(report, totals) {
  const fileUrl = (format) =>
    `/api/reports/${encodeURIComponent(report.report_id)}/file?format=${format}&${userQuery()}`;
  const meta = [report.report_id, "saved in your library"];
  if (totals?.seconds)
    meta.push(`ready in ${durationText(Number(totals.seconds))}`);
  const chart = report.charts[0];
  const figure = chart
    ? el(
        "figure",
        { className: "report-chart" },
        el("img", {
          // The SVG goes in an <img>: a picture cannot run a script.
          src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(chart.svg)}`,
          alt: chart.title || "Chart",
        }),
        el("figcaption", { text: chart.title }),
      )
    : null;
  const actions = report.action_items.length
    ? el(
        "div",
        { className: "report-actions" },
        el("p", { className: "report-actions-title", text: "Action items" }),
        el(
          "ol",
          {},
          report.action_items.map((text) => el("li", { text })),
        ),
      )
    : null;
  return el(
    "article",
    { className: "report-card", dataset: { reportId: report.report_id } },
    el("h3", { className: "report-title", text: report.title }),
    el("p", { className: "report-meta", text: meta.join(" · ") }),
    report.period_text
      ? el("p", { className: "report-sections", text: report.period_text })
      : null,
    el("p", {
      className: "report-sections muted",
      text: report.sections.join(" · "),
    }),
    figure || actions
      ? el("div", { className: "report-grid" }, figure, actions)
      : null,
    el(
      "div",
      { className: "report-buttons" },
      el("a", {
        className: "button primary",
        href: fileUrl("html"),
        target: "_blank",
        rel: "noopener",
        text: "Open HTML",
      }),
      el("a", {
        className: "button outline",
        href: fileUrl("md"),
        download: `${report.report_id}.md`,
        text: "Download Markdown",
      }),
    ),
  );
}

function flashReport(reportId) {
  const card = document.querySelector(
    `[data-report="${CSS.escape(reportId)}"]`,
  );
  if (!card) {
    const slot = el("div", {
      className: "message",
      dataset: { report: reportId },
    });
    $("empty")?.remove();
    $("messages").append(slot);
    loadReportCard(slot, reportId, null).then(() =>
      slot.scrollIntoView({ block: "center" }),
    );
    return;
  }
  card.scrollIntoView({ block: "center" });
  const inner = card.querySelector(".report-card");
  if (inner) {
    inner.classList.add("flash");
    setTimeout(() => inner.classList.remove("flash"), 1600);
  }
}

// --- 3. ask and the progress stream ----------------------------------------------------------
function setBusy(busy) {
  state.busy = busy;
  const input = $("ask-input");
  input.disabled = busy;
  input.placeholder = busy
    ? "Wait for the answer"
    : "Ask about the sales of your brands";
  $("ask-button").disabled = busy;
  $("ask-button").textContent = busy ? "Working…" : "Ask";
  $("new-conversation").disabled = busy;
}

function workingMessage() {
  const text = el("span", {
    className: "working-text",
    text: "Sending the question",
  });
  const clock = el("span", { className: "clock", text: "0:00" });
  const limit = el("div", { className: "working-limit" });
  const box = el(
    "div",
    { className: "message", id: "working" },
    el(
      "div",
      { className: "working" },
      el("span", { className: "pulse" }),
      text,
      clock,
    ),
    limit,
  );
  const started = Date.now();
  const timer = setInterval(() => {
    clock.textContent = clockText(Date.now() - started);
  }, 250);
  return {
    box,
    stage(data) {
      text.textContent = data.text;
      if (data.limit) limit.textContent = data.limit;
    },
    stop() {
      clearInterval(timer);
      box.remove();
    },
  };
}

async function ask(text) {
  if (state.busy || !text.trim()) return;
  setBusy(true);
  closeOpenConfirmations();
  $("empty")?.remove();
  const box = $("messages");
  box.append(userMessage(text));
  const working = workingMessage();
  box.append(working.box);
  scrollToEnd();
  let lastTrace = null;
  try {
    const response = await fetch(`${conversationPath()}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ user: state.user.id, text }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `The server answered ${response.status}.`);
    }
    await readEvents(response, (event, data) => {
      if (event === "stage") working.stage(data);
      else if (event === "turn") {
        upsertAnswer(data, working.box);
        if (data.final && data.trace_id) lastTrace = data.trace_id;
      } else if (event === "error") {
        box.insertBefore(errorMessage(data.message), working.box);
      }
      scrollToEnd();
    });
  } catch (error) {
    box.insertBefore(errorMessage(error.message), working.box);
  } finally {
    working.stop();
    setBusy(false);
    $("ask-input").focus();
    scrollToEnd();
  }
  if (lastTrace) selectAnswer(lastTrace);
  refreshConversations();
  refreshReports();
}

async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end = buffer.indexOf("\n\n");
    while (end >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const parsed = parseEvent(block);
      if (parsed) onEvent(parsed.event, parsed.data);
      end = buffer.indexOf("\n\n");
    }
  }
}

function parseEvent(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // a keep-alive comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}

function upsertAnswer(view, before) {
  const existing = view.trace_id
    ? document.querySelector(
        `#messages [data-trace="${CSS.escape(view.trace_id)}"]`,
      )
    : null;
  if (existing) fillAnswer(existing, view);
  else $("messages").insertBefore(answerMessage(view), before);
}

function errorMessage(text) {
  return el(
    "div",
    { className: "message" },
    el(
      "div",
      { className: "answer" },
      el("span", { className: "label red", text: "Error" }),
      el("p", { className: "note error", text }),
    ),
  );
}

// --- steps pane ------------------------------------------------------------------------------
function clearSteps() {
  $("steps-body").replaceChildren(
    el("p", { className: "muted", text: "Select an answer to see its steps." }),
  );
  $("totals-body").replaceChildren();
}

async function selectAnswer(traceId) {
  state.selectedTrace = traceId;
  for (const answer of document.querySelectorAll(
    "#messages .answer.selected",
  )) {
    answer.classList.remove("selected");
  }
  const wrap = document.querySelector(
    `#messages [data-trace="${CSS.escape(traceId)}"]`,
  );
  wrap?.querySelector(".answer")?.classList.add("selected");
  try {
    const data = await api(
      `/api/turns/${encodeURIComponent(traceId)}?${userQuery()}`,
    );
    if (state.selectedTrace !== traceId) return;
    renderSteps(data);
  } catch (error) {
    $("steps-body").replaceChildren(
      el("p", { className: "muted", text: error.message }),
    );
    $("totals-body").replaceChildren();
  }
}

function renderSteps(data) {
  const items = data.steps.map((step) =>
    el(
      "li",
      { className: step.status === "error" ? "error" : null },
      el("span", { className: "step-title", text: step.title }),
      step.meta
        ? el("span", { className: "step-meta", text: ` · ${step.meta}` })
        : null,
      step.note
        ? el("span", { className: "step-note", text: step.note })
        : null,
      step.sql
        ? el(
            "details",
            { className: "step-sql" },
            el("summary", { text: "SQL" }),
            el("pre", { text: step.sql }),
          )
        : null,
    ),
  );
  $("steps-body").replaceChildren(
    items.length
      ? el("ol", { className: "step-list" }, items)
      : el("p", { className: "muted", text: "No steps in this trace." }),
  );
  $("totals-body").replaceChildren(
    ...data.totals.map((line) => el("p", { text: line })),
    el("a", {
      className: "link",
      href: `/api/turns/${encodeURIComponent(data.trace_id)}/spans?${userQuery()}`,
      target: "_blank",
      rel: "noopener",
      text: "Open the full trace",
    }),
  );
}

// --- history pane ----------------------------------------------------------------------------
function showTab(tab) {
  state.tab = tab;
  $("tab-conversations").setAttribute(
    "aria-selected",
    String(tab === "conversations"),
  );
  $("tab-reports").setAttribute("aria-selected", String(tab === "reports"));
  $("panel-conversations").hidden = tab !== "conversations";
  $("panel-reports").hidden = tab !== "reports";
}

async function refreshConversations() {
  if (!state.user) return;
  const list = $("conversation-list");
  try {
    const rows = await api(`/api/conversations?${userQuery()}`);
    const items = rows.map((row) => {
      const asks = `${row.turn_count} ${row.turn_count === 1 ? "ask" : "asks"}`;
      return el(
        "li",
        {},
        el(
          "button",
          {
            className: `side-item${row.id === state.conversationId ? " selected" : ""}`,
            type: "button",
            onclick: () =>
              !state.busy && openConversation(row.id).catch(showLoadError),
          },
          el("span", {
            className: "side-item-title",
            text: row.title || "New conversation",
          }),
          el("span", {
            className: "side-item-meta",
            text: `${state.user.id} · ${shortDate(row.last_active_at)} · ${asks}`,
          }),
        ),
      );
    });
    list.replaceChildren(
      ...(items.length
        ? items
        : [
            el("li", {
              className: "side-empty",
              text: "No conversations yet.",
            }),
          ]),
    );
  } catch (error) {
    list.replaceChildren(
      el("li", { className: "side-empty", text: error.message }),
    );
  }
}

async function refreshReports() {
  if (!state.user) return;
  const list = $("report-list");
  try {
    const rows = await api(`/api/reports?${userQuery()}`);
    const items = rows.map((row) => {
      const meta = [
        row.report_id,
        shortDate(row.created_at).split(" ").slice(0, 2).join(" "),
      ];
      return el(
        "li",
        {},
        el(
          "button",
          {
            className: "side-item",
            type: "button",
            onclick: (event) => openReport(row, event.currentTarget),
          },
          el("span", { className: "side-item-title", text: row.title }),
          el("span", {
            className: "side-item-meta",
            text: [...meta, ...row.files].join(" · "),
          }),
        ),
      );
    });
    list.replaceChildren(
      ...(items.length
        ? items
        : [el("li", { className: "side-empty", text: "No reports yet." })]),
    );
  } catch (error) {
    list.replaceChildren(
      el("li", { className: "side-empty", text: error.message }),
    );
  }
}

async function openReport(row, button) {
  if (state.busy) return;
  for (const item of document.querySelectorAll(
    "#report-list .side-item.selected",
  )) {
    item.classList.remove("selected");
  }
  button.classList.add("selected");
  if (row.conversation_id === state.conversationId) {
    flashReport(row.report_id);
    return;
  }
  try {
    await openConversation(row.conversation_id, row.report_id);
  } catch {
    flashReport(row.report_id); // the conversation is too old: show the card alone
  }
  showTab("reports");
  const again = [...document.querySelectorAll("#report-list .side-item")].find(
    (item) =>
      item
        .querySelector(".side-item-meta")
        ?.textContent.startsWith(row.report_id),
  );
  again?.classList.add("selected");
}

function showLoadError(error) {
  $("messages").append(errorMessage(error.message));
  scrollToEnd();
}

// --- start -----------------------------------------------------------------------------------
function bindEvents() {
  $("user-list").addEventListener("change", () => {
    $("start-button").disabled = !$("user-list").querySelector("input:checked");
  });
  $("start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const checked = $("user-list").querySelector("input:checked");
    if (!checked) return;
    $("start-button").disabled = true;
    $("start-error").hidden = true;
    try {
      await startConversation(checked.value);
    } catch (error) {
      $("start-error").textContent = error.message;
      $("start-error").hidden = false;
      $("start-button").disabled = false;
    }
  });
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("ask-input");
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    ask(text);
  });
  $("new-conversation").addEventListener("click", () => {
    if (!state.busy) startConversation(state.user.id).catch(showLoadError);
  });
  $("change-user").addEventListener("click", () => {
    if (!state.busy) showStart();
  });
  $("tab-conversations").addEventListener("click", () =>
    showTab("conversations"),
  );
  $("tab-reports").addEventListener("click", () => showTab("reports"));
}

async function init() {
  bindEvents();
  try {
    state.users = await api("/api/users");
  } catch (error) {
    document.body.append(
      el("p", { className: "form-error", text: error.message }),
    );
    return;
  }
  // "#/maya/C-8M2QX" opens a conversation again after a reload.
  const match = location.hash.match(/^#\/([^/]+)\/([^/]+)$/);
  const user =
    match && state.users.find((u) => u.id === decodeURIComponent(match[1]));
  if (user) {
    state.user = user;
    try {
      await openConversation(decodeURIComponent(match[2]));
      return;
    } catch {
      // A new conversation without turns, or an old one: start again.
    }
  }
  await showStart();
}

init();
