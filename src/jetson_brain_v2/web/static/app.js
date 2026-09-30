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
  $("w" + n + "v").textContent = v + " мкс";
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

$("ai-nats-url").value = localStorage.getItem("ai-nats-url") || "nats://127.0.0.1:4222";
$("ai-route").value = localStorage.getItem("ai-route") || "";
$("ai-route").addEventListener("change", () =>
  localStorage.setItem("ai-route", $("ai-route").value));
$("ai-nats-url").addEventListener("change", () =>
  localStorage.setItem("ai-nats-url", $("ai-nats-url").value.trim()));

$("btn-ai-save").onclick = async () => {
  const data = {
    swap: mix.swap, inv1: mix.inv1, inv2: mix.inv2,
    drive_delta_us: Number($("ai-drive").value),
    turn_delta_us: Number($("ai-turn").value),
    rc_nudge_mode: $("ai-rc-mode").value,
    rc_steering_side: $("ai-rc-side").value,
    rc_steering_sign: Number($("ai-rc-sign").value),
  };
  const result = await post("/api/ai/config", data);
  if (result.ok) toast("Проводка и параметры ИИ сохранены на Jetson");
};

$("btn-ai-arm").onclick = async () => {
  const natsUrl = $("ai-nats-url").value.trim();
  localStorage.setItem("ai-nats-url", natsUrl);
  const route = $("ai-route").value;
  localStorage.setItem("ai-route", route);
  const result = await post("/api/ai/start", { nats_url: natsUrl, route });
  if (result.ok) toast("ИИ запускается; движение начнётся только при свежей локализации");
};

$("btn-ai-stop").onclick = async () => {
  const result = await post("/api/ai/stop");
  if (result.ok) toast("ИИ остановлен");
};

let cameraShown = false;
$("btn-camera-view").onclick = () => {
  cameraShown = !cameraShown;
  const preview = $("camera-preview");
  preview.classList.toggle("hidden", !cameraShown);
  preview.src = cameraShown ? `/api/camera/stream.mjpg?t=${Date.now()}` : "";
  $("btn-camera-view").textContent = cameraShown ? "Скрыть поток" : "Показать поток";
};
$("btn-camera-record").onclick = async () => {
  const result = await post("/api/camera/record/start");
  if (result.ok) {
    toast(`Запись началась: ${result.file}`);
    refreshRecordings();
  }
};
$("btn-camera-stop").onclick = async () => {
  const result = await post("/api/camera/record/stop");
  if (result.ok) {
    toast(result.file ? `Запись сохранена: ${result.file}` : "Запись не шла");
    refreshRecordings();
  }
};

function renderFocus(state) {
  const range = $("focus-range");
  range.min = state.min;
  range.max = state.max;
  range.step = state.step;
  range.value = state.value;
  range.disabled = state.auto;
  $("focus-value").textContent = state.value;
  $("focus-state").textContent = state.auto ? "Автофокус включён" : "Фокус зафиксирован";
  $("btn-focus-auto").disabled = state.auto;
  $("btn-focus-manual").disabled = !state.auto;
}

async function loadFocus() {
  try {
    const response = await fetch("/api/camera/focus");
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    renderFocus(data.focus);
  } catch (error) {
    $("focus-state").textContent = `Настройки фокуса недоступны: ${error}`;
  }
}

$("btn-focus-auto").onclick = async () => {
  const result = await post("/api/camera/focus", { auto: true });
  if (result.ok) renderFocus(result.focus);
};
$("btn-focus-manual").onclick = async () => {
  const result = await post("/api/camera/focus", { auto: false });
  if (result.ok) renderFocus(result.focus);
};
$("focus-range").oninput = (event) => {
  $("focus-value").textContent = event.target.value;
};
$("focus-range").onchange = async (event) => {
  const result = await post("/api/camera/focus", { value: Number(event.target.value) });
  if (result.ok) renderFocus(result.focus);
};
loadFocus();

async function refreshRecordings() {
  try {
    const response = await fetch("/api/camera/recordings");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { recordings } = await response.json();
    const list = $("recordings-list");
    list.replaceChildren();
    $("recordings-state").textContent = recordings.length ? `${recordings.length} записей` : "Записей пока нет";
    for (const item of recordings) {
      const row = document.createElement("div");
      row.className = "recording-row";
      const info = document.createElement("div");
      info.className = "recording-info";
      const title = document.createElement("b");
      title.textContent = item.name;
      const details = document.createElement("small");
      details.textContent = `${new Date(item.modified * 1000).toLocaleString("ru-RU")} · ${(item.size / 1048576).toFixed(1)} МБ${item.recording ? " · идёт запись" : ""}`;
      info.append(title, details);
      const actions = document.createElement("div");
      actions.className = "recording-actions";
      const name = encodeURIComponent(item.name);
      const view = document.createElement("button");
      view.className = "small";
      view.textContent = "Смотреть";
      view.disabled = item.recording;
      view.onclick = async () => {
        const selected = item.name;
        selectedRecording = selected;
        const player = $("recording-player");
        player.pause();
        player.removeAttribute("src");
        player.load();
        player.classList.add("hidden");
        $("recordings-state").textContent = `Подготавливается ${selected}… Большая запись может занять несколько минут.`;
        const started = await post(`/api/camera/recordings/${name}/prepare`);
        if (!started.ok) return;
        while (selectedRecording === selected) {
          let state;
          try {
            const response = await fetch(`/api/camera/recordings/${name}/preview/status`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            state = await response.json();
          } catch (error) {
            $("recordings-state").textContent = `Не удалось подготовить видео: ${error}`;
            return;
          }
          if (selectedRecording !== selected) return;
          if (state.ready) {
            player.src = `/api/camera/recordings/${name}/preview`;
            player.classList.remove("hidden");
            player.scrollIntoView({ behavior: "smooth", block: "nearest" });
            $("recordings-state").textContent = `Готово к просмотру: ${selected}`;
            player.load();
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
      };
      const download = document.createElement("a");
      download.className = "small recording-download";
      download.textContent = "Скачать AVI";
      download.href = `/api/camera/recordings/${name}/download`;
      const remove = document.createElement("button");
      remove.className = "small recording-delete";
      remove.textContent = "Удалить";
      remove.disabled = item.recording;
      remove.onclick = async () => {
        if (!confirm(`Удалить запись ${item.name}? Восстановить её будет нельзя.`)) return;
        const result = await post(`/api/camera/recordings/${name}/delete`);
        if (!result.ok) return;
        if (selectedRecording === item.name) {
          selectedRecording = null;
          const player = $("recording-player");
          player.pause();
          player.removeAttribute("src");
          player.load();
          player.classList.add("hidden");
        }
        toast(`Запись удалена: ${item.name}`);
        refreshRecordings();
      };
      actions.append(view, download, remove);
      row.append(info, actions);
      list.append(row);
    }
  } catch (error) {
    $("recordings-state").textContent = `Не удалось загрузить записи: ${error}`;
  }
}

let selectedRecording = null;
$("recording-player").onerror = () => {
  if (!$("recording-player").classList.contains("hidden"))
    $("recordings-state").textContent = "Браузер не смог открыть видео. Попробуйте скачать AVI.";
};
$("btn-recordings-refresh").onclick = refreshRecordings;
refreshRecordings();

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
  DISARMED: ["НЕЙТРАЛЬ", "warn", "Выходы 1500 / 1500 мкс. Включите пульт или управление из браузера."],
  RC_ARMED: ["ПУЛЬТ", "ok", "Выход повторяет штатный пульт."],
  WEB_ARMED: ["БРАУЗЕР", "ok", "Выход управляется из браузера."],
  FAULT: ["ОШИБКА", "bad", "Выходы 1500 / 1500 мкс. После устранения причины включите управление вручную."],
};
const SOURCE = { DISARMED: "нейтраль 1500 мкс", RC_ARMED: "пульт", WEB_ARMED: "браузер", FAULT: "нейтраль 1500 мкс (ошибка)" };
const PWM_STATUS = { READY: "ГОТОВ", "DRY-RUN": "ПРОВЕРКА" };
const RC_STATUS = { CONNECTED: "СВЯЗЬ ЕСТЬ", LOST: "НЕТ СИГНАЛА", INVALID: "НЕВЕРНЫЙ СИГНАЛ" };
const VISION_MOVE = { straight: "прямо", left: "налево", right: "направо", stop: "стоп", lost: "локализация потеряна" };
let aiFieldsLoaded = false;

function renderOffline() {
  setChip("c-jetson", "НЕТ СВЯЗИ", "bad");
  setChip("c-nats", "—", "bad");
  $("vision-state").textContent = "Нет связи с Jetson";
  const b = $("banner");
  b.className = "banner bad";
  $("mode").textContent = "НЕТ СВЯЗИ";
  $("mode-text").textContent = "Нет связи с Jetson. Переподключение…";
}

function render(s) {
  setChip("c-jetson", "В СЕТИ", "ok");
  setChip("c-pwm", PWM_STATUS[s.pwm_status] || "ОШИБКА", s.pwm_status === "READY" ? "ok" : s.pwm_status === "DRY-RUN" ? "warn" : "bad");
  setChip("c-rc", RC_STATUS[s.rc_status] || s.rc_status, s.rc_status === "CONNECTED" ? "ok" : "bad");
  const nats = s.nats || {};
  setChip("c-nats", nats.connected ? "СВЯЗЬ ЕСТЬ" : "НЕТ СВЯЗИ", nats.connected ? "ok" : "bad");
  $("vision-state").textContent = !nats.connected
    ? `NATS не подключён: ${nats.error || nats.url || "проверьте брокер"}`
    : !nats.message_count
      ? `NATS подключён (${nats.subject}); команд от robot-vision ещё не было`
      : nats.recent
        ? `robot-vision: ${VISION_MOVE[nats.last_move_type] || "сообщение"} · ${nats.last_message_age_ms} мс назад · всего ${nats.message_count}`
        : `NATS подключён; нет свежих команд robot-vision (последняя ${Math.round(nats.last_message_age_ms / 1000)} с назад)`;

  const aiActive = !!(s.ai && s.ai.running);
  const [label, cls, text] = MODE[s.mode];
  $("banner").className = "banner " + cls;
  $("mode").textContent = aiActive && s.mode === "WEB_ARMED" ? "AI" : label;
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
    val.textContent = us == null ? "—" : us + " мкс";
    val.classList.toggle("stale", !ok);
    const pill = row.querySelector(".pill");
    pill.textContent = RC_STATUS[st] || st;
    pill.className = "pill " + (ok ? "ok" : "bad");
    setMeter(row.querySelector(".meter"), us, ok ? "" : "idle");
    const age = s[`ch${n}_age_ms`];
    const hz = s[`ch${n}_hz`];
    row.querySelector(".meta").textContent =
      (age == null ? "нет импульсов" : "импульс " + age + " мс назад") + (hz == null ? "" : " · " + hz + " Гц");

    const out = document.querySelector(`[data-ch="out${n}"]`);
    out.querySelector(".ch-val").textContent = s[`out${n}_us`] + " мкс";
    out.querySelector(".sub").textContent = "фактически " + s[`out${n}_actual_us`] + " мкс";
    setMeter(out.querySelector(".meter"), s[`out${n}_us`], s.mode === "RC_ARMED" || s.mode === "WEB_ARMED" ? "" : "idle");
  }
  $("source").textContent = aiActive && s.mode === "WEB_ARMED" ? "ИИ" : SOURCE[s.mode];

  const savedAi = s.ai && s.ai.config;
  if (savedAi && !aiFieldsLoaded) {
    $("ai-drive").value = savedAi.drive_delta_us;
    $("ai-turn").value = savedAi.turn_delta_us;
    $("ai-rc-mode").value = savedAi.rc_nudge_mode;
    $("ai-rc-side").value = savedAi.rc_steering_channel === savedAi.left_output ? "left" : "right";
    $("ai-rc-sign").value = savedAi.rc_steering_sign;
    aiFieldsLoaded = true;
  }
  if (savedAi) {
    const out1Sign = savedAi.left_output === 1 ? savedAi.left_sign : savedAi.right_sign;
    const out2Sign = savedAi.left_output === 2 ? savedAi.left_sign : savedAi.right_sign;
    const out1 = NEUTRAL + out1Sign * savedAi.drive_delta_us;
    const out2 = NEUTRAL + out2Sign * savedAi.drive_delta_us;
    $("ai-values").textContent = `Сохранено: тяга ${savedAi.drive_delta_us} мкс, поворот до ${savedAi.turn_delta_us} мкс. При команде «прямо»: OUT1 ${out1} мкс, OUT2 ${out2} мкс (заданные значения).`;
  } else {
    $("ai-values").textContent = s.ai && s.ai.config_error
      ? `Ошибка настроек ИИ: ${s.ai.config_error}`
      : "Параметры ИИ ещё не сохранены. В форме предложены тяга и поворот по 80 мкс от нейтрали 1500 мкс.";
  }

  $("ai-state").textContent = !s.ai || !s.ai.configured ? "ИИ: сначала сохраните проводку из WASD"
    : aiActive && s.mode === "WEB_ARMED" ? "ИИ управляет приводом; СТОП и пульт доступны"
    : aiActive ? "ИИ ждёт свежую локализацию"
    : s.ai.exit_code == null ? "ИИ готов к запуску" : `ИИ остановлен (код ${s.ai.exit_code})`;
  $("btn-ai-arm").classList.toggle("active", aiActive && s.mode === "WEB_ARMED");
  $("btn-ai-arm").disabled = aiActive || !s.ai || !s.ai.configured;
  $("btn-ai-save").disabled = aiActive;
  const aiLog = (s.ai && s.ai.log_tail || []).join("\n");
  if ($("ai-log").textContent !== aiLog) $("ai-log").textContent = aiLog;

  const camera = s.camera || {};
  $("camera-state").textContent = !camera.configured ? "Камера не настроена: создайте config/camera.json"
    : camera.online ? (camera.recording ? "Камера онлайн · запись идёт" : "Камера онлайн")
    : `Нет кадров с камеры${camera.error ? ": " + camera.error : ""}`;
  $("camera-file").textContent = camera.file || "";
  $("btn-camera-view").disabled = !camera.configured;
  $("btn-camera-record").disabled = !camera.online || camera.recording;
  $("btn-camera-stop").disabled = !camera.recording;

  if (armedLocal && !(s.mode === "WEB_ARMED" && s.web_owner)) disarmLocal();

  $("btn-arm-rc").classList.toggle("active", s.mode === "RC_ARMED");
  $("btn-arm-web").classList.toggle("active", s.mode === "WEB_ARMED" && !aiActive);

  $("web").classList.toggle("locked", !armedLocal);
  const wst = $("web-state");
  if (armedLocal) {
    wst.textContent = "включено — управляет выходом";
    wst.className = "web-state ok";
  } else if (s.mode === "WEB_ARMED") {
    wst.textContent = aiActive ? "управляет ИИ" : "управляет другой браузер";
    wst.className = "web-state warn";
  } else {
    wst.textContent = "не активно — ползунки не влияют на выход";
    wst.className = "web-state warn";
  }
}

paintSlider(1);
paintSlider(2);
connect();
