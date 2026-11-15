"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(" ");
  // Русское согласование числительных: 1 запись, 2 записи, 5 записей.
  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100, b = a % 10;
    return `${n} ${b === 1 && a !== 11 ? one : b >= 2 && b <= 4 && (a < 12 || a > 14) ? few : many}`;
  };
  const ruDate = (value) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(value || "");
    if (!m || !MONTHS[m[2] - 1]) return value || "Дата неизвестна";
    return `${Number(m[3])} ${MONTHS[m[2] - 1]} ${m[1]}` + (m[4] ? `, ${m[4]}:${m[5]}` + (/(?:UTC|Z|[+-]00:00)$/.test(value) ? " UTC" : "") : "");
  };

  // Список авторов реакции: открыт только один, закрывается по Esc и по нажатию мимо.
  document.addEventListener("toggle", (e) => {
    if (e.target.matches?.(".reaction-detail[open]"))
      for (const other of document.querySelectorAll(".reaction-detail[open]")) if (other !== e.target) other.open = false;
  }, true);
  document.addEventListener("click", (e) => {
    for (const open of document.querySelectorAll(".reaction-detail[open]")) if (!open.contains(e.target)) open.open = false;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    for (const open of document.querySelectorAll(".reaction-detail[open]")) { open.open = false; open.querySelector("summary").focus(); }
  });

  // Подсветка найденных слов: текст вставляется узлами, без innerHTML.
  function highlighted(cls, text, terms) {
    const node = el("p", cls), lower = text.toLocaleLowerCase("ru");
    if (lower.length !== text.length) { node.textContent = text; return node; }
    const spans = [];
    for (const term of terms) for (let at = lower.indexOf(term); at !== -1; at = lower.indexOf(term, at + term.length)) spans.push([at, at + term.length]);
    spans.sort((a, b) => a[0] - b[0]);
    let cursor = 0;
    for (const [from, to] of spans) {
      if (from < cursor) continue;
      node.append(text.slice(cursor, from), el("mark", "", text.slice(from, to))); cursor = to;
    }
    node.append(text.slice(cursor));
    return node;
  }

  function reactionsBlock(data) {
    if (data && !data.count) return null;
    const reactions = el("div", "reactions"); reactions.setAttribute("aria-label", "Реакции");
    if (!data) { reactions.append(el("span", "reactions-missing", "Реакции не загружены")); return reactions; }
    reactions.title = `Сохранено ${ruDate(data.captured_at)}`;
    for (const badge of data.badges) {
      const detail = el("details", "reaction-detail"), chip = el("summary", "reaction");
      const icon = el("span", "", badge.icon), arrow = el("span", "reaction-arrow", "▾");
      icon.setAttribute("aria-hidden", "true"); arrow.setAttribute("aria-hidden", "true");
      chip.append(icon, " ", el("span", "", String(badge.count)), arrow);
      chip.title = `${badge.label} — показать авторов`; chip.setAttribute("aria-label", `${badge.label}: ${badge.count}`);
      const people = el("div", "reaction-people"); people.append(el("strong", "", badge.label));
      const users = badge.users || [];
      if (users.length) {
        const list = el("ul");
        for (const user of users) {
          const item = el("li");
          if (user.url && /^https?:\/\//.test(user.url)) { const link = el("a", "", user.name); link.href = user.url; link.rel = "noreferrer noopener"; item.append(link); }
          else item.textContent = user.name;
          list.append(item);
        }
        people.append(list);
      }
      if (!badge.authors_complete) people.append(el("p", "fine", users.length ? `Получено авторов: ${users.length} из ${badge.count}.` : "Имена не загружены."));
      detail.append(chip, people); reactions.append(detail);
    }
    return reactions;
  }

  const search = $("search");
  if (search && window.ARCHIVE) {
    const {entries, documents} = window.ARCHIVE;
    const byEntry = new Map(entries.map(e => [e.id, e]));
    const normalized = documents.map(d => ({...d, haystack: (d.title + " " + d.text).toLocaleLowerCase("ru")}));
    const tagCounts = new Map();
    for (const e of entries) for (const tag of e.tags) tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
    const tags = [...tagCounts.keys()].sort((a, b) => a.localeCompare(b, "ru"));
    for (const tag of tags) { const o = el("option", "", tag); o.value = tag; $("tag").append(o); }
    const params = new URLSearchParams(location.search);
    for (const key of ["year", "tag", "security", "sort"]) if (params.has(key)) $(key).value = params.get(key);
    search.value = params.get("q") || "";
    let page = 1, timer;
    const PAGE_SIZE = 30;
    const FILTERS = ["year", "tag", "security"];
    const toggle = $("toggle-filters"), panel = $("filters");
    const showFilters = (open) => { panel.classList.toggle("is-open", open); toggle.setAttribute("aria-expanded", String(open)); };
    toggle.addEventListener("click", () => showFilters(!panel.classList.contains("is-open")));
    if (FILTERS.some(key => $(key).value)) showFilters(true);

    function pickTag(tag) {
      $("tag").value = tag; page = 1; render();
      if (FILTERS.some(key => $(key).value)) showFilters(true);
      search.scrollIntoView({block: "start"});
    }
    function tagLink(tag) {
      const link = el("a", "", tag); link.href = "?tag=" + encodeURIComponent(tag);
      link.addEventListener("click", (e) => { e.preventDefault(); pickTag(tag); });
      return link;
    }
    if (tags.length) {
      const cloud = $("tag-cloud");
      for (const tag of [...tags].sort((a, b) => tagCounts.get(b) - tagCounts.get(a) || a.localeCompare(b, "ru")).slice(0, 40)) {
        const link = tagLink(tag); link.append(" ", el("small", "", String(tagCounts.get(tag)))); cloud.append(link);
      }
      $("tag-box").hidden = false;
    }

    function render() {
      const query = search.value.trim().toLocaleLowerCase("ru");
      const terms = query.split(/\s+/).filter(Boolean);
      const year = $("year").value, tag = $("tag").value, security = $("security").value;
      const accepts = (e) => e && (!year || e.year === year) && (!tag || e.tags.includes(tag)) && (!security || e.security === security);
      let rows = terms.length ? normalized.filter(d => accepts(byEntry.get(d.entry)) && terms.every(t => d.haystack.includes(t))).map(d => ({e: byEntry.get(d.entry), d})) : entries.filter(accepts).map(e => ({e}));
      rows.sort((a, b) => ((b.e.date.localeCompare(a.e.date) || b.e.id - a.e.id) * ($("sort").value === "old" ? -1 : 1)));
      const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); page = Math.min(page, pages);
      const active = FILTERS.filter(key => $(key).value).length;
      $("results-count").textContent = terms.length ? plural(rows.length, "совпадение", "совпадения", "совпадений") : plural(rows.length, "запись", "записи", "записей");
      $("reset-filters").hidden = !active && !terms.length;
      toggle.textContent = active ? `Фильтры · ${active}` : "Фильтры";
      const list = $("entry-list"); list.replaceChildren();
      if (!rows.length) { const empty = el("div", "empty-state"); empty.append(el("h3", "", "Ничего не нашлось"), el("p", "", "Попробуйте другое слово или сбросьте фильтры.")); list.append(empty); }
      for (const {e, d} of rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)) {
        const card = el("article", "entry-card"), meta = el("div", "entry-meta"), title = el("h3"), link = el("a", "", e.title);
        const time = el("time", "", ruDate(e.date)); if (e.date) time.dateTime = e.date;
        link.href = d?.url || e.url; title.append(link); meta.append(time);
        if (e.security !== "public") meta.append(el("span", "badge lock", e.security_label));
        card.append(meta, title);
        if (d?.kind === "comment") card.append(el("div", "match-label", "Совпадение в комментарии"));
        let excerpt = e.excerpt;
        if (d && terms.length) { const position = d.text.toLocaleLowerCase("ru").indexOf(terms[0]); const start = Math.max(0, position - 70); excerpt = (start ? "…" : "") + d.text.slice(start, start + 300) + (start + 300 < d.text.length ? "…" : ""); }
        card.append(d ? highlighted("entry-excerpt result-snippet", excerpt, terms) : el("p", "entry-excerpt", excerpt));
        const bottom = el("div", "entry-card-bottom");
        if (e.comments) {
          const comments = el("a", "comments-link", plural(e.comments, "комментарий", "комментария", "комментариев") + (e.screened ? ` · ${plural(e.screened, "скрытый", "скрытых", "скрытых")}` : ""));
          comments.href = e.url + "#comments"; bottom.append(comments);
        } else bottom.append(el("span", "", "Без комментариев"));
        if (e.tags.length) { const tagList = el("span", "entry-tags"); tagList.append(...e.tags.map(tagLink)); bottom.append(tagList); }
        card.append(bottom);
        const reactions = reactionsBlock(e.reactions); if (reactions) card.append(reactions);
        list.append(card);
      }
      const pagination = $("pagination"); pagination.replaceChildren();
      if (pages > 1) {
        const prev = el("button", "", "← Назад"), next = el("button", "", "Далее →"); prev.type = next.type = "button"; prev.disabled = page === 1; next.disabled = page === pages;
        prev.onclick = () => { page--; render(); search.scrollIntoView({block: "start"}); }; next.onclick = () => { page++; render(); search.scrollIntoView({block: "start"}); };
        pagination.append(prev, el("span", "", `${page} из ${pages}`), next);
      }
      const state = new URLSearchParams(); if (query) state.set("q", search.value);
      for (const key of FILTERS) if ($(key).value) state.set(key, $(key).value);
      if ($("sort").value === "old") state.set("sort", "old");
      try { history.replaceState(null, "", location.pathname + (state.size ? "?" + state.toString() : "")); } catch {}
    }
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { page = 1; render(); }, 180); });
    for (const key of [...FILTERS, "sort"]) $(key).addEventListener("change", () => { page = 1; render(); });
    $("reset-filters").onclick = () => { search.value = ""; for (const key of FILTERS) $(key).value = ""; page = 1; render(); };
    document.addEventListener("keydown", e => { if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); search.focus(); } });
    render();
  }

  const comments = [...document.querySelectorAll(".comment")];
  const collapsed = new Set();
  // Число ответов в ветке показывается на свёрнутой кнопке.
  const replies = new Map();
  comments.forEach((c, index) => {
    const depth = Number(c.dataset.depth); let count = 0;
    for (let i = index + 1; i < comments.length && Number(comments[i].dataset.depth) > depth; i++) count++;
    replies.set(c.id, count);
  });
  function refreshThreads() {
    let hiddenBelow = null;
    for (const c of comments) {
      const depth = Number(c.dataset.depth);
      if (hiddenBelow !== null && depth <= hiddenBelow) hiddenBelow = null;
      c.hidden = hiddenBelow !== null;
      if (!c.hidden && collapsed.has(c.id)) hiddenBelow = depth;
      const b = c.querySelector(".thread-toggle");
      if (b) {
        b.textContent = collapsed.has(c.id) ? `Развернуть ветку · ${plural(replies.get(c.id), "ответ", "ответа", "ответов")}` : "Свернуть ветку";
        b.setAttribute("aria-expanded", String(!collapsed.has(c.id)));
      }
    }
  }
  for (const c of comments) c.querySelector(".thread-toggle")?.addEventListener("click", () => { collapsed.has(c.id) ? collapsed.delete(c.id) : collapsed.add(c.id); refreshThreads(); });
  $("expand-comments")?.addEventListener("click", () => { collapsed.clear(); refreshThreads(); });
  if (!comments.some(c => c.querySelector(".thread-toggle"))) $("expand-comments")?.setAttribute("hidden", "");
  function reveal() { if (!location.hash.startsWith("#comment-")) return; const target = document.getElementById(location.hash.slice(1)); if (target) { collapsed.clear(); refreshThreads(); target.scrollIntoView({block: "center"}); } }
  window.addEventListener("hashchange", reveal); reveal();
});
