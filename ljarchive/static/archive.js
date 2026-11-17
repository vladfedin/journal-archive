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
    if (!data) { reactions.append(el("span", "reactions-missing", "♡ —")); reactions.title = "Реакции не загружены"; return reactions; }
    reactions.title = `Сохранено ${ruDate(data.captured_at)}`;
    for (const badge of data.badges) {
      const detail = el("details", "reaction reaction-detail"), chip = el("summary");
      const icon = el("span", "", badge.icon);
      icon.setAttribute("aria-hidden", "true");
      if (badge.id < 2) { icon.textContent=""; icon.dataset.icon=badge.id===0?"heart":"pen"; }
      chip.append(icon, " ", el("span", "", String(badge.count)));
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
      if (!badge.authors_complete) people.append(el("p", "fine", users.length ? `${users.length} из ${badge.count}` : "Имена не загружены."));
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
    let view = params.get("view") || "full";
    if (!params.has("view")) try { view = localStorage.getItem("ljarchive-view") || view; } catch {}
    if (view !== "full") view = "compact";
    let selectedDate = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date')||'') ? params.get('date') : '';
    let selectedMonth = !selectedDate && /^\d{4}-\d{2}$/.test(params.get('month')||'') ? params.get('month') : '';
    const bodies = new Map(), pending = new Map();
    window.ArchivePost = (id, html) => {
      bodies.set(id, html);
      pending.get(id)?.(html);
      pending.delete(id);
      // Keep the reading cache bounded even in journals spanning decades.
      if (bodies.size > 60) bodies.delete(bodies.keys().next().value);
    };
    function fullBody(entry, container) {
      const show = html => { if (container.isConnected) container.innerHTML = html; };
      if (bodies.has(entry.id)) { container.innerHTML = bodies.get(entry.id); return; }
      const script = document.createElement("script");
      script.src = `assets/posts/${Number(entry.id)}.js`;
      pending.set(entry.id, show);
      script.onload = () => script.remove();
      script.onerror = () => {
        script.remove(); pending.delete(entry.id);
        const link = el("a", "", "Открыть запись"); link.href = entry.url;
        container.replaceChildren(link);
      };
      document.head.append(script);
    }
    document.querySelectorAll("[data-view]").forEach(button => button.onclick = () => {
      view = button.dataset.view; page = 1;
      try { localStorage.setItem("ljarchive-view", view); } catch {}
      render();
    });
    const FILTERS = ["year", "tag", "security"];

    function pickTag(tag) {
      $("tag").value = tag; page = 1; render();
      search.scrollIntoView({block: "start"});
    }
    function tagLink(tag) {
      const link = el("a", "", tag); link.href = "?tag=" + encodeURIComponent(tag);
      link.addEventListener("click", (e) => { e.preventDefault(); pickTag(tag); });
      return link;
    }
    if (tags.length && $("tag-cloud")) {
      const cloud = $("tag-cloud");
      for (const tag of [...tags].sort((a, b) => tagCounts.get(b) - tagCounts.get(a) || a.localeCompare(b, "ru")).slice(0, 40)) {
        const link = tagLink(tag); link.append(" ", el("small", "", String(tagCounts.get(tag)))); cloud.append(link);
      }
      $("tag-box").hidden = false;
    }

    function render() {
      const PAGE_SIZE = view === "full" ? 10 : 30;
      document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
      document.querySelector(".archive-main").classList.toggle("full-view", view === "full");
      const query = search.value.trim().toLocaleLowerCase("ru");
      const terms = query.split(/\s+/).filter(Boolean);
      const year = $("year").value, tag = $("tag").value, security = $("security").value;
      const period=selectedDate||selectedMonth;
      const accepts = (e) => e && (!year || e.year === year) && (!period || e.date.startsWith(period)) && (!tag || e.tags.includes(tag)) && (!security || e.security === security);
      let rows = terms.length ? normalized.filter(d => accepts(byEntry.get(d.entry)) && terms.every(t => d.haystack.includes(t))).map(d => ({e: byEntry.get(d.entry), d})) : entries.filter(accepts).map(e => ({e}));
      if (view === "full") {
        const seen = new Set();
        rows = rows.filter(row => { if (seen.has(row.e.id)) return false; seen.add(row.e.id); return true; });
      }
      rows.sort((a, b) => ((b.e.date.localeCompare(a.e.date) || b.e.id - a.e.id) * ($("sort").value === "old" ? -1 : 1)));
      const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); page = Math.min(page, pages);
      const active = FILTERS.filter(key => $(key).value).length;
      $("results-count").textContent = terms.length ? plural(rows.length, "совпадение", "совпадения", "совпадений") : plural(rows.length, "запись", "записи", "записей");
      $("reset-filters").hidden = !active && !terms.length && !period;
      $('date-filter').hidden=!period;
      $('date-filter').textContent=selectedDate?ruDate(selectedDate)+' ×':selectedMonth?('Январь Февраль Март Апрель Май Июнь Июль Август Сентябрь Октябрь Ноябрь Декабрь'.split(' ')[Number(selectedMonth.slice(5))-1]+' '+selectedMonth.slice(0,4)+' ×'):'';
      $('date-filter').setAttribute('aria-label','Снять ограничение по дате');

      const list = $("entry-list"); list.replaceChildren();
      if (!rows.length) { const empty = el("div", "empty-state"); empty.append(el("p", "", "Ничего не найдено")); list.append(empty); }
      let lastYear;
      for (const {e, d} of rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)) {
        if(e.year && e.year!==lastYear){const mark=el("a","year-mark",e.year);mark.href="calendar.html?year="+encodeURIComponent(e.year);mark.setAttribute("aria-label","Календарь за "+e.year);list.append(mark);lastYear=e.year;}
        const card = el("article", "entry-row entry-card"), meta = el("div", "entry-meta"), title = el("h2"), link = el("a", "", e.title);
        card.dataset.entryId=e.id;
        const time = el("time", "", ruDate(e.date)); if (e.date) time.dateTime = e.date;
        link.href = d?.url || e.url; title.append(link); meta.append(time);
        if (e.repost) meta.append(el("span", "state repost-label", "Перепост"));
        if (e.security !== "public") meta.append(el("span", "state", e.security_label));
        if (view === "full" && e.userpic?.path) {
          const avatar = el("a", "userpic-link"), image = el("img", "userpic");
          avatar.href = e.userpic.path; avatar.title = e.userpic.keyword || "Юзерпик";
          image.src = e.userpic.path; image.alt = e.userpic.keyword || "Юзерпик автора"; image.loading = "lazy";
          avatar.append(image); card.append(avatar);
        }
        card.append(meta, title);
        if (d?.kind === "comment") card.append(el("div", "match-label", "Совпадение в комментарии"));
        let excerpt = e.excerpt;
        if (d && terms.length) { const position = d.text.toLocaleLowerCase("ru").indexOf(terms[0]); const start = Math.max(0, position - 70); excerpt = (start ? "…" : "") + d.text.slice(start, start + 300) + (start + 300 < d.text.length ? "…" : ""); }
        if (d || view === "compact") card.append(d ? highlighted("entry-excerpt result-snippet", excerpt, terms) : el("p", "entry-excerpt", excerpt));
        const body = view === "full" ? el("div", "feed-body") : null;
        if (body) { card.classList.add("full-post"); card.append(body); }
        const bottom = el("div", "entry-bottom");
        if (e.comments) {
          const comments = el("a", "count-link"), icon = el("span");
          icon.dataset.icon = "comment"; comments.append(icon, String(e.comments));
          comments.setAttribute("aria-label", plural(e.comments, "комментарий", "комментария", "комментариев"));
          comments.href = e.url + "#comments"; bottom.append(comments);
        }
        if (e.tags.length) { const tagList = el("span", "entry-tags"); tagList.append(...e.tags.map(tagLink)); bottom.append(tagList); }
        card.append(bottom);
        const reactions = reactionsBlock(e.reactions); if (reactions) card.append(reactions);
        list.append(card);
        if (body) fullBody(e, body);
      }
      const pagination = $("pagination"); pagination.replaceChildren();
      if (pages > 1) {
        const prev = el("button", "btn", "← Назад"), next = el("button", "btn", "Далее →"); prev.type = next.type = "button"; prev.disabled = page === 1; next.disabled = page === pages;
        prev.onclick = () => { page--; render(); search.scrollIntoView({block: "start"}); }; next.onclick = () => { page++; render(); search.scrollIntoView({block: "start"}); };
        pagination.append(prev, el("span", "", `${page} из ${pages}`), next);
      }
      icons();
      const state = new URLSearchParams(); if (query) state.set("q", search.value);
      for (const key of FILTERS) if ($(key).value) state.set(key, $(key).value);
      if ($("sort").value === "old") state.set("sort", "old");
      state.set("view", view);
      if(selectedDate)state.set('date',selectedDate);
      if(selectedMonth)state.set('month',selectedMonth);
      try { history.replaceState(null, "", location.pathname + (state.size ? "?" + state.toString() : "")); } catch {}
    }
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { page = 1; render(); }, 180); });
    for (const key of [...FILTERS, "sort"]) $(key).addEventListener("change", () => { if(key==='year'){selectedDate='';selectedMonth='';}page = 1; render(); });
    $('date-filter').onclick=()=>{selectedDate='';selectedMonth='';page=1;render();};
    $("reset-filters").onclick = () => { search.value = ""; selectedDate='';selectedMonth='';for (const key of FILTERS) $(key).value = ""; page = 1; render(); };
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
    if($("expand-comments"))$("expand-comments").textContent=collapsed.size?"Развернуть ветки":"Свернуть ветки";
    let hiddenBelow = null;
    for (const c of comments) {
      const depth = Number(c.dataset.depth);
      if (hiddenBelow !== null && depth <= hiddenBelow) hiddenBelow = null;
      c.hidden = hiddenBelow !== null;
      if (!c.hidden && collapsed.has(c.id)) hiddenBelow = depth;
      const b = c.querySelector(".thread-toggle");
      if (b) {
        b.textContent = collapsed.has(c.id) ? `Развернуть · ${replies.get(c.id)}` : `Свернуть · ${replies.get(c.id)}`;
        b.setAttribute("aria-expanded", String(!collapsed.has(c.id)));
      }
    }
  }
  for (const c of comments) c.querySelector(".thread-toggle")?.addEventListener("click", () => { collapsed.has(c.id) ? collapsed.delete(c.id) : collapsed.add(c.id); refreshThreads(); });
  $("expand-comments")?.addEventListener("click", () => {
    const collapse=comments.some(c=>c.querySelector(".thread-toggle")&&!collapsed.has(c.id));
    collapsed.clear(); if(collapse)for(const c of comments)if(c.querySelector(".thread-toggle"))collapsed.add(c.id);
    refreshThreads();
  });
  if (!comments.some(c => c.querySelector(".thread-toggle"))) $("expand-comments")?.setAttribute("hidden", "");
  refreshThreads();
  function reveal() { if (!location.hash.startsWith("#comment-")) return; const target = document.getElementById(location.hash.slice(1)); if (target) { collapsed.clear(); refreshThreads(); target.scrollIntoView({block: "center"}); } }
  window.addEventListener("hashchange", reveal); reveal();
});
