/* Shared helpers for every Inventra page. window.APP is injected by base.html. */
const APP = window.APP || {};

/* ---------- escaping: every dynamic value inserted via innerHTML must go through esc() ---------- */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- money (API uses integer cents) ---------- */
function money(cents) {
  const n = Number(cents || 0) / 100;
  const sign = n < 0 ? "-" : "";
  return sign + (APP.currency || "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function toCents(v) {
  const n = Math.round(parseFloat(v) * 100);
  return Number.isFinite(n) ? n : 0;
}
const tzOffset = () => -new Date().getTimezoneOffset(); // minutes east of UTC
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const debounce = (fn, ms = 250) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/* ---------- fetch wrapper ---------- */
async function api(url, { method = "GET", body, form } = {}) {
  const opts = { method, headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); } catch (e) { const err = new Error("Network error - check your connection"); err.network = true; throw err; }
  if (res.status === 401 && !url.startsWith("/api/login")) { window.location.href = "/login.html"; throw new Error("Session expired"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) { const err = new Error((data && data.error) || `Request failed (${res.status})`); err.status = res.status; throw err; }
  return data;
}

/* ---------- toast ---------- */
let toastTimer;
function toast(message, type = "success") {
  const el = document.getElementById("toast");
  if (!el) return;
  document.getElementById("toast-message").textContent = message;
  document.getElementById("toast-icon").className = type === "success" ? "fa-solid fa-circle-check" : "fa-solid fa-circle-exclamation";
  el.className = "fixed bottom-5 right-5 z-[60] transition-all duration-300 flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg text-white text-sm font-medium translate-y-0 opacity-100 " + (type === "success" ? "bg-emerald-600" : "bg-red-600");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("translate-y-20", "opacity-0"), 3200);
}

/* ---------- modals ---------- */
const openModal = (id) => document.getElementById(id)?.classList.remove("hidden");
const closeModal = (id) => document.getElementById(id)?.classList.add("hidden");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll("[data-modal]:not(.hidden)").forEach((m) => m.classList.add("hidden"));
});

function toggleMobileMenu() { document.getElementById("sidebar").classList.toggle("hidden"); }
async function logout() {
  await fetch("/api/logout", { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } });
  window.location.href = "/login.html";
}
async function submitPasswordChange(e) {
  e.preventDefault();
  try {
    await api("/api/me/password", { method: "POST", body: {
      current_password: document.getElementById("pw-current").value,
      new_password: document.getElementById("pw-new").value } });
    toast("Password updated");
    e.target.reset();
    closeModal("password-modal");
  } catch (err) { toast(err.message, "error"); }
}

/* ---------- product icons ---------- */
const ICON_RULES = [
  [["laptop", "macbook", "notebook pc"], "fa-laptop"], [["phone", "iphone", "mobile", "samsung", "pixel"], "fa-mobile-screen"],
  [["tv", "television", "display", "monitor", "screen"], "fa-tv"], [["mouse", "trackpad"], "fa-computer-mouse"],
  [["usb", "cable", "charger", "plug", "adapter"], "fa-plug"], [["headphone", "headset", "earphone", "airpods"], "fa-headphones"],
  [["keyboard", "keypad"], "fa-keyboard"], [["watch"], "fa-stopwatch"], [["camera", "webcam"], "fa-camera"],
  [["print", "scanner"], "fa-print"], [["speaker", "soundbar"], "fa-volume-high"], [["game", "ps5", "xbox", "controller"], "fa-gamepad"],
  [["shirt", "apparel", "clothing"], "fa-shirt"], [["book", "notebook", "journal"], "fa-book"], [["coffee", "drink", "cup", "mug"], "fa-mug-hot"],
];
function productIcon(p) {
  if (p.icon && p.icon !== "fa-box") return "fa-solid " + p.icon;
  const name = (p.name || "").toLowerCase();
  for (const [keys, icon] of ICON_RULES) if (keys.some((k) => name.includes(k))) return "fa-solid " + icon;
  return "fa-solid fa-box";
}

const CAT_STYLES = ["bg-blue-100 text-blue-700", "bg-emerald-100 text-emerald-700", "bg-amber-100 text-amber-700", "bg-violet-100 text-violet-700",
  "bg-rose-100 text-rose-700", "bg-cyan-100 text-cyan-700", "bg-orange-100 text-orange-700", "bg-teal-100 text-teal-700"];
function categoryStyle(name) {   // stable colour per category name
  if (!name) return "bg-gray-100 text-gray-500";
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return CAT_STYLES[h % CAT_STYLES.length];
}
function categoryBadge(name) {
  return name ? `<span class="px-2 py-0.5 text-[11px] font-medium rounded-full ${categoryStyle(name)}">${esc(name)}</span>` : '<span class="text-gray-300">-</span>';
}

function stockBadge(p) {
  if (p.stock === 0) return '<span class="px-2 py-0.5 text-[11px] font-semibold bg-red-100 text-red-700 rounded-full">Out of stock</span>';
  if (p.stock <= p.reorder_level) return '<span class="px-2 py-0.5 text-[11px] font-semibold bg-amber-100 text-amber-700 rounded-full">Low stock</span>';
  return '<span class="px-2 py-0.5 text-[11px] font-semibold bg-emerald-100 text-emerald-700 rounded-full">In stock</span>';
}

function statusBadge(status) {
  const map = { completed: ["Completed", "bg-emerald-100 text-emerald-700"], partially_refunded: ["Part. refunded", "bg-amber-100 text-amber-700"], refunded: ["Refunded", "bg-red-100 text-red-700"] };
  const [label, cls] = map[status] || [status, "bg-gray-100 text-gray-600"];
  return `<span class="px-2 py-0.5 text-[11px] font-semibold rounded-full ${cls}">${label}</span>`;
}

/* ---------- pagination control ---------- */
function renderPager(el, meta, onPage) {
  if (!el) return;
  if (meta.pages <= 1) { el.innerHTML = `<span class="text-xs text-gray-400">${meta.total} result${meta.total === 1 ? "" : "s"}</span>`; return; }
  el.innerHTML = `
    <span class="text-xs text-gray-500">${meta.total} results - page ${meta.page} of ${meta.pages}</span>
    <div class="flex gap-1">
      <button class="px-2.5 py-1 text-xs border rounded hover:bg-gray-50 disabled:opacity-40" ${meta.page <= 1 ? "disabled" : ""} data-p="${meta.page - 1}">Prev</button>
      <button class="px-2.5 py-1 text-xs border rounded hover:bg-gray-50 disabled:opacity-40" ${meta.page >= meta.pages ? "disabled" : ""} data-p="${meta.page + 1}">Next</button>
    </div>`;
  el.querySelectorAll("button[data-p]").forEach((b) => b.addEventListener("click", () => onPage(Number(b.dataset.p))));
}

/* ---------- receipt (shared by POS and Sales History) ---------- */
const PAY_LABEL = { cash: "Cash", card: "Card", upi: "UPI" };
function receiptHTML(s) {
  const rows = s.items.map((i) => `
    <div class="flex justify-between gap-2"><span class="break-words">${esc(i.name)} x${i.qty}</span><span>${money(i.unit_price_cents * i.qty)}</span></div>`).join("");
  const line = (label, value, cls = "") => `<div class="flex justify-between ${cls}"><span>${label}</span><span>${value}</span></div>`;
  return `
    <div class="text-center border-b pb-3">
      <i class="fa-solid fa-cube text-2xl text-indigo-600 mb-1"></i>
      <h2 class="text-base font-bold text-gray-900 tracking-wide uppercase">${esc(APP.store)}</h2>
      <p class="text-gray-500">Receipt ${esc(s.receipt_no)}</p>
      <p class="text-[10px] text-gray-400 mt-1">${esc(fmtDate(s.created_at))}${s.cashier ? " - " + esc(s.cashier) : ""}</p>
      ${s.customer_name ? `<p class="text-[10px] text-gray-400">Customer: ${esc(s.customer_name)}</p>` : ""}
    </div>
    <div class="space-y-1.5 py-3 border-b">${rows}</div>
    <div class="space-y-1 py-2">
      ${line("Subtotal", money(s.subtotal_cents))}
      ${s.discount_cents ? line("Discount", "-" + money(s.discount_cents)) : ""}
      ${s.tax_cents ? line(`Tax (${esc(APP.taxRate)}%)`, money(s.tax_cents)) : ""}
      ${line("Total", money(s.total_cents), "font-bold text-sm text-gray-900 border-t pt-1")}
      ${line(PAY_LABEL[s.payment_method] || s.payment_method, money(s.tendered_cents))}
      ${s.change_cents ? line("Change", money(s.change_cents)) : ""}
      ${s.refunded_cents ? line("Refunded", "-" + money(s.refunded_cents), "text-red-600") : ""}
    </div>
    ${APP.footer ? `<p class="text-center text-gray-500 pt-2">${esc(APP.footer)}</p>` : ""}`;
}
function showReceipt(sale) {
  document.getElementById("printable-receipt").innerHTML = receiptHTML(sale);
  openModal("receipt-modal");
}
