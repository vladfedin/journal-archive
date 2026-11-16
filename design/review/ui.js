'use strict';
const $ = id => document.getElementById(id);
const page = document.body.dataset.page;
const params = new URLSearchParams(location.search);
const paths = {
  archive:'<path d="M4 7h16v13H4zM3 3h18v4H3zM9 11h6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  x:'<path d="m6 6 12 12M6 18 18 6"/>',
  moon:'<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  folder:'<path d="M3 6h7l2 2h9v11H3zM3 6V4h6l2 2"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  comment:'<path d="M4 4h16v12H9l-5 4z"/>',
  heart:'<path d="M12 20S3 15 3 8.5A4.5 4.5 0 0 1 12 7a4.5 4.5 0 0 1 9 1.5C21 15 12 20 12 20Z"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  pen:'<path d="m14 5 5 5M4 20l5-1L21 7l-5-5L4 14z"/>'
};
const svg = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.archive}</svg>`;
function icons() { document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML=svg(el.dataset.icon); }); }
const isReader = ['archive','entry','status'].includes(page);
$('review').innerHTML = `<div class="review-bar"><div class="review-inner"><span>Макет · Без подключения к ЖЖ</span><a href="review.html">Сценарии</a><a href="index.html">Архивы</a><a href="index.html?new=1">Создание</a><a href="index.html?job=waiting">Пауза</a><a href="entry.html">Запись</a><a href="help.html">Помощь</a></div></div>`;
$('header').innerHTML = `<div class="header-inner"><a class="brand" href="${isReader?'archive.html':'index.html'}"><span class="mark">${svg('archive')}</span>${isReader?'fieldnotes':'Архив ЖЖ'}</a><nav class="nav" aria-label="Разделы">${isReader?'<a href="archive.html">Записи</a>':'<a href="index.html" '+(page==='library'?'aria-current="page"':'')+'>Архивы</a>'}<a href="help.html" ${page==='help'?'aria-current="page"':''}>Помощь</a><button class="icon" id="theme" aria-label="Тёмная тема" title="Тёмная тема">${svg('moon')}</button>${isReader?'<details class="more"><summary aria-label="Действия с архивом">⋯</summary><div class="menu"><button data-json>Скачать JSON</button><a href="status.html?complete=1">Состояние архива</a></div></details>':''}</nav></div>`;
$('footer').innerHTML = isReader?'<span>fieldnotes</span><a href="status.html?complete=1">Состояние архива</a>':'<span>Архив ЖЖ · 0.3</span><a href="#" id="quit">Завершить приложение</a>';
function setTheme(dark) { document.documentElement.dataset.theme=dark?'dark':'light'; $('theme').innerHTML=svg(dark?'sun':'moon'); $('theme').setAttribute('aria-label',dark?'Светлая тема':'Тёмная тема'); $('theme').title=dark?'Светлая тема':'Тёмная тема'; }
let chosen; try { chosen=localStorage.getItem('journal-review-theme'); } catch (_) {}
setTheme(chosen ? chosen==='dark' : matchMedia('(prefers-color-scheme: dark)').matches);
$('theme').onclick=()=>{const dark=document.documentElement.dataset.theme!=='dark';setTheme(dark);try{localStorage.setItem('journal-review-theme',dark?'dark':'light');}catch(_) {}};
let toastTimer;
function toast(text){if(!$('toast'))return;clearTimeout(toastTimer);$('toast').textContent=text;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,4500);}
const sampleData={schema_version:1,username:'fieldnotes',entries:[{itemid:1,subject:'Несколько заметок на полях',event:'<p>Перечитал старую тетрадь.</p>',eventtime:'2026-11-14 18:40:00'}],comments:[{id:1,jitemid:1,parentid:0,user:'maria',body:'Иногда комментарии интереснее самой записи.'}],reactions:[{kind:'post',object_id:1,authors:['maria','alex','booknotes']}]};
document.querySelectorAll('[data-json]').forEach(btn=>btn.onclick=()=>{const blob=new Blob([JSON.stringify(sampleData,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='fieldnotes-example.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
document.querySelectorAll('[data-zip]').forEach(btn=>btn.onclick=()=>toast('В релизе эта команда скачает страницы и данные одним ZIP-файлом.'));
document.querySelectorAll('[data-person],[data-original]').forEach(a=>a.onclick=e=>{e.preventDefault();toast('В рабочем архиве ссылка ведёт в ЖЖ. Здесь показаны вымышленные данные.');});
if($('quit'))$('quit').onclick=e=>{e.preventDefault();toast('Это макет. В приложении команда сохранит прогресс и завершит работу.');};
document.addEventListener('click',event=>{document.querySelectorAll('.more[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});});
if(page==='library'){
  const dialog=$('export-dialog');let mode='new',scope='full',jobState='running',stepTimer;
  let limitEdited=false,taskLimit=null,taskScope='full';
  const scopeNames={full:'весь журнал',period:'за период',latest:'тест (последние 10 записей)',urls:'по ссылкам'};
  function setScope(value){
    scope=value;
    document.querySelectorAll('[data-scope]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.scope===value)));
    $('period-fields').hidden=value!=='period';$('url-fields').hidden=value!=='urls';
    if(!limitEdited)$('request-limit').value=value==='latest'?'50':'';
    $('request-limit').required=value==='latest';
  }
  function openDialog(kind){mode=kind;document.querySelectorAll('.more[open]').forEach(el=>el.open=false);const resume=kind==='resume',isNew=kind==='new';$('dialog-title').textContent=resume?'Продолжить загрузку':isNew?'Новый архив':'Обновить архив';$('submit-export').textContent=resume?'Продолжить':isNew?'Создать':'Обновить';$('username').value=isNew?'':'fieldnotes';$('username').readOnly=!isNew;$('password').value='';$('password').type='password';$('username-field').hidden=resume;$('credentials').style.gridTemplateColumns=resume?'1fr':'';$('scope-fields').hidden=resume;$('resume-note').hidden=!resume;document.querySelector('[data-scope=latest]').hidden=!isNew;document.querySelector('[data-scope=urls]').hidden=isNew;if(!resume){limitEdited=false;setScope(isNew?'full':'period');}else{$('resume-note').textContent='fieldnotes · '+scopeNames[taskScope];$('request-limit').required=false;}dialog.showModal();}
  document.querySelectorAll('[data-new]').forEach(b=>b.onclick=()=>openDialog('new'));
  document.querySelectorAll('[data-update]').forEach(b=>b.onclick=()=>openDialog('update'));
  document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>dialog.close());
  document.querySelectorAll('[data-scope]').forEach(b=>b.onclick=()=>setScope(b.dataset.scope));
  dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
  $('show-password').onclick=()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('show-password').setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');};
  $('request-limit').oninput=()=>limitEdited=true;
  function job(state){
    jobState=state;$('job').hidden=false;$('progress').hidden=!['running','waiting'].includes(state);
    $('job-stage').classList.toggle('paused-message',['limit','error'].includes(state));
    $('job-stage').textContent={running:'Комментарии',limit:`Лимит запросов · ${taskLimit} / ${taskLimit}`,waiting:'Пауза до 14:35',stopped:'Остановлено',error:'Вход не выполнен',done:'Готово'}[state];
    $('job-action').textContent={running:'Остановить',limit:'Продолжить',waiting:'Остановить',stopped:'Продолжить',error:'Повторить',done:'Закрыть'}[state];
    const used=state==='limit'?taskLimit:state==='done'?(taskScope==='full'?128:43):32;
    $('requests-count').textContent=taskLimit?`${Math.min(used,taskLimit)} / ${taskLimit} запросов`:`${used} запросов`;
    if(state==='done'){$('posts-count').textContent=taskScope==='full'?'268':'10';$('comments-count').textContent=taskScope==='full'?'1 342':'141';}
    $('row-status').classList.toggle('partial',state!=='done');$('row-status').textContent=state==='done'?'Готов':state==='running'?'Загрузка':state==='waiting'?'Пауза':'Не завершён';
    const report=state==='done'?'status.html?complete=1':state==='limit'?`status.html?limit=${taskLimit}`:'status.html';
    $('row-status').href=report;document.querySelector('.job-top>a').href=report;
  }
  $('job-action').onclick=()=>{clearTimeout(stepTimer);if(['running','waiting'].includes(jobState))job('stopped');else if(jobState==='done')$('job').hidden=true;else openDialog('resume');};
  $('export-form').onsubmit=e=>{e.preventDefault();if(mode!=='resume'){taskScope=scope;taskLimit=$('request-limit').value?Number($('request-limit').value):null;}$('password').value='';dialog.close();$('empty').hidden=true;$('library').hidden=false;job('running');$('comments-count').textContent=mode==='resume'?'127':'86';stepTimer=setTimeout(()=>job(mode==='resume'||!taskLimit?'done':'limit'),2200);};
  if(params.has('empty')){$('library').hidden=true;$('empty').hidden=false;}
  if(params.has('job')){if(params.get('job')==='limit'){taskLimit=50;taskScope='latest';}job(params.get('job'));}
  if(params.has('new'))openDialog('new');
  if(params.has('update'))openDialog('update');
  if(params.has('resume')){job('stopped');openDialog('resume');}
}
if(page==='archive'){
 const entries=[...document.querySelectorAll('.entry-row')];
 function filter(){const term=$('search').value.trim().toLowerCase();let count=0;for(const row of entries){const text=(row.textContent+' '+(row.dataset.search||'')).toLowerCase();row.hidden=!!(term&&!text.includes(term)||$('year').value&&row.dataset.year!==$('year').value||$('tag').value&&row.dataset.tag!==$('tag').value||$('access').value&&row.dataset.access!==$('access').value);if(!row.hidden)count++;}const active=term||$('year').value||$('tag').value||$('access').value;$('found').textContent=active?`Найдено: ${count}`:'268 записей';$('reset').hidden=!active;$('no-results').hidden=!!count;document.querySelector('.pager').hidden=!!active;}
 for(const id of ['search','year','tag','access'])$(id).addEventListener(id==='search'?'input':'change',filter);
 $('reset').onclick=()=>{for(const id of ['search','year','tag','access'])$(id).value='';filter();};
 $('sort').onchange=()=>{const order=$('sort').value==='old'?[...entries].reverse():entries;order.forEach(row=>$('entry-list').append(row));};
 $('next-page').onclick=e=>{e.preventDefault();toast('В макете показаны пять записей. В архиве здесь следующая страница.');};
 if(params.has('tag')){$('tag').value=params.get('tag');filter();}
 document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();$('search').focus();}});
}
if(page==='entry'){
 const titles={books:'Книги, к которым возвращаюсь',walk:'После прогулки',autumn:'Тот самый осенний день',first:'Первая запись'};
 if(titles[params.get('post')]){$('post-title').textContent=titles[params.get('post')];document.title=$('post-title').textContent+' · fieldnotes';}
 document.querySelectorAll('[data-collapse]').forEach(btn=>btn.onclick=()=>{const block=$(btn.dataset.collapse);block.hidden=!block.hidden;btn.setAttribute('aria-expanded',String(!block.hidden));btn.textContent=(block.hidden?'Развернуть':'Свернуть')+' · '+block.querySelectorAll('.comment').length;});
 $('all-threads').onclick=()=>{const hide=[...document.querySelectorAll('.thread')].some(t=>!t.hidden);document.querySelectorAll('[data-collapse]').forEach(btn=>{const block=$(btn.dataset.collapse);block.hidden=hide;btn.setAttribute('aria-expanded',String(!hide));btn.textContent=(hide?'Развернуть':'Свернуть')+' · '+block.querySelectorAll('.comment').length;});$('all-threads').textContent=hide?'Развернуть ветки':'Свернуть ветки';};
}
if(page==='status'&&params.has('complete')){$('report-note').hidden=true;$('reaction-coverage').textContent=$('author-coverage').textContent='151 / 151';$('status-continue').hidden=true;document.querySelector('.form-advanced').hidden=true;}
if(page==='status'&&params.has('limit')){const limit=Number(params.get('limit'));if(Number.isInteger(limit)&&limit>0&&limit<=10000){$('report-reason').textContent='Достигнут лимит запросов';$('report-requests').textContent=`${limit} / ${limit}`;$('status-continue').href='index.html?job=limit&resume=1';}}
icons();
