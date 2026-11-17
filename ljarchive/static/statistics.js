(() => {
  'use strict';
  let data = window.ARCHIVE_STATS;
  if (!data) return;
  const $ = id => document.getElementById(id);
  let people = new Map(data.people.map(p => [p.id, p]));
  const number = n => new Intl.NumberFormat('ru').format(n);
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const link = (url, text) => {
    const n = node('a', text); n.href = url; return n;
  };
  const discussionURL = (person, year) => 'discussions.html?' + new URLSearchParams({person, ...(year ? {year} : {})});
  $('stats-year').replaceChildren(new Option('Все годы', ''), ...data.years.map(y => new Option(y, y)));
  const initial = new URLSearchParams(location.search);
  if (initial.has('year')) $('stats-year').value = initial.get('year');
  if (people.has(initial.get('person'))) {
    const person = people.get(initial.get('person'));
    $('stats-search').value = person.name;
  }
  let page = 0, edgeLimit = 40;
  function metric(person, year) {
    const row = year ? person.years[year] : person;
    if (!row) return 0;
    const mode = $('stats-metric').value;
    if (mode === 'share') {
      const total = year ? data.yearly[year]?.comments : data.totals.comments;
      return total ? row.comments * 100 / total : 0;
    }
    return row[mode];
  }
  const format = n => $('stats-metric').value === 'share' ? n.toLocaleString('ru', {maximumFractionDigits: 1}) + '%' : number(n);
  function fitYears() {
    const table = $('stats-people'), style = getComputedStyle(table);
    const fixed = parseFloat(style.getPropertyValue('--stats-name-width')) + parseFloat(style.getPropertyValue('--stats-total-width'));
    const minimum = 38;
    const width = Math.max(minimum, Math.min(56, Math.floor((table.parentElement.clientWidth - fixed) / Math.max(1, data.years.length))));
    table.style.setProperty('--stats-year-width', width + 'px');
  }
  new ResizeObserver(fitYears).observe($('stats-people').parentElement);
  function render() {
    const year = $('stats-year').value, own = $('stats-owner').checked;
    const query = $('stats-search').value.trim().toLocaleLowerCase();
    const matches = p => p.name.toLocaleLowerCase().includes(query);
    const visible = data.people.filter(p => (own || !p.owner) && matches(p) && (!year || p.years[year]))
      .sort((a, b) => metric(b, year) - metric(a, year) || a.name.localeCompare(b.name));
    const pages = Math.max(1, Math.ceil(visible.length / 30));
    page = Math.min(page, pages - 1);
    const table = $('stats-people'); table.replaceChildren();
    table.classList.toggle('stats-percent', $('stats-metric').value === 'share');
    table.style.setProperty('--stats-years', String(data.years.length));
    fitYears();
    const head = node('thead'), hr = node('tr');
    const columns = ['Участник', year || 'Всего', ...data.years];
    columns.forEach(value => { const th = node('th', value); th.scope = 'col'; hr.append(th); });
    head.append(hr); table.append(head);
    const totals = node('tbody', undefined, 'stats-totals');
    for (const [label, kind, all, values] of [
      ['Комментарии', 'comments', data.totals.comments, Object.fromEntries(data.years.map(y => [y, data.yearly[y]?.comments || 0]))],
      ['Записи', 'posts', data.totals.posts, data.published || {}]
    ]) {
      const row = node('tr'), heading = node('th', label); heading.scope = 'row'; row.append(heading);
      for (const y of [year, ...data.years]) {
        const value = y ? values[y] || 0 : all, cell = node('td');
        if (value) cell.append(link('discussions.html?' + new URLSearchParams({kind, ...(y ? {year: y} : {})}), number(value)));
        else cell.textContent = '0';
        row.append(cell);
      }
      totals.append(row);
    }
    table.append(totals);
    const body = node('tbody', undefined, 'stats-participants');
    const subset = visible.slice(page * 30, page * 30 + 30);
    let peak = 0;
    for (const p of data.people.filter(p => own || !p.owner)) for (const y of data.years) peak = Math.max(peak, metric(p, y));
    for (const p of subset) {
      const tr = node('tr'), name = node('th', p.name); name.scope = 'row'; name.title = p.name;
      const total = node('td', undefined, 'stats-total'); total.append(link(discussionURL(p.id, year), format(metric(p, year))));
      tr.append(name, total);
      for (const y of data.years) {
        const td = node('td');
        if (p.years[y]) {
          const value = metric(p, y), a = link(discussionURL(p.id, y), format(value));
          if ($('stats-metric').value === 'share') {
            a.replaceChildren(node('span', value.toLocaleString('ru', {maximumFractionDigits: 1})), node('small', '%'));
          }
          a.className = 'heat heat-' + Math.max(1, Math.ceil(Math.log1p(value) / Math.log1p(peak || 1) * 5));
          a.title = `${p.name} · ${y}: ${format(value)} · ${number(p.years[y].comments)} комм. / ${number(p.years[y].posts)} записей`;
          td.append(a);
        } else {
          const zero = node('span', '·', 'heat heat-zero'); zero.title = 'Нет сохранённых комментариев'; td.append(zero);
        }
        if (y === year) td.classList.add('stats-selected');
        tr.append(td);
      }
      body.append(tr);
    }
    if (!subset.length) { const tr = node('tr'), td = node('td', 'Нет участников по этим условиям', 'muted'); td.colSpan = columns.length; tr.append(td); body.append(tr); }
    table.append(body);
    $('stats-page').textContent = `${page + 1} / ${pages} · ${number(visible.length)}`;
    $('stats-prev').disabled = page === 0; $('stats-next').disabled = page + 1 >= pages;
    const edges = data.edges.map(e => ({...e, count: year ? e.years[year]?.count || 0 : e.count,
      url: year ? e.years[year]?.url : e.examples[0]})).filter(e => e.count &&
      (own || (!people.get(e.source).owner && !people.get(e.target).owner)) &&
      (matches(people.get(e.source)) || matches(people.get(e.target))))
      .sort((a, b) => b.count - a.count || a.source.localeCompare(b.source));
    drawGraph(edges);
    const rows = $('stats-edges'); rows.replaceChildren();
    for (const edge of edges.slice(0, edgeLimit)) {
      const tr = node('tr');
      tr.append(node('td', people.get(edge.source).name), node('td', people.get(edge.target).name), node('td', number(edge.count)));
      const td = node('td'); td.append(link(edge.url, 'Открыть ↗')); tr.append(td); rows.append(tr);
    }
    if (!edges.length) { const tr = node('tr'), td = node('td', 'Прямых ответов между участниками пока нет', 'muted'); td.colSpan = 4; tr.append(td); rows.append(tr); }
    $('stats-more-edges').hidden = edges.length <= edgeLimit;
  }
  function drawGraph(edges) {
    const svg = $('stats-graph'), ns = 'http://www.w3.org/2000/svg';
    const element = (tag, attrs = {}, text) => {
      const n = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k, v));
      if (text !== undefined) n.textContent = text;
      return n;
    };
    svg.replaceChildren(element('title', {}, 'Кто кому отвечал'));
    svg.toggleAttribute('hidden', !edges.length);
    if (!edges.length) return;
    const chosen = [], ids = new Set();
    for (const e of edges) {
      const extra = Number(!ids.has(e.source)) + Number(!ids.has(e.target));
      if (ids.size + extra > 20) continue;
      ids.add(e.source); ids.add(e.target); chosen.push(e);
      if (chosen.length === 40) break;
    }
    const positions = new Map([...ids].map((id, i) => {
      const angle = i * Math.PI * 2 / ids.size - Math.PI / 2;
      return [id, {x: 380 + Math.cos(angle) * 260, y: 245 + Math.sin(angle) * 194}];
    }));
    const defs = element('defs'), marker = element('marker', {id:'stats-arrow', viewBox:'0 0 10 10', refX:9, refY:5, markerWidth:5, markerHeight:5, orient:'auto-start-reverse'});
    marker.append(element('path', {d:'M 0 0 L 10 5 L 0 10 z', class:'graph-arrow'})); defs.append(marker); svg.append(defs);
    const max = Math.max(...chosen.map(e => e.count));
    for (const edge of chosen) {
      const a = positions.get(edge.source), b = positions.get(edge.target);
      const dx=b.x-a.x, dy=b.y-a.y, length=Math.hypot(dx,dy), ux=dx/length, uy=dy/length;
      const anchor = element('a', {href:edge.url, 'aria-label':`${people.get(edge.source).name} → ${people.get(edge.target).name}: ${edge.count}`});
      anchor.append(element('title', {}, `${people.get(edge.source).name} → ${people.get(edge.target).name}: ${number(edge.count)}`));
      anchor.append(element('path', {d:`M ${a.x+ux*10} ${a.y+uy*10} Q ${(a.x+b.x)/2-uy*18} ${(a.y+b.y)/2+ux*18} ${b.x-ux*13} ${b.y-uy*13}`,
        class:'graph-edge graph-weight-' + Math.max(1, Math.ceil(edge.count/max*3)), 'marker-end':'url(#stats-arrow)'}));
      svg.append(anchor);
    }
    for (const [id, p] of positions) {
      const person = people.get(id), example = chosen.find(e => e.source === id || e.target === id);
      const anchor = element('a', {href:example.url, 'aria-label':person.name});
      anchor.append(element('circle', {cx:p.x, cy:p.y, r:7, class:'graph-person'}));
      anchor.append(element('text', {x:p.x, y:p.y + (p.y>245?22:-16), 'text-anchor':'middle', class:'graph-label'},
        person.name.length > 22 ? person.name.slice(0,20)+'…' : person.name));
      anchor.append(element('title', {}, person.name)); svg.append(anchor);
    }
  }
  function findings() {
    const years = days => (days / 365.2425).toLocaleString('ru', {maximumFractionDigits:1});
    for (const [target, rows, returns] of [['stats-revivals', data.revivals, false], ['stats-returns', data.returns, true]]) {
      const container = $(target);
      container.replaceChildren();
      if (!rows.length) { container.append(node('p', 'Пока нет таких пауз в сохранённых данных', 'muted')); continue; }
      for (const row of rows) {
        const card = node('article', undefined, 'stats-finding');
        card.append(node('span', `Пауза · ${years(row.days)} г.`, 'stats-gap'));
        const heading = node('h3'); heading.append(link(row.url, returns ? people.get(row.person).name : row.title)); card.append(heading);
        const detail = node('p', row.date + (row.first_comment ? ' · первый сохранённый комментарий' : ' · обсуждение продолжилось'), 'fine');
        detail.append(document.createTextNode(' · '), link(row.previous_url, 'До паузы')); card.append(detail); container.append(card);
      }
    }
  }
  document.querySelectorAll('[data-stats-tab]').forEach(button => button.addEventListener('click', () => {
    const findings = button.dataset.statsTab === 'findings';
    $('stats-explore').hidden = findings; $('stats-findings').hidden = !findings;
    document.querySelectorAll('[data-stats-tab]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  }));
  ['stats-year','stats-metric','stats-owner'].forEach(id => $(id).addEventListener('change', () => {page=0; edgeLimit=40; render();}));
  let timer;
  $('stats-search').addEventListener('input', () => {clearTimeout(timer); timer=setTimeout(() => {page=0; edgeLimit=40; render();},120);});
  $('stats-prev').addEventListener('click', () => {page--; render();});
  $('stats-next').addEventListener('click', () => {page++; render();});
  $('stats-more-edges').addEventListener('click', () => {edgeLimit+=40; render();});
  let hydrated = !!data.generated_at;
  window.addEventListener('archive-statistics', event => {
    const year = $('stats-year').value || (!hydrated ? initial.get('year') : ''), focus = document.activeElement;
    const href = focus?.getAttribute('href'), scroll = [window.scrollX, window.scrollY];
    const scrolls = [...document.querySelectorAll('.stats-scroll')].map(el => [el, el.scrollLeft]);
    data = event.detail; people = new Map(data.people.map(p => [p.id, p]));
    if (!hydrated && !$('stats-search').value && people.has(initial.get('person'))) $('stats-search').value = people.get(initial.get('person')).name;
    hydrated = true;
    const years = ['', ...data.years];
    if (JSON.stringify([...$('stats-year').options].map(o => o.value)) !== JSON.stringify(years)) {
      $('stats-year').replaceChildren(...years.map(y => new Option(y || 'Все годы', y)));
      $('stats-year').value = years.includes(year) ? year : '';
    }
    if ($('stats-summary')) $('stats-summary').textContent = `Участников: ${number(data.totals.people)} · Комментариев: ${number(data.totals.comments)}` + (data.years.length ? ` · ${data.years[0]}–${data.years.at(-1)}` : '');
    if ($('stats-coverage')) $('stats-coverage').hidden = data.coverage.discussions_complete && data.coverage.posts_complete;
    if ($('stats-count-notes')) $('stats-count-notes').textContent = `Без известной даты: ${data.totals.unknown_dates}. Без известного автора: ${data.totals.anonymous}. Удалённых: ${data.totals.deleted}. Отсутствующих родителей: ${data.totals.missing_parents}.`;
    render(); findings();
    if (href && !focus.isConnected) [...document.querySelectorAll('a')].find(a => a.getAttribute('href') === href)?.focus({preventScroll:true});
    for (const [el, left] of scrolls) el.scrollLeft = left;
    window.scrollTo(...scroll);
  });
  render(); findings();
})();
