(() => {
  'use strict';
  let data = window.ARCHIVE_DISCUSSIONS;
  if (!data) return;
  const $ = id => document.getElementById(id), params = new URLSearchParams(location.search);
  const person = params.get('person'), year = params.get('year');
  const byPublication = params.get('kind') === 'posts';
  let people = new Map(data.people.map(p => [p.id, p.name]));
  let posts = new Map(data.posts.map(p => [p.id, p]));
  const node = (tag, text, cls) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n;};
  const link = (url, text) => {const a = node('a', text); a.href = url; return a;};
  const date = text => {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?/.exec(text || '');
    return m ? `${m[3]}.${m[2]}.${m[1]}${m[4] ? ' · ' + m[4] : ''}` : 'Дата неизвестна';
  };
  let page = 0, pages = 1, groups = [];
  const expanded = new Map();
  function regroup() {
    people = new Map(data.people.map(p => [p.id, p.name]));
    posts = new Map(data.posts.map(p => [p.id, p]));
  $('discussion-title').textContent = [byPublication ? 'Записи' : people.get(person) || 'Обсуждения', year].filter(Boolean).join(' · ');
  const back = new URLSearchParams();
  if (person) back.set('person', person);
  if (year) back.set('year', year);
  $('discussion-back').href = 'statistics.html?' + back;
  const selectedPosts = new Set(data.posts.filter(p => !year || p.date?.startsWith(year + '-')).map(p => p.id));
  const matching = data.comments.filter(c => byPublication ? selectedPosts.has(c.entry) : (!person || c.person === person) && (!year || c.year === year));
  const grouped = new Map();
  if (byPublication) for (const id of selectedPosts) grouped.set(id, []);
  for (const c of matching) {if (!grouped.has(c.entry)) grouped.set(c.entry, []); grouped.get(c.entry).push(c);}
  groups = [...grouped].sort((a, b) => {
    if (byPublication) return (posts.get(b[0])?.date || '').localeCompare(posts.get(a[0])?.date || '') || b[0] - a[0];
    const left = a[1].at(-1), right = b[1].at(-1);
    return right.day.localeCompare(left.day) || right.id - left.id;
  });
  $('discussion-count').textContent = `Записей: ${groups.length.toLocaleString('ru')} · комментариев: ${matching.length.toLocaleString('ru')}`;
  pages = Math.max(1, Math.ceil(groups.length / 20));
    page = Math.min(page, pages - 1);
  }
  function render() {
    const list = $('discussion-list'); list.replaceChildren();
    for (const [eid, comments] of groups.slice(page * 20, page * 20 + 20)) {
      const post = posts.get(eid) || {title: `Запись #${eid}`, url: `entries/${eid}.html`};
      const card = node('article', undefined, 'discussion-card'), heading = node('h2');
      heading.append(link(post.url, post.title));
      card.append(node('p', `${date(post.date)} · комментариев: ${comments.length}`, 'fine'), heading);
      const items = node('ul', undefined, 'discussion-comments');
      const more = node('button', 'Ещё комментарии', 'btn text'); more.type = 'button';
      let shown = 0, batch = expanded.get(eid) || 20;
      function appendComments() {
        for (const c of comments.slice(shown, shown + batch)) {
          const item = node('li'), meta = link(c.url, date(c.date));
          meta.className = 'small'; item.append(meta);
          if (!person) item.append(node('span', ' · ' + (people.get(c.person) || 'Аноним'), 'small'));
          if (c.state_label) item.append(node('span', ' · ' + c.state_label, 'small muted'));
          item.append(node('p', c.excerpt)); items.append(item);
        }
        shown += batch; batch = 20; expanded.set(eid, shown); more.hidden = shown >= comments.length;
      }
      more.onclick = appendComments; appendComments(); card.append(items, more); list.append(card);
    }
    if (!groups.length) list.append(node('p', 'Нет записей по этим условиям', 'muted'));
    $('discussion-page').textContent = `${page + 1} / ${pages}`;
    $('discussion-prev').disabled = page === 0; $('discussion-next').disabled = page + 1 === pages;
  }
  $('discussion-prev').onclick = () => {page--; render(); window.scrollTo(0, 0);};
  $('discussion-next').onclick = () => {page++; render(); window.scrollTo(0, 0);};
  window.addEventListener('archive-statistics', event => {
    const scroll = [window.scrollX, window.scrollY], focus = document.activeElement, href = focus?.getAttribute('href');
    data = event.detail; regroup(); render();
    if (href && !focus.isConnected) [...document.querySelectorAll('a')].find(a => a.getAttribute('href') === href)?.focus({preventScroll:true});
    window.scrollTo(...scroll);
  });
  regroup(); render();
})();
