'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const root = $('fulltext');
  if (!root) return;
  const q = $('ft-query'), kind = $('ft-kind'), author = $('ft-author'), year = $('ft-year');
  const messages = $('ft-messages'), grouped = $('ft-group'), list = $('ft-results');
  const labels = {post:'Запись', comment:'Комментарий', profile:'Профиль', material:'Материал', message:'Личное сообщение'};
  const fold = s => s.toLowerCase().replace(/ё/g, 'е');
  const words = s => fold(s).match(/[\p{L}\p{N}]+/gu) || [];
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const parts = [], postings = new Map(), docs = [];
  let loading, page = 1, generation = 0, timer;
  function tokens(query) {
    const clauses = [];
    for (const [,phrase,word] of query.replace(/[«»]/g,'"').matchAll(/"([^"]*)"?|([^\s"]+)/g)) {
      const terms = words(phrase || word || '');
      if (phrase && terms.length) clauses.push(terms.join(' '));
      else clauses.push(...terms);
    }
    return clauses;
  }
  function loadPortable() {
    if (!loading) {
      window.searchPart = data => parts.push(data);
      loading = Promise.all(root.dataset.parts.split(' ').map(part => new Promise((resolve,reject) => {
        const script = document.createElement('script');
        script.src = 'assets/fulltext-' + part + '.js';
        script.onload = resolve;
        script.onerror = () => reject(new Error('Не удалось открыть поисковые файлы архива.'));
        document.head.append(script);
      }))).then(() => {
        for (const part of parts) {
          const offset = docs.length;
          for (const d of part.docs) docs.push(d);
          for (const [word,ids] of Object.entries(part.postings)) {
            if (!postings.has(word)) postings.set(word, new Set());
            const set = postings.get(word);
            for (const id of ids) set.add(offset + id);
          }
        }
        parts.length = 0;
      });
    }
    return loading;
  }
  async function portable(clauses, p) {
    await loadPortable();
    const sets = clauses.flatMap(t => t.split(' ')).map(t => postings.get(t)).sort((a,b)=>(a?.size || 0)-(b?.size || 0));
    const hits = [];
    if (sets.every(Boolean)) for (const id of sets[0]) {
      if (!sets.every(set => set.has(id))) continue;
      const d = docs[id];
      if ((p.kind && p.kind !== d.kind) || (p.messages === '0' && d.kind === 'message') ||
          (p.year && !d.date.startsWith(p.year)) || (p.author && fold(d.author) !== fold(p.author))) continue;
      const fields = [d.title,d.text,d.author,d.tags].map(v => ' '+words(v).join(' ')+' ');
      if (!clauses.every(t => fields.some(value => value.includes(' '+t+' ')))) continue;
      const rank = clauses.reduce((n,t) => n + [6,1,3,2].reduce((v,w,i)=> v+(fields[i].includes(' '+t+' ')?w:0),0),0);
      hits.push({d,rank});
    }
    hits.sort((a,b)=>b.rank-a.rank || b.d.date.localeCompare(a.d.date));
    const size = 40, start = (p.page-1)*size;
    return {total:hits.length, size, rows:hits.slice(start,start+size).map(hit=>hit.d)};
  }
  function marked(node, text, terms) {
    let end = 0;
    for (const m of text.matchAll(/[\p{L}\p{N}]+/gu)) {
      if (!terms.has(fold(m[0]))) continue;
      node.append(text.slice(end,m.index), el('mark',m[0]));
      end = m.index + m[0].length;
    }
    node.append(text.slice(end));
  }
  function row(d, terms) {
    const item = el('article', '', 'entry-row');
    const meta = [labels[d.kind], !root.dataset.archive && d.archive, d.author, (d.date || '').slice(0,10), d.access].filter(Boolean);
    item.append(el('div',meta.join(' · '),'entry-meta'));
    const heading = el('h2'), link = el('a');
    if (/^(entries\/\d+\.html(?:#comment-\d+)?|messages(?:-\d+)?\.html#message-\d+|account\.html(?:#[\w-]+)?)$/.test(d.href)) {
      link.href = (root.dataset.archive ? '' : '/archive/'+encodeURIComponent(d.archive)+'/') + d.href;
    }
    marked(link,d.title || d.context || labels[d.kind],terms);
    heading.append(link); item.append(heading);
    if (d.context && d.title && d.context !== d.title) item.append(el('div',d.context,'small muted'));
    const text = [d.text,d.tags].filter(Boolean).join(' · ');
    let first;
    for (const m of text.matchAll(/[\p{L}\p{N}]+/gu)) {
      if (terms.has(fold(m[0]))) { first=m; break; }
    }
    const start = Math.max(0,(first?.index || 0)-70), stop = Math.min(text.length,start+230);
    if (text) {
      const excerpt = el('p','','entry-excerpt');
      marked(excerpt,(start?'…':'')+text.slice(start,stop)+(stop<text.length?'…':''),terms);
      item.append(excerpt);
    }
    return item;
  }
  async function search() {
    const current = ++generation;
    const query = q.value.trim(), clauses = tokens(query);
    const p = {q:query, kind:kind.value, author:author.value.trim(), year:year.value.trim(),
      messages:messages.checked?'1':'0',page:String(page)};
    const url = new URL(location.href);
    for (const [key,v] of Object.entries({...p,group:grouped.checked?'1':''})) {
      if (!v || (key==='messages' || key==='page') && v==='1') url.searchParams.delete(key);
      else url.searchParams.set(key,v);
    }
    try { history.replaceState(null,'',url); } catch { /* Some file viewers restrict history changes. */ }
    list.replaceChildren();
    $('ft-warning').hidden = $('ft-pager').hidden = $('ft-group-label').hidden = true;
    $('ft-status').textContent = '';
    if (!clauses.length) return;
    $('ft-status').textContent = 'Ищем…';
    try {
      let data;
      if (root.dataset.api) {
        const params = new URLSearchParams({...p,archive:root.dataset.archive});
        const response = await fetch(root.dataset.api+'?'+params);
        if (!response.ok) throw new Error('Не удалось выполнить поиск.');
        data = await response.json();
      } else data = await portable(clauses,p);
      if (current !== generation) return;
      const pages = Math.max(1,Math.ceil(data.total/data.size));
      if (page>pages) { page=pages; return search(); }
      $('ft-status').textContent = data.total ? 'Найдено · '+data.total : 'Ничего не найдено';
      $('ft-group-label').hidden = !data.total;
      if (data.unavailable?.length) {
        $('ft-warning').textContent = 'Пропущены архивы: '+data.unavailable.join(', ');
        $('ft-warning').hidden = false;
      }
      const terms = new Set(clauses.flatMap(t=>t.split(' '))), groups = new Map();
      for (const d of data.rows) {
        if (grouped.checked && ['comment','post'].includes(d.kind)) {
          const key = (d.archive||'')+'/'+d.href.split('#')[0];
          if (!groups.has(key)) {
            const details = el('details');
            details.append(el('summary',d.kind==='post'?d.title:d.context));
            groups.set(key,details); list.append(details);
          }
          groups.get(key).append(row(d,terms));
        } else list.append(row(d,terms));
      }
      $('ft-pager').hidden = pages === 1;
      $('ft-page').textContent = page+' / '+pages;
      $('ft-prev').disabled = page===1; $('ft-next').disabled = page===pages;
    } catch (err) { if (current===generation) $('ft-status').textContent=err.message; }
  }
  const initial = new URLSearchParams(location.search);
  for (const [id,key] of [[q,'q'],[kind,'kind'],[author,'author'],[year,'year']]) id.value = initial.get(key)||'';
  grouped.checked=initial.get('group')==='1'; messages.checked=initial.get('messages')!=='0';
  page=Math.max(1,Math.floor(Number(initial.get('page')))||1);
  for (const input of [q,kind,author,year,messages,grouped]) input.addEventListener('input',()=>{
    if (input===kind && kind.value==='message') messages.checked=true;
    if (input===messages && !messages.checked && kind.value==='message') kind.value='';
    generation++; clearTimeout(timer); page=1; timer=setTimeout(search,160);
  });
  for (const [id,step] of [['ft-prev',-1],['ft-next',1]]) $(id).onclick=()=>{page+=step; search(); q.scrollIntoView({block:'start'});};
  search();
})();
