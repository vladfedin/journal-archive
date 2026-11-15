"use strict";
const $ = id => document.getElementById(id);
let busy = false, polling = false;
const terminal = new Set(["complete", "partial", "cancelled", "error", "demo"]);
const labels = {waiting: "Пауза", login: "Вход", posts: "Записи", comments: "Комментарии", reactions: "Реакции", render: "Страницы", complete: "Готово", partial: "Частично", cancelled: "Остановлено", error: "Ошибка", demo: "Пример"};
const MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(" ");
// Русское согласование числительных: 1 запись, 2 записи, 5 записей.
const form = (n, one, few, many) => { const a = Math.abs(n) % 100, b = a % 10; return b === 1 && a !== 11 ? one : b >= 2 && b <= 4 && (a < 12 || a > 14) ? few : many; };
const postWord = n => form(n, "запись", "записи", "записей"), commentWord = n => form(n, "комментарий", "комментария", "комментариев");
const ruDay = value => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || ""); return m && MONTHS[m[2] - 1] ? `${Number(m[3])} ${MONTHS[m[2] - 1]} ${m[1]}` : ""; };

async function api(path, data) {
  const res = await fetch(path, {method: data === undefined ? "GET" : "POST", headers: data === undefined ? {} : {"Content-Type": "application/json"}, body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store"});
  const value = await res.json(); if (!res.ok) throw new Error(value.error || "Не удалось выполнить запрос."); return value;
}
function error(message) { $("form-error").textContent = message; $("form-error").hidden = !message; }
function setBusy(value) {
  busy = value; $("start").disabled = value; $("demo").disabled = value;
  for (const id of ["username", "password", "images", "delay", "entry-limit", "max-requests", "target-archive", "date-from", "date-to", "entry-urls"]) $(id).disabled = value;
  $("start").textContent = value ? "Архив создаётся…" : "Создать архив";
}
function showStatus(s) {
  if (!s.phase || s.phase === "idle") return;
  const appeared = $("job").hidden;
  $("job").hidden = false; $("job-phase").textContent = labels[s.phase] || s.phase; $("job-title").textContent = terminal.has(s.phase) ? "Результат выгрузки" : "Готовим архив"; $("job-message").textContent = s.message || "";
  if(s.phase === "waiting" && s.retry_at) $("job-message").textContent += ` Следующая попытка не раньше ${new Date(s.retry_at * 1000).toLocaleString("ru-RU")}.`;
  $("post-count").textContent = s.entries || 0; $("post-word").textContent = postWord(s.entries || 0);
  $("comment-count").textContent = s.comments || 0; $("comment-word").textContent = commentWord(s.comments || 0);
  $("request-count-wrap").hidden = !s.request_limit; $("request-count").textContent = `${s.requests_used || 0} / ${s.request_limit || 50}`;
  if (s.total) { $("job-progress").max = s.total; $("job-progress").value = s.completed || 0; } else $("job-progress").removeAttribute("value");
  $("job-progress").hidden = terminal.has(s.phase); $("cancel").hidden = terminal.has(s.phase); $("cancel").disabled = !!s.cancelling;
  $("open-result").hidden = !s.archive_url; if (s.archive_url) $("open-result").href = s.archive_url;
  setBusy(!terminal.has(s.phase));
  // Ход выгрузки появляется под формой: на телефоне он иначе остаётся за краем экрана.
  if (appeared && !terminal.has(s.phase)) $("job").scrollIntoView({block: "nearest"});
}
async function poll() {
  if (polling) return; polling = true;
  try { const s = await api("/api/status"); showStatus(s); if (terminal.has(s.phase)) await library(); }
  catch (e) { error("Связь с локальным приложением прервалась. Проверьте, что оно запущено; сохранённый прогресс останется на диске."); }
  finally { polling = false; setTimeout(poll, busy ? 1200 : 5000); }
}
async function library() {
  const data = await api("/api/archives"); $("output-path").textContent = data.output; $("library-count").textContent = data.archives.length; const list = $("archives"); list.replaceChildren();
  if (!data.archives.length) { const p = document.createElement("p"); p.className = "library-empty"; p.textContent = "Здесь появятся ваши сохранённые журналы."; list.append(p); }
  const selectedArchive=$("target-archive").value;
  $("target-archive").replaceChildren(new Option("Основной архив пользователя", ""));
  for (const archive of data.archives) {
    const row = document.createElement("div"); row.className = "archive-row";
    const info = document.createElement("div"), title = document.createElement("strong"), desc = document.createElement("p"), links = document.createElement("div");
    title.textContent = archive.username + (archive.entry_limit ? ` · последние ${archive.entry_limit}` : "");
    const saved = ruDay(archive.created_at);
    desc.textContent = [`${archive.entries_saved} ${postWord(archive.entries_saved)}`, `${archive.comments_saved} ${commentWord(archive.comments_saved)}`, labels[archive.status] || "Частично",
      archive.request_limit ? `запросов ${archive.requests_used} / ${archive.request_limit}` : "", saved ? `снимок от ${saved}` : ""].filter(Boolean).join(" · ");
    info.append(title, desc); links.className = "archive-row-links";
    for (const [text, href, cls] of [["Открыть", archive.url, "primary-button compact"], ["Отчёт", archive.report_url], ["Скачать JSON", archive.json_url], ["Скачать ZIP", archive.zip_url]]) { const a = document.createElement("a"); a.textContent = text; a.href = href; if (cls) a.className = cls; links.append(a); }
    if(archive.slug !== "_demo") {
      $("target-archive").append(new Option(archive.slug, archive.slug));
      const update=document.createElement("button"); update.type="button";update.className="text-button";update.textContent="Обновить архив";update.disabled=busy;
      update.onclick=()=>{$("username").value=archive.username;$("target-archive").value=archive.slug;$("entry-limit").value="period";scopeSettings();$("export").scrollIntoView({block:"start"});};links.append(update);
    }
    row.append(info, links); list.append(row);
  }
  $("target-archive").value=selectedArchive;
}
$("export-form").addEventListener("submit", async e => {
  e.preventDefault(); if (busy) return; error("");
  const selected=$("entry-limit").value;
  const data = {username: $("username").value, password: $("password").value, images: $("images").checked, delay: Number($("delay").value), entry_limit: selected === "10" ? 10 : null, max_requests: Number($("max-requests").value)};
  if(selected !== "10") Object.assign(data,{mode:selected === "all"?"full":selected, date_from:$("date-from").value||null,date_to:$("date-to").value||null,urls:$("entry-urls").value,archive:$("target-archive").value||null});
  setBusy(true);
  try { const result = await api("/api/export", data); $("password").value = ""; showStatus(result); } catch (e) { error(e.message); setBusy(false); } finally { data.password = ""; }
});
$("show-password").onclick = () => { const show = $("password").type === "password"; $("password").type = show ? "text" : "password"; $("show-password").textContent = show ? "Скрыть" : "Показать"; $("show-password").setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль"); $("show-password").setAttribute("aria-pressed", String(show)); };
$("cancel").onclick = async () => { try { $("cancel").disabled = true; showStatus(await api("/api/cancel", {})); } catch (e) { error(e.message); $("cancel").disabled = false; } };
$("demo").onclick = async () => { if (busy) return; error(""); setBusy(true); try { showStatus(await api("/api/demo", {})); } catch (e) { error(e.message); setBusy(false); } };
$("refresh-library").onclick = () => library().catch(e => error(e.message));
function scopeSettings(){const scope=$("entry-limit").value;$("sync-settings").hidden=scope==="10";$("period-settings").hidden=scope!=="period";$("url-settings").hidden=scope!=="urls";}
$("entry-limit").onchange=scopeSettings;
$("quit").onclick=async()=>{try{await api("/api/quit",{});$("quit").disabled=true;error("Приложение завершает работу с сохранением прогресса. Эту вкладку можно закрыть.");}catch(e){error(e.message);}};
library().catch(e => error(e.message)); poll();
