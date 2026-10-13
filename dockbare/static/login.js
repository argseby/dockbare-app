const $ = (s) => document.querySelector(s);
document.documentElement.dataset.theme = localStorage.getItem("dockbare.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
const form = $("#login-form"), err = $("#error"), btn = $("#submit");
let countdown = null;

function showError(msg, type = "danger") { err.innerHTML = msg ? `<div class="banner ${type}">${msg}</div>` : ""; }

async function init() {
  try {
    const s = await fetch("/api/auth/session", { credentials: "same-origin" }).then((r) => r.json());
    if (s.authenticated) { location.href = "/admin"; return; }
    if (s.totp_enabled) $("#totp-group").classList.remove("hidden");
    if (!s.auth_configured) { $("#notice").innerHTML = `<div class="banner danger"><div><b>No admin password configured.</b> Set <code>DOCKBARE_ADMIN_PASSWORD</code> in your <code>.env</code> file and restart the container. All access is blocked until then.</div></div>`; btn.disabled = true; }
  } catch { /* offline; keep form */ }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  showError("");
  btn.disabled = true;
  const body = { username: $("#username").value.trim(), password: $("#password").value, totp: $("#totp").value };
  try {
    const r = await fetch("/api/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Requested-With": "Dockbare" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (r.ok) { location.href = "/admin"; return; }
    if (r.status === 429) {
      let wait = parseInt(r.headers.get("Retry-After") || "60", 10);
      clearInterval(countdown);
      const tick = () => { showError(`Too many failed attempts. Try again in ${wait}s.`, "warn"); if (wait-- <= 0) { clearInterval(countdown); showError(""); btn.disabled = false; } };
      tick(); countdown = setInterval(tick, 1000);
      return;
    }
    showError(d.error || "Sign-in failed.");
    $("#password").value = ""; $("#password").focus();
  } catch (e2) {
    showError("Network error: " + e2.message);
  } finally {
    if (!countdown) btn.disabled = false;
  }
});
init();
