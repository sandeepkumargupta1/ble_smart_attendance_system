"use strict";

const byId = id => document.getElementById(id);
let token = null;
let identity = null;
let sessions = [];
let busy = false;
let captchaTimer = null;

function selectedMode() {
  const checked = document.querySelector("input[name='attendance-mode']:checked");
  return checked ? checked.value : "BLE";
}

function stopCaptchaCountdown() {
  if (captchaTimer) { clearInterval(captchaTimer); captchaTimer = null; }
}

function renderCaptcha(data) {
  // data: {captcha, captcha_expires_at} or {active: false}
  const panel = byId("captcha-panel");
  stopCaptchaCountdown();
  if (!data || data.active === false || !data.captcha_expires_at) {
    byId("captcha-code").textContent = "expired — refresh";
    byId("captcha-countdown").textContent = "—";
    return;
  }
  byId("captcha-code").textContent = data.captcha;
  const tick = () => {
    const remaining = Math.max(0, data.captcha_expires_at - Date.now());
    const mm = String(Math.floor(remaining / 60000)).padStart(2, "0");
    const ss = String(Math.floor(remaining % 60000 / 1000)).padStart(2, "0");
    byId("captcha-countdown").textContent = mm + ":" + ss;
    if (remaining <= 0) {
      byId("captcha-code").textContent = "expired — refresh";
      stopCaptchaCountdown();
    }
  };
  tick();
  captchaTimer = setInterval(tick, 1000);
}

async function refreshCaptchaPanel() {
  const panel = byId("captcha-panel");
  if (identity.role === "student" || !byId("session-select").value) {
    panel.hidden = true;
    stopCaptchaCountdown();
    return;
  }
  const session = sessions.find(s => s.session_id === byId("session-select").value);
  const needsCaptcha = session && session.attendance_mode === "BLE_CAPTCHA";
  panel.hidden = !needsCaptcha;
  if (!needsCaptcha) { stopCaptchaCountdown(); return; }
  try {
    const view = await request(selectedPath() + "/captcha");
    renderCaptcha(view.active ? view : null);
  } catch (error) {
    stopCaptchaCountdown();
    byId("captcha-code").textContent = "—";
  }
}

async function request(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request rejected: " + response.status);
  return data;
}

function options(select, rows, value, text) {
  const previous = select.value;
  select.replaceChildren(...rows.map(row => {
    const option = document.createElement("option");
    option.value = value(row);
    option.textContent = text(row);
    return option;
  }));
  if (rows.some(row => value(row) === previous)) select.value = previous;
}

function selectedPath() {
  const id = byId("session-select").value;
  if (!id) throw new Error("Start or select a session first.");
  return "/api/demo/sessions/" + encodeURIComponent(id);
}

async function refreshAttendance() {
  byId("attendance").replaceChildren();
  if (!byId("session-select").value) {
    options(byId("relay"), [""], s => s, () => "Direct");
    byId("session-status").textContent = "No sessions. Ask the teacher to start one.";
    return;
  }
  const session = sessions.find(s => s.session_id === byId("session-select").value);
  byId("session-status").textContent = session.status + " — " + session.subject + " — " +
    (session.attendance_mode === "BLE_CAPTCHA" ? "BLE + CAPTCHA" : "BLE") + " — SIMULATION";
  const rows = await request(selectedPath() + "/attendance");
  const thead = byId("attendance-head");
  const isBleCaptcha = session && session.attendance_mode === "BLE_CAPTCHA";
  if (thead) {
    if (isBleCaptcha) {
      thead.innerHTML = "<tr><th>Student</th><th>Name</th><th>BLE</th><th>CAPTCHA</th><th>Final Status</th></tr>";
    } else {
      thead.innerHTML = "<tr><th>Student</th><th>Name</th><th>Status</th><th>Route</th><th>Simulated RSSI</th></tr>";
    }
  }

  byId("attendance").replaceChildren(...rows.map(row => {
    const tr = document.createElement("tr");
    if (isBleCaptcha) {
      const tdStudent = document.createElement("td");
      tdStudent.textContent = row.student_id;

      const tdName = document.createElement("td");
      tdName.textContent = row.name;

      const tdBle = document.createElement("td");
      tdBle.innerHTML = row.ble_verified
        ? '<span style="color:#2e7d32;font-weight:600">✓ BLE</span>'
        : '<span style="color:#c62828;font-weight:600">✗ BLE</span>';

      const tdCaptcha = document.createElement("td");
      tdCaptcha.innerHTML = row.captcha_verified
        ? '<span style="color:#2e7d32;font-weight:600">✓ CAPTCHA</span>'
        : '<span style="color:#c62828;font-weight:600">✗ CAPTCHA</span>';

      const tdStatus = document.createElement("td");
      if (row.status === "PRESENT") {
        if (row.ble_verified && row.captcha_verified) {
          tdStatus.innerHTML = '<strong style="color:#2e7d32">PRESENT</strong>';
        } else if (row.ble_verified) {
          tdStatus.innerHTML = '<strong style="color:#2e7d32">PRESENT — BLE VERIFIED</strong>';
        } else if (row.captcha_verified) {
          tdStatus.innerHTML = '<strong style="color:#2e7d32">PRESENT — CAPTCHA VERIFIED</strong>';
        } else {
          tdStatus.innerHTML = '<strong style="color:#2e7d32">PRESENT</strong>';
        }
      } else if (row.status === "ELIGIBLE") {
        if (row.ble_verified && row.captcha_verified) {
          tdStatus.innerHTML = '<strong style="color:#1565c0">ELIGIBLE</strong>';
        } else if (row.ble_verified) {
          tdStatus.innerHTML = '<strong style="color:#1565c0">ELIGIBLE — BLE VERIFIED</strong>';
        } else if (row.captcha_verified) {
          tdStatus.innerHTML = '<strong style="color:#1565c0">ELIGIBLE — CAPTCHA VERIFIED</strong>';
        } else {
          tdStatus.innerHTML = '<strong style="color:#1565c0">ELIGIBLE</strong>';
        }
      } else {
        tdStatus.innerHTML = '<span style="color:#757575">NOT VERIFIED</span>';
      }

      tr.append(tdStudent, tdName, tdBle, tdCaptcha, tdStatus);
    } else {
      for (const value of [row.student_id, row.name, row.status, row.route || "—", row.rssi ?? "—"]) {
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.append(td);
      }
    }
    return tr;
  }));
  if (identity.role === "student") {
    const relays = session.status === "ACTIVE" ? await request(selectedPath() + "/relays") : [];
    options(byId("relay"), ["", ...relays], s => s, s => s || "Direct");
    byId("captcha-input-row").hidden = session.attendance_mode !== "BLE_CAPTCHA";
    if (session.attendance_mode !== "BLE_CAPTCHA") byId("captcha-input").value = "";
  }
}

async function refresh() {
  const classes = await request("/api/demo/classes");
  options(byId("class-select"), classes, c => c.class_id, c => c.class_name + " / " + c.subject);
  sessions = await request("/api/demo/sessions");
  options(byId("session-select"), sessions, s => s.session_id, s => s.class_id + " — " + s.status + " — " + s.session_id.slice(-8));
  await refreshAttendance();
  await refreshCaptchaPanel();
}

async function run(action) {
  if (busy) return;
  busy = true;
  const controls = Array.from(document.querySelectorAll("button, input, select"));
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  byId("workspace").setAttribute("aria-busy", "true");
  byId("message").textContent = "Working…";
  try {
    const message = await action();
    byId("message").textContent = message || "Updated.";
  } catch (error) {
    byId("message").textContent = error.message;
  } finally {
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    byId("workspace").setAttribute("aria-busy", "false");
    busy = false;
  }
}

byId("login-form").addEventListener("submit", event => {
  event.preventDefault();
  run(async () => {
    const data = await request("/api/auth/login", { username: byId("username").value, password: byId("password").value });
    token = data.access_token;
    identity = await request("/api/demo/profile");
    byId("password").value = "";
    byId("identity").textContent = identity.user_id + " / " + identity.role;
    byId("login-panel").hidden = true;
    byId("workspace").hidden = false;
    byId("teacher-panel").hidden = identity.role === "student";
    byId("admin-panel").hidden = identity.role !== "admin";
    byId("student-panel").hidden = identity.role !== "student";
    await refresh();
    return "Signed in. Tokens are kept only in this tab's memory.";
  });
});

byId("logout").addEventListener("click", () => {
  if (busy) return;
  token = null;
  identity = null;
  sessions = [];
  stopCaptchaCountdown();
  byId("captcha-panel").hidden = true;
  byId("captcha-input-row").hidden = true;
  byId("captcha-input").value = "";
  byId("class-select").replaceChildren();
  byId("session-select").replaceChildren();
  options(byId("relay"), [""], s => s, () => "Direct");
  byId("position").value = "near";
  byId("identity").textContent = "";
  byId("session-status").textContent = "";
  byId("attendance").replaceChildren();
  byId("workspace").hidden = true;
  byId("login-panel").hidden = false;
  byId("message").textContent = "Signed out. Relay opt-in remains session-bound; disable it before leaving if desired.";
});
byId("refresh").addEventListener("click", () => run(refresh));
byId("session-select").addEventListener("change", () => run(async () => {
  await refreshAttendance();
  await refreshCaptchaPanel();
}));
byId("start").addEventListener("click", () => run(async () => {
  const session = await request("/api/demo/sessions", {
    class_id: byId("class-select").value,
    attendance_mode: selectedMode()
  });
  await refresh();
  byId("session-select").value = session.session_id;
  await refreshAttendance();
  if (session.attendance_mode === "BLE_CAPTCHA") await refreshCaptchaPanel();
  return "Session started (" + (session.attendance_mode === "BLE_CAPTCHA" ? "BLE + CAPTCHA" : "BLE only") + "). Students can refresh their tabs.";
}));
byId("captcha-refresh").addEventListener("click", () => run(async () => {
  const data = await request(selectedPath() + "/captcha/refresh", {});
  renderCaptcha(data);
  return "New CAPTCHA generated. The previous code is now invalid.";
}));
byId("finalize").addEventListener("click", () => run(async () => {
  const result = await request(selectedPath() + "/finalize", {});
  await refresh();
  return "Finalized " + result.updated + " eligible records. Remaining students are NOT_VERIFIED, not absent.";
}));
byId("verify").addEventListener("click", () => run(async () => {
  const path = selectedPath();
  const session = sessions.find(s => s.session_id === byId("session-select").value);
  const isBleCaptcha = session && session.attendance_mode === "BLE_CAPTCHA";
  const captchaCode = isBleCaptcha ? byId("captcha-input").value.trim().toUpperCase() || null : null;
  let proof;
  try {
    proof = await request(path + "/challenge", {
      position: byId("position").value,
      relay_student_id: byId("relay").value || null,
      captcha_code: captchaCode
    });
  } catch (err) {
    if (isBleCaptcha && !err.message.includes("already recorded") && !err.message.includes("closed") && !err.message.includes("expired")) {
      throw new Error("Attendance not verified.\n\nBLE verification failed and CAPTCHA verification failed.");
    }
    throw err;
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(proof.nonce));
  const response = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  let result;
  try {
    result = await request(path + "/verify", {
      challenge_id: proof.challenge_id,
      response,
      captcha_code: captchaCode
    });
  } catch (err) {
    if (isBleCaptcha && !err.message.includes("already recorded") && !err.message.includes("closed") && !err.message.includes("expired")) {
      throw new Error("Attendance not verified.\n\nBLE verification failed and CAPTCHA verification failed.");
    }
    throw err;
  }
  await refreshAttendance();
  if (isBleCaptcha) {
    let method = "BLE";
    if (result.ble_verified && result.captcha_verified) {
      method = "BLE + CAPTCHA";
    } else if (result.captcha_verified) {
      method = "CAPTCHA";
    } else if (result.ble_verified) {
      method = "BLE";
    } else if (result.verification) {
      method = result.verification;
    }
    return "✓ Attendance Marked\n\nVerification:\n" + method;
  }
  const method = result.verification || (result.route || "accepted");
  return result.status + " (" + method + ") — simulation accepted by backend; teacher must finalize.";
}));
for (const [id, enabled] of [["relay-on", true], ["relay-off", false]]) {
  byId(id).addEventListener("click", () => run(async () => {
    await request(selectedPath() + "/relay", { enabled, position: byId("position").value });
    return "Session relay " + (enabled ? "enabled" : "disabled") + " (simulated).";
  }));
}
byId("join-form").addEventListener("submit", event => {
  event.preventDefault();
  run(async () => {
    await request("/api/v2/classes/join", { class_code: byId("join-code").value });
    await refresh();
    return "Class joined.";
  });
});
byId("class-form").addEventListener("submit", event => {
  event.preventDefault();
  run(async () => {
    await request("/api/classes", { class_id: byId("class-id").value, class_name: byId("class-name").value,
      subject: byId("subject").value, teacher_id: byId("teacher-id").value, class_code: byId("class-code").value });
    await refresh();
    return "Class created.";
  });
});
