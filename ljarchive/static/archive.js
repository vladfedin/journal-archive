"use strict";
document.addEventListener("DOMContentLoaded", () => {
  function revealReportDetails() {
    const details = document.getElementById('skipped');
    if (details && location.hash === '#skipped') details.open = true;
  }
  revealReportDetails();
  window.addEventListener('hashchange', revealReportDetails);
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

  const singleTabClick = (event, link) => link && event.button === 0 && !event.defaultPrevented &&
    !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !link.target;
  const routeKey = "ljarchive.feed-to-entry";
  const returnKey = "ljarchive.entry-to-feed";
  const returnLink = document.querySelector('.reading-nav a[href="../index.html"]');
  if (returnLink) {
    // The referrer is sometimes empty for file://. Transfer the source for
    // a same-tab click once, then bind it to this particular history entry.
    let fromFeed = history.state?.fromJournalFeed;
    try {
      const route = JSON.parse(sessionStorage.getItem(routeKey));
      sessionStorage.removeItem(routeKey);
      if (route?.to === location.href && route.from === returnLink.href) {
        fromFeed = route.position;
        history.replaceState({...history.state, fromJournalFeed: fromFeed, entryUrl: location.href}, "");
      }
    } catch {}
    if (fromFeed) returnLink.addEventListener("click", event => {
      if (!singleTabClick(event, returnLink)) return;
      // A comment permalink can add another history entry within this post.
      if (history.state?.entryUrl === location.href && history.length > 1) {
        event.preventDefault();
        history.back();
        return;
      }
      try { sessionStorage.setItem(returnKey, JSON.stringify({to: returnLink.href, position: fromFeed})); } catch {}
    });
  }

  const search = $("search");
  if (search && window.ARCHIVE) {
    const {entries} = window.ARCHIVE;
    const byEntry = new Map(entries.map(e => [e.id, e]));
    const normalizeDocs = documents => documents.map(d => ({...d, haystack: (d.title + " " + d.text).toLocaleLowerCase("ru")}));
    let normalized = window.ARCHIVE.documents ? normalizeDocs(window.ARCHIVE.documents) : null;
    let loadingSearch = false;
    function loadSearch() {
      if (loadingSearch) return;
      loadingSearch = true;
      const script = document.createElement('script');
      script.src = 'assets/search-data.js';
      script.onload = () => {
        normalized = normalizeDocs(window.ARCHIVE.documents);
        script.remove(); if (search.value.trim()) render();
      };
      script.onerror = () => {
        loadingSearch = false; script.remove();
        if (search.value.trim()) $('results-count').textContent = 'Не удалось открыть поисковый индекс.';
      };
      document.head.append(script);
    }
    const tagCounts = new Map();
    for (const e of entries) for (const tag of e.tags) tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
    const tags = [...tagCounts.keys()].sort((a, b) => a.localeCompare(b, "ru"));
    for (const tag of tags) { const o = el("option", "", tag); o.value = tag; $("tag").append(o); }
    let restore = history.state?.journalFeed;
    try {
      const returning = JSON.parse(sessionStorage.getItem(returnKey));
      sessionStorage.removeItem(returnKey);
      if (returning?.to === location.href) restore = returning.position;
    } catch {}
    history.scrollRestoration = "manual";
    const params = new URLSearchParams(restore?.filters ?? location.search);
    for (const key of ["year", "tag", "security", "sort"]) if (params.has(key)) $(key).value = params.get(key);
    search.value = params.get("q") || "";
    let timer;
    let view = params.get("view") || "full";
    if (!params.has("view")) try { view = localStorage.getItem("ljarchive-view") || view; } catch {}
    if (view !== "full") view = "compact";
    let selectedDate = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date')||'') ? params.get('date') : '';
    let selectedMonth = !selectedDate && /^\d{4}-\d{2}$/.test(params.get('month')||'') ? params.get('month') : '';
    const bodies = new Map(), pending = new Map();
    let alignReadingPosition = null, resizeWatch = null;
    function releasePosition() {
      alignReadingPosition = null;
      resizeWatch?.disconnect();
    }
    for (const event of ["pointerdown", "wheel", "touchstart", "keydown"]) {
      window.addEventListener(event, releasePosition, {passive: true, capture: true});
    }
    window.ArchivePost = (id, html) => {
      bodies.set(id, html);
      pending.get(id)?.(html);
      pending.delete(id);
      // Keep the reading cache bounded even in journals spanning decades.
      if (bodies.size > 60) bodies.delete(bodies.keys().next().value);
    };
    function fullBody(entry, container) {
      const show = html => {
        container.style.minHeight = "";
        container.innerHTML = html;
        alignReadingPosition?.();
      };
      if (bodies.has(entry.id)) { show(bodies.get(entry.id)); return; }
      const script = document.createElement("script");
      script.src = `assets/posts/${Number(entry.id)}.js`;
      pending.set(entry.id, show);
      script.onload = () => script.remove();
      script.onerror = () => {
        script.remove(); pending.delete(entry.id);
        const link = el("a", "", "Открыть запись"); link.href = entry.url;
        container.style.minHeight = "";
        container.replaceChildren(link);
      };
      document.head.append(script);
    }
    document.querySelectorAll("[data-view]").forEach(button => button.onclick = () => {
      view = button.dataset.view;
      try { localStorage.setItem("ljarchive-view", view); } catch {}
      render();
    });
    const FILTERS = ["year", "tag", "security"];

    const searchToggle = $("feed-search-toggle");
    const optionsToggle = $("feed-options-toggle");
    const panels = [searchToggle, optionsToggle];
    function openPanel(toggle) {
      for (const button of panels) {
        const open = button === toggle;
        $(button.getAttribute("aria-controls")).hidden = !open;
        button.setAttribute("aria-expanded", String(open));
      }
      if (toggle === searchToggle) search.focus();
    }
    document.querySelector(".archive-sections a.nav-search").hidden = true;
    for (const button of panels) {
      button.hidden = false;
      button.onclick = () => openPanel(button.getAttribute("aria-expanded") === "true" ? null : button);
    }
    function syncSearchLink() {
      const q = search.value.trim();
      $("archive-search-link").href = q ? "search.html?q=" + encodeURIComponent(q) : "search.html";
    }
    syncSearchLink();

    function pickTag(tag) {
      $("tag").value = tag; render();
      optionsToggle.focus({preventScroll: true});
      optionsToggle.scrollIntoView({block: "center"});
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

    const list = $("entry-list");
    const more = el("button", "btn", "Показать ещё");
    more.type = "button";
    more.hidden = true;
    more.setAttribute("aria-controls", list.id);
    $("pagination").replaceChildren(more);
    let rows = [], shown = 0, lastYear, terms = [], pinFirst;
    let filters = params.toString();
    const observer = window.IntersectionObserver && new IntersectionObserver(records => {
      if (records.some(record => record.isIntersecting) && !more.hidden && document.activeElement !== more) appendBatch();
    }, {rootMargin: "0px 0px 300px"});
    const lazyBodies = window.IntersectionObserver && new IntersectionObserver(records => {
      for (const record of records) {
        if (!record.isIntersecting || !record.target.isConnected) continue;
        lazyBodies.unobserve(record.target);
        fullBody(byEntry.get(Number(record.target.closest("article").dataset.entryId)), record.target);
      }
    }, {rootMargin: "600px 0px"});

    more.onclick = () => {
      const next = shown;
      appendBatch();
      list.querySelector(`[data-row="${next}"] h2 a`)?.focus();
    };

    // Keep the reading position in this history entry, including offline files.
    function remember(card) {
      const state = {filters, shown, y: scrollY};
      if (view === "full") {
        state.width = list.clientWidth;
        state.heights = Array.from(list.querySelectorAll(".feed-body"), body => body.getBoundingClientRect().height);
      }
      if (card) {
        state.row = Number(card.dataset.row);
        state.href = card.querySelector("h2 a").getAttribute("href");
        state.offset = card.getBoundingClientRect().top;
      }
      try { history.replaceState({...history.state, journalFeed: state}, ""); } catch {}
      return state;
    }
    function visibleCard() {
      return document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.closest("#entry-list article");
    }
    window.addEventListener("pagehide", () => remember(visibleCard()));
    list.addEventListener("click", event => {
      const link = event.target.closest("a");
      if (!singleTabClick(event, link)) return;
      const card = link.closest("article");
      if (!card || !link.matches("h2 a, .count-link")) return;
      const position = remember(card);
      try {
        sessionStorage.setItem(routeKey, JSON.stringify({from: new URL("index.html", location.href).href, to: link.href, position}));
      } catch {}
    });

    function render() {
      releasePosition();
      observer?.disconnect();
      lazyBodies?.disconnect();
      more.hidden = true;
      shown = 0;
      lastYear = null;
      rows = [];
      document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
      document.querySelector(".archive-main").classList.toggle("full-view", view === "full");
      const query = search.value.trim().toLocaleLowerCase("ru");
      terms = query.split(/\s+/).filter(Boolean);
      const year = $("year").value, tag = $("tag").value, security = $("security").value;
      const period=selectedDate||selectedMonth;
      const accepts = (e) => e && (!year || e.year === year) && (!period || e.date.startsWith(period)) && (!tag || e.tags.includes(tag)) && (!security || e.security === security);
      const labels = [];
      if (query) labels.push("«" + search.value.trim() + "»");
      for (const key of FILTERS) if ($(key).value) labels.push($(key).selectedOptions[0].text);
      if ($("sort").value === "old") labels.push($("sort").selectedOptions[0].text);
      $("feed-selection-label").textContent = labels.join(" · ");
      $("feed-selection").hidden = !labels.length && !period;
      $("reset-filters").hidden = $("feed-selection").hidden;
      if (terms.length && !normalized) {
        $('results-count').textContent = 'Ищем…';
        loadSearch(); return;
      }
      rows = terms.length ? normalized.filter(d => accepts(byEntry.get(d.entry)) && terms.every(t => d.haystack.includes(t))).map(d => ({e: byEntry.get(d.entry), d})) : entries.filter(accepts).map(e => ({e}));
      if (view === "full") {
        const seen = new Set();
        rows = rows.filter(row => { if (seen.has(row.e.id)) return false; seen.add(row.e.id); return true; });
      }
      pinFirst = !terms.length && !period && !year && !tag && !security;
      rows.sort((a, b) => (pinFirst && Number(!!b.e.sticky) - Number(!!a.e.sticky)) ||
        (b.e.date.localeCompare(a.e.date) || b.e.id - a.e.id) * ($("sort").value === "old" ? -1 : 1));
      $("results-count").textContent = terms.length ? plural(rows.length, "совпадение", "совпадения", "совпадений") : plural(rows.length, "запись", "записи", "записей");
      $('date-filter').hidden=!period;
      $('date-filter').textContent=selectedDate?ruDate(selectedDate)+' ×':selectedMonth?('Январь Февраль Март Апрель Май Июнь Июль Август Сентябрь Октябрь Ноябрь Декабрь'.split(' ')[Number(selectedMonth.slice(5))-1]+' '+selectedMonth.slice(0,4)+' ×'):'';
      $('date-filter').setAttribute('aria-label','Снять ограничение по дате');

      list.replaceChildren();
      if (!rows.length) { const empty = el("div", "empty-state"); empty.append(el("p", "", "Ничего не найдено")); list.append(empty); }
      const state = new URLSearchParams(); if (query) state.set("q", search.value);
      for (const key of FILTERS) if ($(key).value) state.set(key, $(key).value);
      if ($("sort").value === "old") state.set("sort", "old");
      state.set("view", view);
      if(selectedDate)state.set('date',selectedDate);
      if(selectedMonth)state.set('month',selectedMonth);
      filters = state.toString();
      const url = new URL(location.href);
      url.search = filters;
      try { history.replaceState(null, "", url); } catch {}
      const previous = restore;
      restore = null;
      appendBatch(previous?.shown, previous);
      if (previous) {
        const card = list.querySelector(`[data-row="${Number(previous.row)}"]`);
        if (card && card.querySelector("h2 a").getAttribute("href") === previous.href) {
          alignReadingPosition = () => {
            const delta = card.getBoundingClientRect().top - previous.offset;
            if (Math.abs(delta) > 1) window.scrollBy({top: delta, behavior: "instant"});
          };
          alignReadingPosition();
          if (window.ResizeObserver) {
            resizeWatch = new ResizeObserver(() => alignReadingPosition?.());
            resizeWatch.observe(list);
          }
        } else {
          window.scrollTo({top: previous.y, behavior: "instant"});
        }
      }
      if (!more.hidden) observer?.observe(more);
    }

    function appendBatch(limit, previous) {
      const size = view === "full" ? 10 : 30;
      const end = Math.min(rows.length, Math.max(shown + size, Number(limit) || 0));
      const batch = document.createDocumentFragment();
      for (; shown < end; shown++) {
        const {e, d} = rows[shown];
        if(!(pinFirst && e.sticky) && e.year && e.year!==lastYear){const mark=el("a","year-mark",e.year);mark.href="calendar.html?year="+encodeURIComponent(e.year);mark.setAttribute("aria-label","Календарь за "+e.year);batch.append(mark);lastYear=e.year;}
        const card = el("article", "entry-row entry-card"), meta = el("div", "entry-meta"), title = el("h2"), link = el("a", "", e.title);
        if(e.author)meta.append(el("span", "author", e.author));
        card.dataset.entryId=e.id;
        card.dataset.row=shown;
        const time = el("time", "", ruDate(e.date)); if (e.date) time.dateTime = e.date;
        link.href = d?.url || e.url; title.append(link); meta.append(time);
        if (e.sticky) meta.append(el("span", "state", "Закреплено"));
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
        batch.append(card);
        if (body) {
          const height = previous?.width === list.clientWidth && previous.heights?.[shown];
          if (height > 0) body.style.minHeight = height + "px";
          if (previous && lazyBodies) lazyBodies.observe(body);
          else fullBody(e, body);
        }
      }
      icons(batch);
      list.append(batch);
      more.hidden = shown >= rows.length;
      if (more.hidden) observer?.disconnect();
    }
    search.addEventListener("input", () => { syncSearchLink(); clearTimeout(timer); timer = setTimeout(render, 180); });
    for (const key of [...FILTERS, "sort"]) $(key).addEventListener("change", () => { if(key==='year'){selectedDate='';selectedMonth='';}render(); });
    $('date-filter').onclick=()=>{selectedDate='';selectedMonth='';render();optionsToggle.focus();};
    $("reset-filters").onclick = () => {
      search.value = ""; selectedDate = ""; selectedMonth = "";
      for (const key of FILTERS) $(key).value = "";
      $("sort").value = "new";
      syncSearchLink(); render(); optionsToggle.focus();
    };
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
        event.preventDefault(); openPanel(searchToggle);
      }
      if (event.key === "Escape") {
        const open = panels.find(button => button.getAttribute("aria-expanded") === "true");
        if (open) { event.preventDefault(); openPanel(null); open.focus(); }
      }
    });
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
