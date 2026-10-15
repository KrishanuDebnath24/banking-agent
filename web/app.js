// Demo Bank assistant: sign-in, chat, confirmation slips with security codes, and the "What happened" trace.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const els = {
    signIn: $("sign-in"), chat: $("chat"), customers: $("customers"), who: $("who"), whoName: $("who-name"),
    log: $("log"), empty: $("empty"), form: $("composer"), input: $("message"), send: $("send"),
    trace: $("trace"), newChat: $("new-chat"), signOut: $("sign-out"), slipTemplate: $("slip-template"),
  };
  let me = null;
  let threadId = null;
  let busy = false;

  // ---------- storage (per-viewer convenience only; the server holds the real state) ----------
  const store = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } },
    del(key) { try { sessionStorage.removeItem(key); } catch { /* private mode */ } },
  };
  const threadKey = () => `thread-${me && me.customer}`;

  // ---------- API ----------
  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    if (res.status === 401) { showSignIn(); throw new Error("Please sign in."); }
    if (!res.ok && !(res.status === 409 && data.status)) {
      throw new Error(data.detail || `Something went wrong (${res.status}).`);
    }
    return data;
  }

  // ---------- safe markdown (escape first, then a small subset) ----------
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  function markdown(text) {
    const lines = String(text || "").replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*\|/.test(line)) {                       // table
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const isSep = (r) => cells(r).every((c) => /^:?-{2,}:?$/.test(c));
        const [head, ...rest] = rows.filter((r) => !isSep(r));
        if (head) {
          out.push("<table><thead><tr>" + cells(head).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>"
            + rest.map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("")
            + "</tbody></table>");
        }
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {                  // bullet list
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ""));
        out.push("<ul>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ul>");
        continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {               // numbered list
        const items = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ""));
        out.push("<ol>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ol>");
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(\||[-*]\s|\d+[.)]\s)/.test(lines[i])) para.push(lines[i++]);
      out.push(`<p>${para.map((p) => inline(p.replace(/^#+\s*/, ""))).join("<br>")}</p>`);
    }
    return out.join("");
  }

  // ---------- rendering ----------
  function scrollDown() { els.log.scrollTop = els.log.scrollHeight; }

  function addMessage(role, text, opts = {}) {
    els.empty.hidden = true;
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}${opts.error ? " error" : ""}`;
    const body = document.createElement("div");
    body.className = "text";
    if (role === "bank" && !opts.error) body.innerHTML = markdown(text);   // escaped inside markdown()
    else body.textContent = text;
    wrap.appendChild(body);
    els.log.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function thinking() {
    const el = document.createElement("p");
    el.className = "thinking msg";
    el.textContent = "Working on it";
    els.log.appendChild(el);
    scrollDown();
    return el;
  }

  const argText = (args) => Object.entries(args || {})
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");

  function addTrace(question, data) {
    const li = document.createElement("li");
    li.className = "trace-item";
    const q = document.createElement("p");
    q.className = "trace-q";
    q.textContent = question;
    li.appendChild(q);
    const steps = document.createElement("ul");
    steps.className = "trace-steps";
    for (const s of data.steps || []) {
      const item = document.createElement("li");
      if (s.kind === "specialist") { item.className = "specialist"; item.textContent = s.label; }
      else {
        item.textContent = s.name;
        const a = argText(s.args);
        if (a) { const span = document.createElement("span"); span.className = "args"; span.textContent = ` (${a})`; item.appendChild(span); }
      }
      steps.appendChild(item);
    }
    if (!(data.steps || []).length) {
      const item = document.createElement("li");
      item.textContent = "Answered directly";
      steps.appendChild(item);
    }
    li.appendChild(steps);
    for (const g of data.guard || []) {
      const p = document.createElement("p");
      p.className = `trace-guard${/blocked/.test(g) ? " blocked" : ""}`;
      p.textContent = `Guardrail: ${g}`;
      li.appendChild(p);
    }
    if (data.cost) {
      const p = document.createElement("p");
      p.className = "trace-cost";
      p.textContent = data.cost;
      li.appendChild(p);
    }
    els.trace.prepend(li);
  }

  function renderSlip(slipData) {
    const node = els.slipTemplate.content.firstElementChild.cloneNode(true);
    node.querySelector(".slip-title").textContent = slipData.title;
    node.querySelector(".slip").setAttribute("aria-label", slipData.title);
    const dl = node.querySelector(".slip-rows");
    for (const [k, v] of slipData.rows || []) {
      const row = document.createElement("div");
      const dt = document.createElement("dt"); dt.textContent = k;
      const leader = document.createElement("span"); leader.className = "leader"; leader.setAttribute("aria-hidden", "true");
      const dd = document.createElement("dd"); dd.textContent = v;
      row.append(dt, leader, dd);
      dl.appendChild(row);
    }
    const approve = node.querySelector(".approve");
    const decline = node.querySelector(".decline");
    const codeForm = node.querySelector(".slip-code");
    const codeInput = node.querySelector(".code-input");
    const codeNote = node.querySelector(".code-note");
    approve.textContent = slipData.approve_label || "Approve";
    els.empty.hidden = true;
    els.log.appendChild(node);
    scrollDown();
    approve.focus();

    const lock = (on) => { approve.disabled = decline.disabled = on; codeInput.disabled = on; };
    const close = (outcome) => {
      node.classList.add("done");
      const p = document.createElement("p");
      p.className = "slip-outcome";
      p.textContent = outcome;
      node.querySelector(".slip").appendChild(p);
    };

    async function decide(approved) {
      if (busy) return;
      const code = codeForm.hidden ? undefined : codeInput.value.trim();
      if (approved && !codeForm.hidden && !/^\d{6}$/.test(code)) { codeNote.textContent = "Enter the 6-digit code from the email."; codeInput.focus(); return; }
      busy = true; lock(true);
      try {
        const data = await api("/api/confirm", { thread_id: threadId, approve: approved, code });
        if (data.status === "code_required") {
          codeForm.hidden = false;
          node.querySelector(".code-label").textContent =
            `Enter the 6-digit code we emailed to ${data.sent_to}. It expires in ${Math.round(data.expires_in / 60)} minutes.`;
          node.querySelector(".code-label").setAttribute("for", codeInput.id = `code-${Date.now()}`);
          approve.textContent = "Confirm code";
          lock(false); codeInput.focus();
          return;
        }
        if (data.status === "wrong_code") {
          codeNote.textContent = `That code isn't right. ${data.attempts_left} ${data.attempts_left === 1 ? "try" : "tries"} left.`;
          codeInput.value = ""; lock(false); codeInput.focus();
          return;
        }
        close(approved ? "Approved." : "Declined.");
        handle(slipData.title, data);
      } catch (err) {
        lock(false);
        codeNote.textContent = err.message;
      } finally {
        busy = false;
      }
    }
    approve.addEventListener("click", () => decide(true));
    decline.addEventListener("click", () => decide(false));
    codeForm.addEventListener("submit", (e) => { e.preventDefault(); decide(true); });
  }

  function handle(question, data) {
    if (data.thread_id) { threadId = data.thread_id; store.set(threadKey(), threadId); }
    if (data.steps || data.cost) addTrace(question, data);
    if (data.status === "needs_confirmation") renderSlip(data.slip);
    else if (data.status === "answered") addMessage("bank", data.answer);
  }

  // ---------- actions ----------
  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true; els.send.disabled = true;
    addMessage("you", text);
    els.input.value = ""; autosize();
    const wait = thinking();
    try {
      handle(text, await api("/api/chat", { message: text, thread_id: threadId }));
    } catch (err) {
      addMessage("bank", err.message, { error: true });
    } finally {
      wait.remove();
      busy = false; els.send.disabled = false; els.input.focus();
    }
  }

  function resetConversation(forget = true) {
    threadId = null;
    if (forget) store.del(threadKey());
    els.log.querySelectorAll(".msg, .slip-wrap, .thinking").forEach((n) => n.remove());
    els.trace.replaceChildren();
    els.empty.hidden = false;
  }

  async function restore() {
    const saved = store.get(threadKey());
    if (!saved) return;
    try {
      const data = await api(`/api/history?thread_id=${encodeURIComponent(saved)}`);
      threadId = data.thread_id;
      for (const m of data.messages) addMessage(m.role, m.text);
      if (data.slip) renderSlip(data.slip);
    } catch { store.del(threadKey()); }
  }

  async function showSignIn() {
    me = null;
    els.chat.hidden = true; els.who.hidden = true; els.signIn.hidden = false;
    let cfg = { customers: [] };
    try { cfg = await (await fetch("/api/config", { credentials: "same-origin" })).json(); } catch { /* offline */ }
    els.customers.replaceChildren();
    for (const c of cfg.customers) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "customer";
      const name = document.createElement("strong"); name.textContent = c.name;
      const label = document.createElement("span"); label.textContent = `Demo customer ${c.customer}`;
      b.append(name, label);
      b.addEventListener("click", async () => {
        b.disabled = true;
        try { me = await api("/api/login", { customer: c.customer }); showChat(); }
        catch (err) { b.disabled = false; alert(err.message); }
      });
      els.customers.appendChild(b);
    }
  }

  async function showChat() {
    els.signIn.hidden = true; els.chat.hidden = false; els.who.hidden = false;
    els.whoName.textContent = me.name;
    resetConversation(false);   // clear the page, keep the saved conversation id
    await restore();
    if (window.matchMedia("(max-width: 52rem)").matches) $("trace-toggle").open = false;
    els.input.focus();
  }

  function autosize() { els.input.style.height = "auto"; els.input.style.height = `${Math.min(els.input.scrollHeight, 160)}px`; }

  // ---------- wiring ----------
  els.form.addEventListener("submit", (e) => { e.preventDefault(); send(els.input.value); });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(els.input.value); }
  });
  els.input.addEventListener("input", autosize);
  document.querySelectorAll(".suggestion").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
  els.newChat.addEventListener("click", () => { resetConversation(); els.input.focus(); });
  els.signOut.addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch { /* already signed out */ }
    store.del(threadKey());
    showSignIn();
  });

  (async () => {
    try {
      const res = await fetch("/api/me", { credentials: "same-origin" });
      if (res.ok) { me = await res.json(); showChat(); } else showSignIn();
    } catch { showSignIn(); }
  })();
})();
