'use strict';

const API_BASE = location.origin;
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
const $ = id => document.getElementById(id);
const escapeHtml = v => String(v ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
}[c]));

/* ============================================================
   🔐 AUTH (client-side demo — production: replace with server auth)
   ============================================================ */
const AUTH_ENABLED = true;
const AUTH_KEY = "sp-engine-auth";
const AUTH_USER = "admin";
const AUTH_PASS = "admin"; // ⚠️ পরিবর্তন করুন

/* ============================================================
   🧠 STATE
   ============================================================ */
const S = {
  ws: null,
  reconnectDelay: 1000,
  maxLogs: 300,
  detectedMelbetId: null,
  selectedEntityId: null,
  selectedFormat: null,
  selectedFixture: null,
  activeEntityId: null,
  lockedMelbetId: null,
  isLocked: false,
  isVerified: false,
  guardKilled: false,
  mulling: { delivery: false, batsman: false, wicket: false },
  workerLaunched: { delivery: false, batsman: false, wicket: false },
  currentOver: null,
  overDots: [],
  lastEventAt: 0,
};

/* ============================================================
   📝 LOG
   ============================================================ */
function log(msg, level = "info") {
  const t = $("terminal");
  if (!t) return;
  const li = document.createElement("li");
  li.className = typeof level === "boolean" ? (level ? "err" : "info") : level;
  li.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  t.appendChild(li);
  t.scrollTop = t.scrollHeight;
  while (t.children.length > S.maxLogs) t.removeChild(t.firstChild);
}

/* ============================================================
   🔌 HELPERS
   ============================================================ */
async function postJson(path, body) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return await res.json();
  } catch (e) {
    log(`# API Error: ${e.message}`, "err");
    return null;
  }
}

function setErr(id, msg) {
  const el = $(id);
  if (!el) return;
  if (msg) { el.textContent = msg; el.hidden = false; }
  else { el.hidden = true; el.textContent = ""; }
}

function setPill(name, state, label) {
  const el = $(`pill${name}`);
  if (!el) return;
  el.className = `pill pill-${state}`;
  el.textContent = label;
}

async function withBusy(btn, fn) {
  if (!btn || btn.dataset.busy === "1") return;
  btn.dataset.busy = "1";
  const prev = btn.disabled;
  btn.disabled = true;
  try { return await fn(); }
  finally { btn.dataset.busy = ""; btn.disabled = prev; }
}

/* ============================================================
   ⚠️ CONFIRM MODAL
   ============================================================ */
function confirmDialog(title, msg, danger = true) {
  return new Promise(resolve => {
    const modal = $("modal");
    $("modalTitle").textContent = title;
    $("modalMsg").textContent = msg;
    const ok = $("modalOk"), cancel = $("modalCancel");
    ok.className = danger ? "btn-danger" : "btn-primary";
    ok.textContent = danger ? "Confirm" : "OK";
    modal.hidden = false;
    const finish = v => {
      modal.hidden = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onNo);
      resolve(v);
    };
    const onOk = () => finish(true);
    const onNo = () => finish(false);
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onNo);
  });
}

/* ============================================================
   🎾 BALL / OVER DOTS
   ============================================================ */
function setBall(kind, value) {
  const ball = $("ball");
  if (!ball) return;
  const known = ["four","six","wicket","dot","run","noball","wide","overdone","freehit","idle","msg","raw"];
  ball.className = "ball " + (known.includes(kind) ? kind : "run");
  const label = ball.querySelector("span");
  if (label) label.textContent = String(value ?? "");
  else ball.textContent = String(value ?? "");
}

function classifyEventText(ev) {
  const e = String(ev || "").trim().toLowerCase();
  if (!e) return { kind: "idle", label: "-" };
  if (/^(w|wicket|out|bowled|lbw|caught|stumped|run ?out)/.test(e)) return { kind: "wicket", label: "W" };
  if (/^(nb|no ?ball)/.test(e)) return { kind: "noball", label: String(ev).toUpperCase() };
  if (/^(wd|wide)/.test(e)) return { kind: "wide", label: String(ev).toUpperCase() };
  if (/^6$|^six/.test(e)) return { kind: "six", label: "6" };
  if (/^4$|^four/.test(e)) return { kind: "four", label: "4" };
  if (/^0$|^dot/.test(e)) return { kind: "dot", label: "0" };
  if (/^\d+$/.test(e)) return { kind: "run", label: String(parseInt(e, 10)) };
  return { kind: "raw", label: String(ev).slice(0, 60) };
}

function renderOverDots() {
  const c = $("overDots");
  if (!c) return;
  let h = "";
  for (let i = 0; i < 6; i++) {
    const v = S.overDots[i];
    if (v === "d") h += '<div class="od d">0</div>';
    else if (v === "f") h += '<div class="od f">4</div>';
    else if (v === "s") h += '<div class="od s">6</div>';
    else if (v === "w") h += '<div class="od w">W</div>';
    else if (v === "x") h += '<div class="od x">!</div>';
    else if (v === "r") h += '<div class="od">•</div>';
    else h += '<div class="od e">·</div>';
  }
  c.innerHTML = h;
}

function updateOverDots(over, ball, kind) {
  if (over == null) return;
  if (S.currentOver !== over) { S.currentOver = over; S.overDots = []; }
  const idx = Math.max(0, Math.min(5, (ball || 1) - 1));
  S.overDots[idx] =
    kind === "wicket" ? "w" : kind === "six" ? "s" : kind === "four" ? "f" :
    kind === "dot" ? "d" : (kind === "noball" || kind === "wide") ? "x" : "r";
  renderOverDots();
}

/* ============================================================
   📊 BARS
   ============================================================ */
function updateOddsBar(lag) {
  if (lag == null) return;
  const h = Math.min(90, Math.max(4, lag / 5));
  if ($("barOdds")) $("barOdds").style.height = `${h}px`;
  if ($("barOddsTxt")) $("barOddsTxt").textContent = `${lag}ms`;
}

function updateClickBar(ms) {
  if (ms == null) return;
  const h = Math.min(90, Math.max(4, ms / 5));
  if ($("barClick")) $("barClick").style.height = `${h}px`;
  if ($("barClickTxt")) $("barClickTxt").textContent = `${ms}ms`;
}

/* ============================================================
   📡 STATUS SETTERS
   ============================================================ */
function setEntityStatus(state, msgs) {
  const el = $("entityStatus");
  if (!el) return;
  if (state === "connected") {
    el.textContent = `● Connected${msgs ? ` (${msgs})` : ""}`;
    el.style.color = "var(--green)";
  } else if (state === "connecting") {
    el.textContent = "◐ Connecting…";
    el.style.color = "var(--yellow)";
  } else {
    el.textContent = `○ ${state || "Disconnected"}`;
    el.style.color = "var(--yellow)";
  }
}

function setGuard(killed) {
  S.guardKilled = !!killed;
  setPill("Guard", killed ? "offline" : "online", killed ? "Killed" : "Guard");
  const b = $("bKill");
  if (!b) return;
  b.style.background = killed ? "var(--red)" : "";
  b.style.borderColor = killed ? "var(--red)" : "";
  b.style.color = killed ? "#fff" : "";
  b.title = killed ? "Kill-switch ON — click to resume" : "Kill-switch: stop all bets";
  b.innerHTML = killed ? '<i class="fa-solid fa-play"></i>' : '<i class="fa-solid fa-ban"></i>';
}

function setLock(isOn) {
  S.isLocked = isOn;
  const b = $("lockBtn");
  if (!b) return;
  if (isOn) {
    b.className = "lock-btn locked";
    b.innerHTML = '<i class="fa-solid fa-lock"></i> UNLOCK';
  } else {
    b.className = "lock-btn unlocked";
    b.innerHTML = '<i class="fa-solid fa-lock-open"></i> LOCK';
    setVerified(false);
  }
}

function setVerified(isOn) {
  S.isVerified = isOn;
  const b = $("verifyBtn");
  if (!b) return;
  b.classList.toggle("verified", isOn);
  b.textContent = isOn ? "VERIFIED ✓" : "VERIFY";
}

function setBrowserReady(ready) {
  setPill("Browser", ready ? "online" : "offline", ready ? "Browser" : "Browser OFF");
}

/* ============================================================
   🔄 RESET
   ============================================================ */
function resetUiState(msg) {
  setLock(false);
  setVerified(false);
  setBrowserReady(false);
  S.lockedMelbetId = null;
  S.detectedMelbetId = null;
  S.activeEntityId = null;
  S.selectedEntityId = null;
  S.selectedFormat = null;
  S.selectedFixture = null;
  S.mulling = { delivery: false, batsman: false, wicket: false };
  $("matchKey").value = "";
  $("entityKey").value = "";
  $("type").value = "";
  $("matchUrl").value = "";
  ["errMatchUrl","errMatchKey","errEntityKey"].forEach(id => setErr(id, ""));
  if ($("status")) $("status").textContent = msg || "Waiting for data...";
  if ($("matchTeams")) $("matchTeams").textContent = "-- vs --";
  if ($("score")) $("score").textContent = "0 - 0";
  if ($("overTxt")) $("overTxt").textContent = "0.0 OVER";
  if ($("overNo")) $("overNo").textContent = "-- OVER";
  S.currentOver = null;
  S.overDots = [];
  renderOverDots();
  setBall("idle", "-");
  document.querySelectorAll("input.radio-look[data-kind]").forEach(cb => {
    cb.checked = false;
    const t = $(cb.dataset.target);
    if (t) t.hidden = true;
  });
}

/* ============================================================
   🎯 ENTITY EVENTS
   ============================================================ */
function handleEntityBallEvent(m, isSnapshot = false) {
  const p = classifyEventText(m.event);
  setBall(p.kind, p.label);
  $("status").textContent = isSnapshot ? "Preview • last known" : "Entity Push • Live";
  if (!isSnapshot) {
    const now = Date.now();
    if (S.lastEventAt) updateClickBar(Math.min(300, now - S.lastEventAt));
    S.lastEventAt = now;
  }
  if (m.over_number != null) {
    $("overTxt").textContent = `${m.over_number}.${m.ball_number || 0} OVER`;
    $("overNo").textContent = `OVER ${m.over_number}`;
  }
  if (m.score) $("score").textContent = m.score;
  if (m.teams) $("matchTeams").textContent = m.teams;
  updateOverDots(m.over_number, m.ball_number, p.kind);
  if (m.lag_ms != null) updateOddsBar(m.lag_ms);
  if (!isSnapshot) {
    log(`[Ball] ${m.over_number ?? "?"}.${m.ball_number ?? "?"} ${m.event || "-"} ${m.lag_ms != null ? `(lag=${m.lag_ms}ms)` : ""}`);
  }
}

function applySnapshot(m) {
  S.currentOver = null;
  S.overDots = [];
  renderOverDots();
  setBall("idle", "-");
  $("status").textContent = "Preview • waiting for data...";
  $("matchTeams").textContent = m.teams || "-- vs --";
  $("score").textContent = m.score || "0 - 0";
  $("overTxt").textContent = "0.0 OVER";
  $("overNo").textContent = "-- OVER";
  if (m.event) {
    handleEntityBallEvent({
      ...m.event,
      teams: m.teams || m.event.teams,
      score: m.score || m.event.score,
    }, true);
  }
}

function setMulling(kind, on) {
  S.mulling[kind] = on;
  const cb = $(kind);
  if (cb) {
    cb.checked = on;
    const t = $(cb.dataset.target);
    if (t) t.hidden = !on;
  }
}

/* ============================================================
   🔌 WEBSOCKET (exponential backoff + single-instance guard)
   ============================================================ */
function scheduleReconnect() {
  if (S.ws && S.ws.readyState !== WebSocket.CLOSED) return;
  const delay = S.reconnectDelay;
  S.reconnectDelay = Math.min(S.reconnectDelay * 2, 30000);
  log(`# WS disconnected, retry in ${delay}ms`, "warn");
  setTimeout(() => {
    if (AUTH_ENABLED && !sessionStorage.getItem(AUTH_KEY)) return;
    startLiveListener();
  }, delay);
}

function startLiveListener() {
  if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) return;
  try {
    S.ws = new WebSocket(WS_URL);
  } catch (e) {
    log(`# WS create error: ${e.message}`, "err");
    scheduleReconnect();
    return;
  }

  S.ws.onopen = () => {
    S.reconnectDelay = 1000;
    setPill("Ws", "online", "WS");
    log("# WebSocket connected", "ok");
  };

  S.ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case "entity_push_status":
        setEntityStatus(m.status, m.messages);
        break;

      case "guard_status":
        setGuard(m.killed);
        break;

      case "browser_status":
        if (!m.main) {
          S.isLocked = false;
          S.lockedMelbetId = null;
          S.detectedMelbetId = null;
          S.activeEntityId = null;
          $("matchKey").value = "";
          setBrowserReady(false);
        } else {
          setBrowserReady(true);
          if (m.match_key) { S.detectedMelbetId = m.match_key; $("matchKey").value = m.match_key; }
        }
        if (m.locked) setLock(true);
        if (m.verified) setVerified(true);
        if (m.entity_key) { S.activeEntityId = m.entity_key; $("entityKey").value = m.entity_key; }
        S.workerLaunched.batsman = !!m.batsman;
        S.workerLaunched.wicket = !!m.wicket;
        break;

      case "browser_closed":
        if (m.target === "main") {
          resetUiState("Browser closed");
          log(`# ⚠️ Main browser closed — UI reset`, "err");
        } else {
          S.workerLaunched[m.target] = false;
          S.mulling[m.target] = false;
          log(`# ⚠️ ${m.target} browser closed`, "err");
        }
        break;

      case "entity_active_match":
        S.activeEntityId = m.match_id;
        if (m.match_id) log(`# ✅ Active: entity=${m.match_id} melbet=${m.melbet_match_id}`, "ok");
        break;

      case "match_id_detected":
        S.detectedMelbetId = m.match_id;
        if (!S.isLocked) $("matchKey").value = m.match_id;
        log(`# 🎯 Melbet match ${m.locked ? "(locked)" : "detected"}: ${m.match_id}`);
        break;

      case "verified":
        setVerified(true);
        log(`# ✅ Verified → melbet=${m.match_id} entity=${m.entity_match_id}`, "ok");
        break;

      case "unlocked":
        setLock(false);
        S.lockedMelbetId = null;
        $("matchKey").value = S.detectedMelbetId || "";
        break;

      case "entity_match_info":
        if (m.teams) $("matchTeams").textContent = m.teams;
        break;

      case "entity_snapshot":
        applySnapshot(m);
        break;

      case "entity_ball_event":
        handleEntityBallEvent(m);
        break;

      case "mulling_status":
        setMulling(m.worker, !!m.enabled);
        break;

      case "worker_error":
        log(`# ⚠️ ${m.worker} error: ${m.message}`, "err");
        break;

      case "match_update":
        if (m.teams) $("matchTeams").textContent = m.teams;
        if (m.score) $("score").textContent = m.score;
        if (m.over) $("overTxt").textContent = `${m.over} OVER`;
        if (m.overNo) $("overNo").textContent = m.overNo;
        if (m.status) $("status").textContent = m.status;
        if (m.ball) setBall(m.ballKind || "run", m.ball);
        break;
    }
  };

  S.ws.onclose = () => {
    setPill("Ws", "offline", "WS");
    setEntityStatus("disconnected");
    scheduleReconnect();
  };

  S.ws.onerror = () => {};
}

/* ============================================================
   📋 FIXTURES
   ============================================================ */
async function loadFixtures() {
  const tb = $("fixture");
  if (!tb) return;
  tb.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">Loading fixtures...</td></tr>`;
  try {
    const res = await fetch(`${API_BASE}/api/fixtures`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const data = await res.json();
    const fixtures = data.fixtures || [];
    if (!fixtures.length) {
      tb.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">No fixtures</td></tr>`;
      return;
    }
    let h = "";
    fixtures.forEach(f => {
      const color =
        /live|started/i.test(f.status) ? "var(--green)" :
        /upcoming|not started/i.test(f.status) ? "var(--yellow)" : "var(--text-muted)";
      h += `<tr style="cursor:pointer"
        data-key="${escapeHtml(f.match_id)}"
        data-title="${escapeHtml(f.title || "")}"
        data-type="${escapeHtml(f.format || "")}"
        data-status="${escapeHtml(f.status || "")}">
        <td><code>${escapeHtml(f.match_id)}</code></td>
        <td><strong>${escapeHtml(f.title)}</strong></td>
        <td>${escapeHtml(f.format || "—")}</td>
        <td><span style="color:${color}">${escapeHtml((f.status || "").toUpperCase())}</span></td>
      </tr>`;
    });
    tb.innerHTML = h;
    tb.querySelectorAll("tr").forEach(tr => {
      tr.addEventListener("click", () => {
        tb.querySelectorAll("tr").forEach(r => r.classList.remove("selected"));
        tr.classList.add("selected");
        S.selectedEntityId = tr.dataset.key;
        S.selectedFormat = tr.dataset.type;
        S.selectedFixture = { title: tr.dataset.title, format: tr.dataset.type, status: tr.dataset.status };
        $("entityKey").value = tr.dataset.key;
        $("type").value = tr.dataset.type;
        setErr("errEntityKey", "");
        log(`# Fixture selected: ${tr.dataset.title} (entity=${tr.dataset.key}, format=${tr.dataset.type})`);
        postJson("/api/preview", { entity_match_id: tr.dataset.key });
      });
    });
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--red)">Failed: ${escapeHtml(e.message)}</td></tr>`;
    log(`# Fixture load failed: ${e.message}`, "err");
  }
}

/* ============================================================
   🔐 AUTH FLOW
   ============================================================ */
function showApp() {
  $("authGate").hidden = true;
  $("appRoot").style.display = "";
  startLiveListener();
  loadFixtures();
  updateClickBar(0);
  updateOddsBar(0);
  setLock(false);
  setBrowserReady(false);
}

function showGate() {
  $("authGate").hidden = false;
  $("appRoot").style.display = "none";
  if (S.ws) { try { S.ws.close(); } catch {} S.ws = null; }
}

/* ============================================================
   🚀 WIRE UP
   ============================================================ */
window.addEventListener("DOMContentLoaded", () => {
  // ----- Auth -----
  if (AUTH_ENABLED) {
    if (sessionStorage.getItem(AUTH_KEY) === "1") showApp();
    else showGate();

    $("authForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const u = $("authUser").value.trim();
      const p = $("authPass").value;
      if (u === AUTH_USER && p === AUTH_PASS) {
        sessionStorage.setItem(AUTH_KEY, "1");
        setErr("authErr", "");
        showApp();
      } else {
        setErr("authErr", "ভুল ইউজারনেম বা পাসওয়ার্ড");
      }
    });

    $("logoutBtn").addEventListener("click", async () => {
      if (Object.values(S.mulling).some(Boolean)) {
        const ok = await confirmDialog("Logout?", "Mulling এখনো চালু আছে। Logout করলে সিস্টেম চলতে থাকবে, কিন্তু UI থেকে নিয়ন্ত্রণ হারাবেন।", true);
        if (!ok) return;
      }
      sessionStorage.removeItem(AUTH_KEY);
      showGate();
    });
  } else {
    showApp();
  }

  // ----- Terminal clear -----
  $("clearTerminal").addEventListener("click", () => {
    const t = $("terminal");
    if (t) t.innerHTML = "";
  });

  $("refreshFixtures").addEventListener("click", loadFixtures);

  // ----- Launch -----
  async function launchMain() {
    const url = $("site").value.trim();
    if (!url) { setErr("errSite", "Website URL required"); return; }
    setErr("errSite", "");
    log(`# Launching main browser: ${url}`);
    const res = await postJson("/api/launch", { website_url: url });
    if (res?.status === "success") log(`# ✅ ${res.message || "Browser opened"}`, "ok");
    else log(`# ❌ ${res?.message || "Launch failed"}`, "err");
  }
  $("submitSiteBtn").addEventListener("click", () => withBusy($("submitSiteBtn"), launchMain));
  $("bLaunch").addEventListener("click", () => withBusy($("bLaunch"), launchMain));

  // ----- Stop (confirm) -----
  $("bStop").addEventListener("click", async () => {
    if (Object.values(S.mulling).some(Boolean)) {
      const ok = await confirmDialog("Stop All Workers?", "সব চলমান মুলিং ও ব্রাউজার বন্ধ হবে। নিশ্চিত?", true);
      if (!ok) return;
    }
    await withBusy($("bStop"), async () => {
      log("# Stop requested — disarming all workers…", "warn");
      await postJson("/api/stop-all", {});
      location.reload();
    });
  });

  // ----- Kill-switch (confirm) -----
  $("bKill").addEventListener("click", async () => {
    const arming = !S.guardKilled;
    if (arming) {
      const ok = await confirmDialog(
        "Engage Kill-Switch?",
        "সমস্ত worker ও বেট প্লেসমেন্ট তাৎক্ষণিক বন্ধ হবে। আপনি Resume না করা পর্যন্ত কোনো বেট যাবে না।",
        true
      );
      if (!ok) return;
    }
    await withBusy($("bKill"), async () => {
      const res = await postJson(arming ? "/api/guard/kill" : "/api/guard/resume", {});
      if (res?.status === "success") log(`# ${res.message}`, arming ? "warn" : "ok");
      else log(`# ❌ ${res?.message || "Kill-switch request failed"}`, "err");
    });
  });

  // ----- Match URL → match key parse -----
  let parseTimer = null;
  $("matchUrl").addEventListener("input", () => {
    clearTimeout(parseTimer);
    parseTimer = setTimeout(async () => {
      if (S.isLocked) return;
      const url = $("matchUrl").value.trim();
      if (!url) { setErr("errMatchUrl", ""); return; }
      const res = await postJson("/api/parse-match-url", { url });
      if (res?.status === "success") {
        $("matchKey").value = res.match_id;
        setErr("errMatchUrl", "");
        log(`# Match URL থেকে ম্যাচ আইডি: ${res.match_id}`);
      } else {
        setErr("errMatchUrl", res?.message || "URL থেকে match key পাওয়া যায়নি");
      }
    }, 400);
  });

  // ----- Lock -----
  $("lockBtn").addEventListener("click", async () => {
    if (!S.isLocked) {
      const mid = ($("matchKey").value || "").trim();
      if (!mid) { setErr("errMatchKey", "আগে ব্রাউজারে ম্যাচ খুলুন — match ID detected হয়নি"); return; }
      setErr("errMatchKey", "");
      await withBusy($("lockBtn"), async () => {
        const res = await postJson("/api/lock", { match_id: mid });
        if (res?.status === "success") {
          S.lockedMelbetId = res.match_id;
          setLock(true);
          log(`# 🔒 Locked: ${res.match_id}`, "ok");
        } else log(`# ❌ ${res?.message || "Lock failed"}`, "err");
      });
    } else {
      if (Object.values(S.mulling).some(Boolean)) {
        const ok = await confirmDialog("Unlock Match?", "Mulling চলাকালীন Unlock করলে বেট ভুল ম্যাচে যেতে পারে। নিশ্চিত?", true);
        if (!ok) return;
      }
      await withBusy($("lockBtn"), async () => {
        const res = await postJson("/api/unlock", {});
        if (res?.status === "success") { setLock(false); log("# Match unlocked", "ok"); }
      });
    }
  });

  // ----- Verify -----
  $("verifyBtn").addEventListener("click", async () => {
    if (S.isVerified) return;
    const url = $("matchUrl").value.trim();
    if (!url) { setErr("errMatchUrl", "Match URL required"); return; }
    if (!S.isLocked) { setErr("errMatchUrl", "আগে LOCK করুন"); return; }
    if (!S.selectedEntityId) { setErr("errEntityKey", "আগে একটি fixture সিলেক্ট করুন"); return; }
    if (!S.selectedFormat) { setErr("errEntityKey", "Match format missing"); return; }
    const mid = ($("matchKey").value || "").trim();
    if (!mid) { setErr("errMatchKey", "Melbet match ID missing"); return; }
    setErr("errMatchUrl", "");
    setErr("errEntityKey", "");
    setErr("errMatchKey", "");
    await withBusy($("verifyBtn"), async () => {
      log("# Verifying match URL…");
      const res = await postJson("/api/verify-match-url", {
        url, match_id: mid,
        entity_match_id: S.selectedEntityId,
        format: S.selectedFormat,
        title: S.selectedFixture?.title || "",
      });
      if (res?.status === "success") {
        setVerified(true);
        log(`# ✅ Verified → melbet=${res.match_id} entity=${res.entity_match_id}`, "ok");
      } else {
        log(`# ❌ ${res?.message || "Verify failed"}`, "err");
        setErr("errMatchUrl", res?.message || "Verification failed");
      }
    });
  });

  // ----- Strategy checkboxes -----
  document.querySelectorAll("input.radio-look[data-kind]").forEach(cb => {
    cb.addEventListener("change", async () => {
      const kind = cb.dataset.kind;
      const target = $(cb.dataset.target);
      if (!cb.checked) { if (target) target.hidden = true; return; }

      document.querySelectorAll("input.radio-look[data-kind]").forEach(o => {
        if (o !== cb) { o.checked = false; const t = $(o.dataset.target); if (t) t.hidden = true; }
      });

      if (kind === "batsman" || kind === "wicket") {
        if (!S.workerLaunched[kind]) {
          const url = $("site").value.trim();
          if (!url) { cb.checked = false; setErr("errSite", "Website URL required"); return; }
          log(`# Launching ${kind} browser (separate profile)…`);
          const res = await postJson(`/api/launch-worker/${kind}`, { website_url: url });
          if (res?.status === "success") {
            S.workerLaunched[kind] = true;
            log(`# ✅ ${kind} browser opened — login + open verified match URL manually`, "ok");
            if (target) target.hidden = false;
          } else {
            cb.checked = false;
            log(`# ❌ ${kind} launch failed: ${res?.message}`, "err");
          }
        } else if (target) target.hidden = false;
      } else if (target) target.hidden = false;
    });
  });

  // ----- Mulling -----
  document.querySelectorAll(".mulling-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.kind;
      const wanted = !S.mulling[kind];
      if (wanted) {
        if (S.guardKilled) { log("# Kill-switch ON — আগে Resume করুন", "err"); return; }
        if (!S.isVerified) { log("# Verify a match first", "err"); return; }
        if (!S.activeEntityId) { log("# No active entity match", "err"); return; }
        if ((kind === "batsman" || kind === "wicket") && !S.workerLaunched[kind]) {
          log(`# Open ${kind} browser first (click checkbox)`, "err"); return;
        }
        const ok = await confirmDialog(`${kind.toUpperCase()} Mulling চালু?`,
          `${kind} worker সক্রিয়ভাবে বেট প্লেস শুরু করবে। নিশ্চিত?`, false);
        if (!ok) return;
      }
      await withBusy(btn, async () => {
        log(`# ${kind} mulling → ${wanted ? "ON" : "OFF"}…`);
        const res = await postJson(`/api/mulling/${kind}`, { enabled: wanted });
        if (res?.status === "success") {
          setMulling(kind, wanted);
          log(`# ✅ ${res.message}`, "ok");
        } else log(`# ❌ ${res?.message || "Mulling failed"}`, "err");
      });
    });
  });

  document.querySelectorAll(".btns .stop").forEach(btn => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.kind;
      if (!kind || !S.mulling[kind]) return;
      await withBusy(btn, async () => {
        const res = await postJson(`/api/mulling/${kind}`, { enabled: false });
        if (res?.status === "success") { setMulling(kind, false); log(`# ${kind} mulling stopped`, "warn"); }
      });
    });
  });

  // ----- beforeunload guard while mulling active -----
  window.addEventListener("beforeunload", (e) => {
    if (Object.values(S.mulling).some(Boolean)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
});