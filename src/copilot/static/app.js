// Dashboard + chat client. The dashboard comes from /api/overview; the chat posts a
// message, reads the Server-Sent Events stream, and renders each step the agent takes
// (tool calls, results, approvals, the answer). After every exchange the dashboard
// and audit log are reloaded, so an approved fix shows up as a check turning green.

const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const auditList = document.getElementById("audit");
const toolTpl = document.getElementById("tool-template");
const approvalTpl = document.getElementById("approval-template");

let sessionId = null;
let lastStatus = new Map(); // check id -> status, to highlight changes
const toolCards = new Map(); // tool_use id -> <details>

const LABELS = {
  hotel_day_statistics: "Hotel statistics",
  pos_day_summary: "POS day summary",
  pos_to_pms_room_charges: "Room charges: POS vs PMS",
  card_clearing_reconciliation: "Card clearing reconciliation",
  bank_exceptions: "Bank exceptions",
  supplier_invoices: "Supplier invoices",
  ad_spend_vs_budget: "Ad spend vs budget",
  list_accounts: "Chart of accounts",
  post_journal_adjustment: "Post journal adjustment",
  post_room_charge_to_folio: "Post room charge to folio",
  flag_invoice: "Put invoice on hold",
};

const STATUS_TEXT = { ok: "OK", warn: "Check", fail: "Problem" };

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function rand(value) {
  const n = Number(value);
  // en-US grouping gives R1,234.56, the format finance teams here use in reports
  return `${n < 0 ? "-" : ""}R${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Minimal, safe Markdown: escape first, then bold, inline code and bullet lists.
function render(md) {
  const lines = escapeHtml(md).split("\n");
  let html = "", inList = false;
  for (const line of lines) {
    const item = line.match(/^\s*[-*]\s+(.*)/);
    if (item && !inList) { html += "<ul>"; inList = true; }
    if (!item && inList) { html += "</ul>"; inList = false; }
    const text = (item ? item[1] : line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
    html += item ? `<li>${text}</li>` : text ? `<p>${text}</p>` : "";
  }
  return html + (inList ? "</ul>" : "");
}

// A plain-language line for each action that needs approval.
function describe(name, a) {
  switch (name) {
    case "post_journal_adjustment":
      return `Dr ${a.debit_account} / Cr ${a.credit_account}, ${rand(a.amount)} on ${a.day}${a.bank_ref ? `, resolving bank line ${a.bank_ref}` : ""}. ${a.reason}`;
    case "post_room_charge_to_folio":
      return `Post POS check ${a.check_no} to the guest's folio. ${a.reason}`;
    case "flag_invoice":
      return `Put invoice ${a.invoice_no} on hold so it isn't paid. ${a.reason}`;
    default:
      return "";
  }
}

// ------------------------------------------------------------------ dashboard

async function refreshOverview() {
  let data;
  try {
    data = await (await fetch("/api/overview")).json();
  } catch {
    return; // the chat still works without the dashboard
  }
  document.getElementById("hotel").textContent = data.hotel;
  const d = new Date(`${data.business_date}T00:00:00`);
  document.getElementById("business-date").textContent = d.toLocaleDateString("en-ZA", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

  const mode = document.getElementById("mode");
  mode.textContent = data.mode === "live" ? "Live · Claude API" : "Demo · recorded answers, real tools";
  mode.className = `badge badge-${data.mode}`;
  mode.title = data.mode === "live"
    ? "Answers come from Claude. Usage is capped."
    : "The model's words are recorded; tool calls, data, approvals and the audit log are real.";

  document.getElementById("kpis").innerHTML = data.kpis
    .map((k) => `<div class="kpi"><span class="kpi-label">${escapeHtml(k.label)}</span><strong>${escapeHtml(k.value)}</strong><span class="kpi-note">${escapeHtml(k.note)}</span></div>`)
    .join("");

  const list = document.getElementById("checks");
  list.innerHTML = "";
  for (const c of data.checks) {
    const li = document.createElement("li");
    const changed = lastStatus.has(c.id) && lastStatus.get(c.id) !== c.status;
    li.className = `check ${c.status}${changed ? " changed" : ""}`;
    li.innerHTML = `
      <span class="status" aria-label="${STATUS_TEXT[c.status]}">${STATUS_TEXT[c.status]}</span>
      <div class="check-body">
        <p class="check-title">${escapeHtml(c.title)} <span class="check-headline">${escapeHtml(c.headline)}</span></p>
        <p class="check-detail">${escapeHtml(c.detail)}</p>
      </div>`;
    if (c.question) {
      const ask = document.createElement("button");
      ask.type = "button";
      ask.className = "secondary small-btn ask";
      ask.textContent = "Ask copilot";
      ask.addEventListener("click", () => askQuestion(c.question));
      li.appendChild(ask);
    }
    list.appendChild(li);
  }
  lastStatus = new Map(data.checks.map((c) => [c.id, c.status]));

  const box = document.getElementById("suggestions");
  if (box) {
    box.innerHTML = "";
    for (const q of data.suggestions) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = q;
      chip.addEventListener("click", () => askQuestion(q));
      box.appendChild(chip);
    }
  }
}

// ------------------------------------------------------------------ chat

function add(el) {
  document.getElementById("empty")?.remove();
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function bubble(kind, html) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.innerHTML = html;
  return add(div);
}

function toolCard(e) {
  const node = toolTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".tool-name").textContent = LABELS[e.name] || e.name;
  node.querySelector(".tool-state").textContent = "running…";
  node.querySelector(".tool-input").textContent = JSON.stringify(e.input, null, 2);
  node.classList.add("running");
  toolCards.set(e.id, node);
  return add(node);
}

function toolResult(e) {
  const node = toolCards.get(e.id);
  if (!node) return;
  node.classList.remove("running");
  node.classList.add(e.ok ? "ok" : "failed");
  node.querySelector(".tool-state").textContent = e.ok ? "done" : "error";
  let out = e.content;
  try { out = JSON.stringify(JSON.parse(e.content), null, 2); } catch { /* plain text */ }
  node.querySelector(".tool-output").textContent = out;
}

function approvalCard(e) {
  const node = approvalTpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".tool-name").textContent = LABELS[e.name] || e.name;
  node.querySelector(".approval-summary").textContent = describe(e.name, e.input);
  node.querySelector(".tool-input").textContent = JSON.stringify(e.input, null, 2);
  const decide = (approved) => {
    node.querySelectorAll("button").forEach((b) => (b.disabled = true));
    node.classList.add(approved ? "approved" : "rejected");
    node.querySelector(".approval-title").insertAdjacentText("beforeend", approved ? " · approved" : " · rejected");
    stream("/api/decision", { session_id: sessionId, tool_use_id: e.id, approved });
  };
  node.querySelector(".approve").addEventListener("click", () => decide(true));
  node.querySelector(".reject").addEventListener("click", () => decide(false));
  return add(node);
}

function handle(e) {
  switch (e.type) {
    case "session": sessionId = e.id; break;
    case "tool_call": toolCard(e); break;
    case "tool_result": toolResult(e); break;
    case "approval_required": approvalCard(e); break;
    case "rejected": bubble("system", "Action rejected. Nothing was changed."); break;
    case "note": if (e.text) bubble("note", render(e.text)); break;
    case "answer": bubble("assistant", render(e.text || "(no answer)")); break;
    case "error": bubble("error", escapeHtml(e.message)); break;
  }
}

async function stream(url, body) {
  send.disabled = true;
  const thinking = bubble("thinking", "Working…");
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const data = part.replace(/^data: /, "");
        if (data) handle(JSON.parse(data));
      }
    }
  } catch (err) {
    bubble("error", escapeHtml(`Connection problem: ${err.message}`));
  } finally {
    thinking.remove();
    send.disabled = false;
    refreshAudit();
    refreshOverview();
  }
}

function askQuestion(text) {
  if (send.disabled) return;
  bubble("user", escapeHtml(text));
  input.value = "";
  stream("/api/chat", { session_id: sessionId, message: text });
}

async function refreshAudit() {
  try {
    const entries = await (await fetch("/api/audit")).json();
    auditList.innerHTML = entries.length
      ? entries
          .map((a) => `<li class="a-${escapeHtml(a.event)}"><span>${escapeHtml(a.at.slice(11, 19))}</span> ${escapeHtml(a.event.replaceAll("_", " "))}: <strong>${escapeHtml(LABELS[a.tool] || a.tool)}</strong></li>`)
          .join("")
      : '<li class="muted">Nothing yet. Ask the copilot something.</li>';
  } catch { /* audit is informational */ }
}

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = input.value.trim();
  if (text) askQuestion(text);
});

input.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); }
});

document.getElementById("reset").addEventListener("click", async () => {
  const res = await fetch("/api/reset", { method: "POST" });
  if (!res.ok) { bubble("error", escapeHtml((await res.json()).detail || "Reset failed.")); return; }
  location.reload();
});

refreshOverview();
refreshAudit();
