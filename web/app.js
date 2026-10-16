// Demo Bank assistant: sign-in, chat with live progress, confirmation slips with security codes,
// and the "What happened" trace. An answer is started with POST /api/chat and then checked on
// (GET /api/turns/{id}) every second until it's ready, so no request is held open for long.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const els = {
    signIn: $("sign-in"), chat: $("chat"), customers: $("customers"), owners: $("owners"), who: $("who"),
    whoName: $("who-name"), log: $("log"), empty: $("empty"), form: $("composer"), input: $("message"),
    send: $("send"), trace: $("trace"), newChat: $("new-chat"), signOut: $("sign-out"), resetBank: $("reset-bank"),
    slipTemplate: $("slip-template"), notice: $("notice"), visitorForm: $("visitor-form"),
    visitorEmail: $("visitor-email"), visitorName: $("visitor-name"), visitorNote: $("visitor-note"),
    visitorGo: $("visitor-go"), visitorTerms: $("visitor-terms"), publicSignin: $("public-signin"),
    googleButton: $("google-button"), orDivider: $("or-divider"), emailForm: $("email-form"),
    signinEmail: $("signin-email"), emailGo: $("email-go"), codeForm: $("code-form"), signinCode: $("signin-code"),
    signinCodeLabel: $("signin-code-label"), codeGo: $("code-go"), codeRestart: $("code-restart"),
    publicNote: $("public-note"), choose: $("choose"), chooseTitle: $("choose-title"),
    chooseCustomers: $("choose-customers"), chooseNote: $("choose-note"), switchCustomer: $("switch-customer"),
  };
  let signinChallenge = null;
  const SIGNIN_ERRORS = {
    cancelled: "Google sign-in was cancelled.",
    expired: "That sign-in took too long, or was started in another browser. Please try again.",
    state: "That sign-in link wasn't started here. Please try again.",
    google: "Google sign-in didn't work just now. Please try again, or use an email code.",
    unverified: "Your Google account's email address isn't verified, so it can't be used here.",
    invite: "The demo is invite-only for now, and that address isn't on the list.",
    signups: "Too many new demo banks from your network in the last hour. Please try again later.",
  };
  let me = null;
  let config = { customers: [], visitors: { bank_hours: 24, daily_messages: 30 } };
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
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    if (res.status === 401 && path !== "/api/login") {
      showSignIn(data.detail);
      throw new ApiError("Please sign in.", 401, data);
    }
    if (!res.ok && !(res.status === 409 && data.status)) {
      const detail = typeof data.detail === "string" ? data.detail : null;
      throw new ApiError(detail || `Something went wrong (${res.status}).`, res.status, data);
    }
    return { ...data, httpStatus: res.status };
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // Check back on an answer until it's ready, showing each step as it happens. Each check is a
  // "long" one: the server holds it for up to 20 seconds and replies as soon as there's news.
  async function follow(turnId, progressEl) {
    const started = Date.now();
    let seen = 0;
    while (Date.now() - started < 5 * 60 * 1000) {
      const asked = Date.now();
      let data;
      try {
        data = await api(`/api/turns/${encodeURIComponent(turnId)}?wait=20&seen=${seen}`);
      } catch (err) {
        if (err.status === 401 || err.status === 404 || (err.data && err.data.closed)) throw err;
        await sleep(1000);
        continue;   // a network blip: keep checking
      }
      if (data.status !== "running") return data;
      seen = (data.steps || []).length;
      if (progressEl) showProgress(progressEl, data.steps);
      if (Date.now() - asked < 300) await sleep(500);   // an older server that doesn't hold checks
    }
    throw new Error("This is taking longer than it should. Reload the page in a minute to see the answer.");
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

  const stepText = (s) => (s.kind === "specialist" ? s.label : `Using ${String(s.name).replace(/_/g, " ")}`);

  function thinking() {
    els.empty.hidden = true;
    const el = document.createElement("div");
    el.className = "thinking msg";
    const label = document.createElement("p");
    label.className = "thinking-label";
    label.textContent = "Working on it";
    const steps = document.createElement("ul");
    steps.className = "progress";
    el.append(label, steps);
    els.log.appendChild(el);
    scrollDown();
    return el;
  }

  function showProgress(el, steps) {
    const list = el.querySelector(".progress");
    if (!steps || list.childElementCount === steps.length) return;
    list.replaceChildren(...steps.map((s) => {
      const li = document.createElement("li");
      li.className = s.kind;
      li.textContent = stepText(s);
      return li;
    }));
    scrollDown();
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

  function renderSlip(slipData, code) {
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

    const showCodeBox = (sentTo, expiresIn) => {
      codeForm.hidden = false;
      const minutes = Math.max(1, Math.round(expiresIn / 60));
      node.querySelector(".code-label").textContent =
        `Enter the 6-digit code we emailed to ${sentTo}. It expires in ${minutes} minute${minutes === 1 ? "" : "s"}.`;
      node.querySelector(".code-label").setAttribute("for", codeInput.id = `code-${Date.now()}`);
      approve.textContent = "Confirm code";
      codeInput.focus();
    };
    if (code) showCodeBox(code.sent_to, code.expires_in); else approve.focus();

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
      const typed = codeForm.hidden ? undefined : codeInput.value.trim();
      if (approved && !codeForm.hidden && !/^\d{6}$/.test(typed)) { codeNote.textContent = "Enter the 6-digit code from the email."; codeInput.focus(); return; }
      busy = true; lock(true); setComposer(false);
      let wait = null;
      try {
        const data = await api("/api/confirm", { thread_id: threadId, approve: approved, code: typed });
        if (data.status === "code_required") { lock(false); showCodeBox(data.sent_to, data.expires_in); return; }
        if (data.status === "wrong_code") {
          codeNote.textContent = `That code isn't right. ${data.attempts_left} ${data.attempts_left === 1 ? "try" : "tries"} left.`;
          codeInput.value = ""; lock(false); codeInput.focus();
          return;
        }
        close(approved ? "Approved." : "Declined.");
        if (data.status === "needs_confirmation") { renderSlip(data.slip); return; }   // another action in the same step
        wait = thinking();
        handle(slipData.title, await follow(data.turn_id, wait));
        refreshAllowance();
      } catch (err) {
        if (node.classList.contains("done")) addMessage("bank", err.message, { error: true });
        else { lock(false); codeNote.textContent = err.message; }
      } finally {
        if (wait) wait.remove();
        busy = false; setComposer(true);
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
    else if (data.status === "failed") addMessage("bank", data.error || "I couldn't finish that. Please try again.", { error: true });
  }

  function setComposer(on) { els.send.disabled = !on; }

  // ---------- visitors: their pretend bank, its time left and today's messages ----------
  function timeLeft(expires) {
    const minutes = Math.max(0, Math.round((expires * 1000 - Date.now()) / 60000));
    if (minutes >= 90) return `in about ${Math.round(minutes / 60)} hours`;
    return `in ${minutes} minute${minutes === 1 ? "" : "s"}`;
  }

  function renderNotice() {
    if (!me || me.kind !== "guest") { els.notice.hidden = true; return; }
    els.notice.hidden = false;
    els.notice.textContent = `This is your own pretend bank: no real money, and it's deleted ${timeLeft(me.expires)}. `
      + `${me.messages_left} of ${config.visitors.daily_messages} messages left today.`;
  }

  async function refreshAllowance() {
    if (!me || me.kind !== "guest") return;
    try { me = { ...me, ...(await api("/api/me")) }; renderNotice(); } catch { /* shown elsewhere */ }
  }

  // ---------- actions ----------
  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true; setComposer(false);
    addMessage("you", text);
    els.input.value = ""; autosize();
    const wait = thinking();
    try {
      const started = await api("/api/chat", { message: text, thread_id: threadId });
      if (started.thread_id) { threadId = started.thread_id; store.set(threadKey(), threadId); }
      if (started.httpStatus === 409 && started.status === "needs_confirmation") {
        addMessage("bank", started.detail, { error: true });
      } else {
        // 409 "running": an earlier message is still being answered, so show that answer.
        handle(text, await follow(started.turn_id, wait));
      }
      refreshAllowance();
    } catch (err) {
      addMessage("bank", err.message, { error: true });
    } finally {
      wait.remove();
      busy = false; setComposer(true); els.input.focus();
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
    let data;
    try {
      data = await api(`/api/history?thread_id=${encodeURIComponent(saved)}`);
    } catch { store.del(threadKey()); return; }
    threadId = data.thread_id;
    let lastQuestion = "";
    for (const m of data.messages) { addMessage(m.role, m.text); if (m.role === "you") lastQuestion = m.text; }
    if (data.slip) renderSlip(data.slip, data.code);
    if (data.running_turn) {   // reloaded while an answer was on its way
      busy = true; setComposer(false);
      const wait = thinking();
      try { handle(lastQuestion, await follow(data.running_turn, wait)); }
      catch (err) { addMessage("bank", err.message, { error: true }); }
      finally { wait.remove(); busy = false; setComposer(true); }
    }
  }

  async function loadConfig() {
    try { config = await (await fetch("/api/config", { credentials: "same-origin" })).json(); } catch { /* offline */ }
  }

  const isPublic = () => config.auth_mode === "public";
  const signinNote = (text) => { (isPublic() ? els.publicNote : els.visitorNote).textContent = text || ""; };

  async function showSignIn(message) {
    me = null;
    els.chat.hidden = true; els.who.hidden = true; els.choose.hidden = true; els.signIn.hidden = false;
    await loadConfig();
    els.publicSignin.hidden = !isPublic();
    els.visitorForm.hidden = isPublic();
    const google = isPublic() && config.signin && config.signin.google;
    els.googleButton.hidden = !google;
    els.orDivider.hidden = !google;
    showEmailStep();
    signinNote(message && message !== "Please sign in." ? message : "");
    const v = config.visitors || { bank_hours: 24, daily_messages: 30 };
    els.visitorTerms.textContent = `Your demo bank is deleted ${v.bank_hours} hours after you open it, with your `
      + `conversations. Up to ${v.daily_messages} messages a day. It's all pretend: please don't type real card `
      + "numbers or passwords.";
    els.customers.replaceChildren();
    els.owners.hidden = !(config.customers || []).length;   // the development sign-in only
    for (const c of config.customers || []) {
      els.customers.appendChild(customerButton(c, async () => {
        await api("/api/login", { customer: c.customer });
        await enter();
      }, (err) => signinNote(err.message)));
    }
    (isPublic() ? (google ? els.googleButton : els.signinEmail) : els.visitorEmail).focus();
  }

  function customerButton(c, choose, fail) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "customer";
    const name = document.createElement("strong"); name.textContent = c.name;
    const label = document.createElement("span"); label.textContent = `Demo customer ${c.customer}`;
    b.append(name, label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await choose(); } catch (err) { b.disabled = false; fail(err); }
    });
    return b;
  }

  // The owner (an address in OWNER_EMAILS) picks Robert or Anil, and can switch later.
  function showChooser() {
    els.signIn.hidden = true; els.chat.hidden = true; els.choose.hidden = false;
    els.who.hidden = me.choosing; els.chooseNote.textContent = "";
    els.chooseTitle.textContent = me.choosing ? `Welcome back, ${me.name}` : "Switch customer";
    els.chooseCustomers.replaceChildren(...(me.customers || []).map((c) => customerButton(c, async () => {
      await api("/api/owner/customer", { customer: c.customer });
      await enter();
    }, (err) => { els.chooseNote.textContent = err.message; })));
    const first = els.chooseCustomers.querySelector("button");
    if (first) first.focus();
  }

  function showEmailStep() {
    signinChallenge = null;
    els.emailForm.hidden = false; els.codeForm.hidden = true; els.signinCode.value = "";
  }

  async function enter() {
    me = await api("/api/me");
    if (me.choosing) showChooser(); else await showChat();
  }

  async function showChat() {
    els.signIn.hidden = true; els.choose.hidden = true; els.chat.hidden = false; els.who.hidden = false;
    const customer = (me.customers || []).find((c) => c.customer === me.customer);
    els.whoName.textContent = customer && customer.name !== me.name ? `${me.name} as ${customer.name}` : me.name;
    els.resetBank.hidden = me.kind !== "guest";
    els.switchCustomer.hidden = !(me.kind === "owner" && (me.customers || []).length);
    renderNotice();
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
  els.newChat.addEventListener("click", () => { if (!busy) { resetConversation(); els.input.focus(); } });
  els.signOut.addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch { /* already signed out */ }
    store.del(threadKey());
    showSignIn();
  });
  els.resetBank.addEventListener("click", async () => {
    if (busy || !window.confirm("Start over with a new pretend bank? Your balances and conversations will be replaced.")) return;
    busy = true; setComposer(false);
    try {
      await api("/api/guest/reset", {});
      resetConversation();
      await refreshAllowance();
    } catch (err) {
      addMessage("bank", err.message, { error: true });
    } finally { busy = false; setComposer(true); }
  });
  els.switchCustomer.addEventListener("click", () => { if (!busy) showChooser(); });
  els.emailForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = els.signinEmail.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { signinNote("Enter your email address."); els.signinEmail.focus(); return; }
    els.emailGo.disabled = true; signinNote("");
    try {
      const sent = await api("/api/auth/email/start", { email });
      signinChallenge = sent.challenge;
      els.emailForm.hidden = true; els.codeForm.hidden = false;
      els.signinCodeLabel.textContent = `Enter the 6-digit code we emailed to ${sent.sent_to}. `
        + `It expires in ${Math.round(sent.expires_in / 60)} minutes.`;
      els.signinCode.focus();
    } catch (err) {
      signinNote(err.message);
    } finally { els.emailGo.disabled = false; }
  });
  els.codeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = els.signinCode.value.trim();
    if (!/^\d{6}$/.test(code)) { signinNote("Enter the 6-digit code from the email."); els.signinCode.focus(); return; }
    els.codeGo.disabled = true; signinNote("");
    try {
      await api("/api/auth/email/check", { challenge: signinChallenge, code });
      await enter();
    } catch (err) {
      const d = err.data || {};
      if (d.error === "wrong_code") {
        signinNote(`That code isn't right. ${d.attempts_left} ${d.attempts_left === 1 ? "try" : "tries"} left.`);
        els.signinCode.value = ""; els.signinCode.focus();
      } else if (d.error) {
        showEmailStep();
        signinNote("That code has expired or had too many tries. Ask for a new one.");
      } else signinNote(err.message);
    } finally { els.codeGo.disabled = false; }
  });
  els.codeRestart.addEventListener("click", () => { showEmailStep(); signinNote(""); els.signinEmail.focus(); });
  els.visitorForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = els.visitorEmail.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      els.visitorNote.textContent = "Enter your email address.";
      els.visitorEmail.focus();
      return;
    }
    els.visitorGo.disabled = true; els.visitorNote.textContent = "";
    try {
      await api("/api/login", { email, name: els.visitorName.value.trim() || undefined });
      await enter();
    } catch (err) {
      els.visitorNote.textContent = err.message;
    } finally { els.visitorGo.disabled = false; }
  });

  (async () => {
    await loadConfig();
    const error = new URLSearchParams(window.location.search).get("signin_error");
    if (error !== null) window.history.replaceState(null, "", window.location.pathname);   // back from Google
    try {
      const res = await fetch("/api/me", { credentials: "same-origin" });
      if (res.ok) { me = await res.json(); if (me.choosing) showChooser(); else showChat(); }
      else showSignIn(error !== null ? (SIGNIN_ERRORS[error] || SIGNIN_ERRORS.google) : undefined);
    } catch { showSignIn(); }
  })();
})();
