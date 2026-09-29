"use strict";

// crypto.randomUUID needs a secure context (https); plain LAN http does not have it.
const clientId = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256).toString(16).padStart(2, "0")).join("");

const $ = (id) => document.getElementById(id);
const NEUTRAL = 1500;

let ws = null;
let armedLocal = false; // this browser owns WEB control
const keys = new Set();
// e.code is layout independent: works with Russian layout (ц/ф/ы/в) and Caps Lock.
const KEY_CODES = { KeyW: "w", KeyA: "a", KeyS: "s", KeyD: "d" };
let kbActive = false; // sliders are currently driven by the keyboard

// Differential (tank) mixing for WASD. Which output is the left side and which way is
// "forward" depends on wiring, so it is configurable and stored in the browser.
const mix = Object.assign({ swap: false, inv1: false, inv2: false },
  JSON.parse(localStorage.getItem("mix") || "{}"));

// ---------- websocket ----------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?client_id=${clientId}`);
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => {
    renderOffline();
    disarmLocal();
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

setInterval(() => send({ type: "hb" }), 100);
setInterval(sendControl, 50);

function sendControl() {
  if (!armedLocal) return;
  send({ type: "control", ch1_us: +$("w1").value, ch2_us: +$("w2").value });
}

// ---------- REST buttons ----------

async function post(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Id": clientId },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!data.ok && data.error) toast(data.error);
    return data;
  } catch (e) {
    toast("Нет связи с Jetson: " + e);
    return { ok: false };
  }
}

function centerSliders() {
  setSlider(1, NEUTRAL);
  setSlider(2, NEUTRAL);
}

function disarmLocal() {
  if (armedLocal) centerSliders();
  armedLocal = false;
}

$("btn-stop").onclick = () => {
  disarmLocal();
  centerSliders();
  post("/api/stop");
};

$("btn-arm-rc").onclick = () => {
  disarmLocal();
  centerSliders();
  post("/api/arm/rc");
};

$("btn-arm-web").onclick = async () => {
  armedLocal = false;
  keys.clear();
  kbActive = false;
  centerSliders(); // sliders must start exactly at 1500
  const r = await post("/api/arm/web", { client_id: clientId });
  if (r.ok) {
    centerSliders();
    armedLocal = true;
  }
};

$("btn-center").onclick = () => {
  centerSliders();
  if (armedLocal) post("/api/center", { client_id: clientId });
};

window.addEventListener("pagehide", () => {
  if (armedLocal) navigator.sendBeacon("/api/stop?reason=pagehide");
});

// ---------- sliders & keyboard ----------

function setSlider(n, v) {
  $("w" + n).value = v;
  paintSlider(n);
}

function paintSlider(n) {
  const v = +$("w" + n).value;
  $("w" + n + "v").textContent = v + " us";
  setMeter($("w" + n).parentElement.querySelector(".meter"), v);
}

for (const n of [1, 2]) {
  $("w" + n).addEventListener("input", () => {
    paintSlider(n);
    sendControl();
  });
}
$("power").addEventListener("input", (e) => ($("pwv").textContent = e.target.value));

const clamp1 = (v) => Math.max(-1, Math.min(1, v));

// W/S = forward/back (both sides), A/D = spin in place, W+A / W+D = arc.
function applyKeys() {
  const p = +$("power").value;
  const throttle = (keys.has("w") ? 1 : 0) - (keys.has("s") ? 1 : 0);
  const turn = (keys.has("d") ? 1 : 0) - (keys.has("a") ? 1 : 0);
  if (throttle || turn) {
    const left = clamp1(throttle + turn) * p;
    const right = clamp1(throttle - turn) * p;
    let o1 = mix.swap ? right : left;
    let o2 = mix.swap ? left : right;
    if (mix.inv1) o1 = -o1;
    if (mix.inv2) o2 = -o2;
    setSlider(1, NEUTRAL + Math.round(o1));
    setSlider(2, NEUTRAL + Math.round(o2));
    kbActive = true;
  } else if (kbActive) {
    centerSliders();
    kbActive = false;
  }
  sendControl();
}

for (const k of ["swap", "inv1", "inv2"]) {
  $("mix-" + k).checked = mix[k];
  $("mix-" + k).addEventListener("change", (e) => {
    mix[k] = e.target.checked;
    localStorage.setItem("mix", JSON.stringify(mix));
    e.target.blur(); // keep Space/WASD for driving, not for toggling the checkbox
    applyKeys();
  });
}

document.addEventListener("keydown", (e) => {
  if (e.code === "Space") {
    e.preventDefault();
    $("btn-stop").click();
    return;
  }
  const k = KEY_CODES[e.code];
  if (!k || e.ctrlKey || e.metaKey || e.altKey) return;
  e.preventDefault();
  if (e.repeat || keys.has(k)) return;
  keys.add(k);
  applyKeys();
});

document.addEventListener("keyup", (e) => {
  const k = KEY_CODES[e.code];
  if (k && keys.delete(k)) applyKeys();
});

window.addEventListener("blur", () => {
  keys.clear();
  applyKeys();
});

// ---------- rendering ----------

// Bipolar meter: fill from the 1500 center line towards the value.
function setMeter(el, us, state) {
  el.classList.toggle("off", us == null);
  el.classList.toggle("idle", state === "idle");
  if (us == null) return;
  const pct = Math.max(0, Math.min(100, ((us - 1000) / 1000) * 100));
  const fill = el.querySelector(".fill");
  fill.style.left = Math.min(pct, 50) + "%";
  fill.style.width = Math.abs(pct - 50) + "%";
}

function setChip(id, text, cls) {
  const el = $(id);
  el.querySelector("b").textContent = text;
  el.className = "chip " + cls;
}

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
}

const MODE = {
  DISARMED: ["DISARMED", "warn", "Выходы в нейтрали 1500 / 1500. Выберите RC CONTROL или WEB CONTROL."],
  RC_ARMED: ["RC", "ok", "Выход повторяет штатный пульт."],
  WEB_ARMED: ["WEB", "ok", "Выход управляется из браузера."],
  FAULT: ["FAULT", "bad", "Выходы 1500 / 1500. Нужен ручной ARM."],
};
const SOURCE = { DISARMED: "нейтраль 1500", RC_ARMED: "пульт RC", WEB_ARMED: "браузер", FAULT: "нейтраль 1500 (FAULT)" };

function renderOffline() {
  setChip("c-jetson", "OFFLINE", "bad");
  const b = $("banner");
  b.className = "banner bad";
  $("mode").textContent = "OFFLINE";
  $("mode-text").textContent = "Нет связи с Jetson. Переподключение…";
}

function render(s) {
  setChip("c-jetson", "ONLINE", "ok");
  setChip("c-pwm", s.pwm_status, s.pwm_status === "READY" ? "ok" : s.pwm_status === "DRY-RUN" ? "warn" : "bad");
  setChip("c-rc", s.rc_status, s.rc_status === "CONNECTED" ? "ok" : "bad");

  const [label, cls, text] = MODE[s.mode];
  $("banner").className = "banner " + cls;
  $("mode").textContent = label;
  let detail = s.mode === "FAULT" && s.fault ? "Причина: " + s.fault + ". " + text : text;
  if (s.pwm_error) detail += " PWM: " + s.pwm_error;
  if (s.rc_error) detail += " RC input: " + s.rc_error;
  $("mode-text").textContent = detail;

  for (const n of [1, 2]) {
    const row = document.querySelector(`[data-ch="ch${n}"]`);
    const us = s[`ch${n}_us`];
    const st = s[`ch${n}_status`];
    const ok = st === "CONNECTED";
    const val = row.querySelector(".ch-val");
    val.textContent = us == null ? "—" : us + " us";
    val.classList.toggle("stale", !ok);
    const pill = row.querySelector(".pill");
    pill.textContent = st;
    pill.className = "pill " + (ok ? "ok" : "bad");
    setMeter(row.querySelector(".meter"), us, ok ? "" : "idle");
    const age = s[`ch${n}_age_ms`];
    const hz = s[`ch${n}_hz`];
    row.querySelector(".meta").textContent =
      (age == null ? "нет кадров" : "кадр " + age + " ms назад") + (hz == null ? "" : " · " + hz + " Hz");

    const out = document.querySelector(`[data-ch="out${n}"]`);
    out.querySelector(".ch-val").textContent = s[`out${n}_us`] + " us";
    out.querySelector(".sub").textContent = "реально " + s[`out${n}_actual_us`] + " us";
    setMeter(out.querySelector(".meter"), s[`out${n}_us`], s.mode === "RC_ARMED" || s.mode === "WEB_ARMED" ? "" : "idle");
  }
  $("source").textContent = SOURCE[s.mode];

  if (armedLocal && !(s.mode === "WEB_ARMED" && s.web_owner)) disarmLocal();

  $("btn-arm-rc").classList.toggle("active", s.mode === "RC_ARMED");
  $("btn-arm-web").classList.toggle("active", s.mode === "WEB_ARMED");

  $("web").classList.toggle("locked", !armedLocal);
  const wst = $("web-state");
  if (armedLocal) {
    wst.textContent = "ARMED — управляет выходом";
    wst.className = "web-state ok";
  } else if (s.mode === "WEB_ARMED") {
    wst.textContent = "управляет другой браузер";
    wst.className = "web-state warn";
  } else {
    wst.textContent = "не активно — ползунки не влияют на выход";
    wst.className = "web-state warn";
  }
}

paintSlider(1);
paintSlider(2);
connect();
