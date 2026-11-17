'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['idle','complete','partial','cancelled','error','demo']);
const labels = {login:'Вход',posts:'Записи',comments:'Комментарии',reactions:'Реакции',videos:'Видео',materials:'Материалы',personal:'Личные данные',userpics:'Юзерпики',render:'Страницы',waiting:'Пауза',complete:'Готов',partial:'Не завершён',cancelled:'Остановлено',error:'Ошибка',demo:'Пример'};
const scopes = {full:'весь журнал',period:'за период',urls:'по ссылкам',latest:'тест (последние 10 записей)'};
const el = (tag, cls, text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const day = value => {const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(value||'');return m?`${Number(m[3])} ${'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(' ')[Number(m[2])-1]} ${m[1]}`:'';};
const dateValue = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
let moving=false,libraryKey='',dismissedJob=null,busy=false,closed=false,submitting=false,pollTimer,mode='new',scope='full',selected=null,limitEdited=false,lastState={},archives=[];
async function api(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});const value=await r.json();if(!r.ok)throw new Error(value.error||'Не удалось выполнить запрос.');return value;}
function error(message,form=false){const target=$(form?'form-error':'app-error');target.textContent=message;target.hidden=!message;}
function setBusy(value){busy=value;document.querySelectorAll('[data-new],[data-update],[data-resume]').forEach(b=>b.disabled=value||moving||closed);$('submit-export').disabled=value||moving||submitting||closed;$('move-storage').disabled=value||moving||closed;$('move-storage').title=value?'После завершения загрузки':'';}
function setScope(value){scope=value;document.querySelectorAll('[data-scope]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scope===value)));$('period-fields').hidden=value!=='period';$('url-fields').hidden=value!=='urls';$('entry-urls').required=value==='urls';$('entry-urls').disabled=value!=='urls';if(!limitEdited)$('request-limit').value=value==='latest'?'50':'';$('request-limit').required=value==='latest';$('account').checked=value==='full';}
function openDialog(kind,archive=null){
  if(busy||moving||closed)return;mode=kind;selected=archive;error('',true);const resume=kind==='resume',isNew=kind==='new';
  document.querySelectorAll('.more[open]').forEach(menu=>menu.open=false);
  $('export-form').reset();limitEdited=false;
  $('dialog-title').textContent=resume?'Продолжить загрузку':isNew?'Новый архив':'Обновить архив';
  $('submit-export').textContent=resume?'Продолжить':isNew?'Создать':'Обновить';
  $('username').value=isNew?'':archive.username;$('username').readOnly=!isNew;$('username-field').hidden=resume;
  $('credentials').classList.toggle('is-resume',resume);$('password').type='password';$('show-password').setAttribute('aria-label','Показать пароль');
  $('scope-fields').hidden=resume;$('resume-note').hidden=!resume;
  document.querySelector('[data-scope=latest]').hidden=!isNew;document.querySelector('[data-scope=urls]').hidden=isNew;
  const to=new Date(),from=new Date(to);from.setDate(from.getDate()-29);$('date-from').value=dateValue(from);$('date-to').value=dateValue(to);
  if(!resume){setScope(isNew?'full':'period');if(archive?.parameters){$('images').checked=!!archive.parameters.images;$('videos').checked=!!archive.parameters.videos;}}
  else{$('request-limit').required=false;$('entry-urls').required=false;$('entry-urls').disabled=true;$('resume-note').textContent=archive.username+' · '+(scopes[archive.parameters?.mode]||'загрузка');}
  $('export-dialog').showModal();(isNew?$('username'):$('password')).focus();
}
function showStatus(s){lastState=s;if(!s.phase||s.phase==='idle'||s.job_id&&s.job_id===dismissedJob)return;$('job').hidden=false;$('job-title').textContent=s.username||'';
  let phase=s.request_limit_reached?'Лимит запросов':labels[s.phase]||'Загрузка';
  if(s.phase==='waiting'&&s.retry_at)phase=s.retry_at*1000>Date.now()?'Пауза до '+new Date(s.retry_at*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'Повтор запроса';
  $('job-stage').textContent=s.cancelling?'Остановка…':phase;
  $('job-stage').classList.toggle('paused-message',['error','partial'].includes(s.phase));
  $('posts-count').textContent=mapSlug===s.slug&&mapView?.data?.expected?`${mapView.data.known} / ${mapView.data.expected}`:s.entries||0;$('comments-count').textContent=s.comments||0;
  $('requests-count').textContent=`${s.requests_used||0}${s.request_limit?' / '+s.request_limit:''} запросов`;
  $('progress').hidden=terminal.has(s.phase)||!$('job-timeline').hidden;if(s.total){$('progress').max=s.total;$('progress').value=s.completed||0;}else $('progress').removeAttribute('value');
  $('open-result').hidden=!s.archive_url;if(s.archive_url)$('open-result').href=s.archive_url;
  $('job-statistics').hidden=!s.archive_url;if(s.archive_url)$('job-statistics').href=s.archive_url.replace(/index\.html$/,'statistics.html');
  $('job-report').hidden=!s.archive_url;if(s.archive_url)$('job-report').href=s.archive_url.replace(/index\.html$/,'report.html');
  $('job-message').textContent=s.message||'';$('job-message').hidden=!s.message||!['partial','error','cancelled'].includes(s.phase)||!!s.request_limit_reached;
  $('job-action').disabled=!!s.cancelling;$('job-action').textContent=!terminal.has(s.phase)?'Остановить':['complete','demo'].includes(s.phase)?'Закрыть':s.phase==='error'?'Повторить':'Продолжить';
  setBusy(!terminal.has(s.phase));
}
async function library(){const data=await api('/api/archives');storageStatus(data.relocation);const key=JSON.stringify(data);if(key===libraryKey){setBusy(busy);return;}libraryKey=key;archives=data.archives.filter(a=>a.slug!=='_demo');$('output-path').textContent=data.output;$('archives').replaceChildren();$('empty').hidden=!!archives.length;$('library').hidden=!archives.length;
  for(const archive of archives){const row=el('tr'),name=el('td'),link=el(archive.url?'a':'strong','journal-name',archive.username);if(archive.url)link.href=archive.url;name.append(link);
    const mode=archive.parameters?.mode;const description=archive.entry_limit?`Тест · ${archive.entry_limit} записей`:mode==='period'?[day(archive.parameters.date_from),day(archive.parameters.date_to)].filter(Boolean).join(' — '):'';
    if(description)name.append(el('span','journal-scope',description));row.append(name,el('td','mono',String(archive.entries_saved||0)),el('td','mono',String(archive.comments_saved||0)),el('td','mono',day(archive.created_at)));
    const state=el('td'),status=el(archive.report_url?'a':'span','status'+(archive.status==='complete'?'':' partial'),labels[archive.status]||'Не завершён');if(archive.report_url)status.href=archive.report_url;state.append(status);row.append(state);
    const cell=el('td'),actions=el('div','row-actions');if(archive.url){const open=el('a','btn','Открыть');open.href=archive.url;actions.append(open);}
    const more=el('details','more'),summary=el('summary','','⋯');summary.setAttribute('aria-label','Действия с архивом '+archive.username);const menu=el('div','menu');
    if(archive.resumable){const resume=el('button','','Продолжить');resume.type='button';resume.dataset.resume='';resume.onclick=()=>openDialog('resume',archive);menu.append(resume);}
    const update=el('button','','Обновить');update.type='button';update.dataset.update='';update.onclick=()=>openDialog('update',archive);menu.append(update);
    if(archive.url){menu.append(el('hr'));for(const [text,url] of [['Скачать JSON',archive.activitystreams_url],['Исходный JSON',archive.json_url],['Скачать ZIP',archive.zip_url],['Статистика',archive.url.replace('index.html','statistics.html')],['Календарь',archive.url.replace('index.html','calendar.html')],['Хронология',archive.url.replace('index.html','timeline.html')],['Профиль',archive.url.replace('index.html','account.html')],['Переписка',archive.url.replace('index.html','messages.html')],['Состояние архива',archive.report_url]]){const a=el('a','',text);a.href=url;menu.append(a);}}
    more.append(summary,menu);actions.append(more);cell.append(actions);row.append(cell);$('archives').append(row);
  }setBusy(busy);
}
let mapView=null,mapSlug='',mapTime=0,mapPhase='';
async function refreshMap(state){
  if(!state.slug||state.slug==='_demo')return;
  if(mapSlug!==state.slug){mapView?.resize.disconnect();mapSlug=state.slug;mapTime=0;mapView=new window.ArchiveTimeline($('job-timeline'),'/archive/'+encodeURIComponent(state.slug)+'/');}
  if(Date.now()-mapTime<8000&&mapPhase===state.phase){mapView.active=terminal.has(state.phase)?null:state.entry_id;mapView.draw();return;}
  mapTime=Date.now();mapPhase=state.phase;
  const data=await api('/api/timeline/'+encodeURIComponent(state.slug));
  $('job-timeline').hidden=!data.expected;mapView.set(data,data.active);
  if(data.expected){$('progress').hidden=true;$('posts-count').textContent=`${data.known} / ${data.expected}`;}
}
let previousKey='';async function poll(){if(closed)return;try{const state=await api('/api/status');showStatus(state);await refreshMap(state);error('');const key=JSON.stringify([state.phase,state.slug,state.archive_url]);if(key!==previousKey||!busy){await library();previousKey=key;}}catch(e){error('Приложение недоступно. Запустите его снова.');}finally{if(!closed)pollTimer=setTimeout(poll,busy?1200:5000);}}
document.querySelectorAll('[data-new]').forEach(b=>b.onclick=()=>openDialog('new'));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$('export-dialog').close());
document.querySelectorAll('[data-scope]').forEach(b=>b.onclick=()=>setScope(b.dataset.scope));
$('export-dialog').addEventListener('close',()=>$('password').value='');
$('request-limit').oninput=()=>limitEdited=true;
$('show-password').onclick=()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('show-password').setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');};
$('export-form').onsubmit=async e=>{e.preventDefault();if(busy||submitting||closed)return;error('',true);let data;
  if(mode==='resume')data={resume:selected.slug,password:$('password').value};
  else data={username:$('username').value,password:$('password').value,entry_limit:scope==='latest'?10:null,mode:scope==='latest'?null:scope,images:$('images').checked,videos:$('videos').checked,account:$('account').checked,messages:$('messages').checked,delay:Number($('delay').value),max_requests:$('request-limit').value?Number($('request-limit').value):null,date_from:scope==='period'?$('date-from').value||null:null,date_to:scope==='period'?$('date-to').value||null:null,urls:scope==='urls'?$('entry-urls').value:[],archive:selected?.slug||null};
  submitting=true;$('submit-export').disabled=true;
  try{const state=await api('/api/export',data);$('export-dialog').close();showStatus(state);await library();}
  catch(e){error(e.message,true);$('password').value='';$('password').focus();}
  finally{data.password='';submitting=false;$('submit-export').disabled=busy;}
};
$('job-action').onclick=async()=>{try{if(busy){$('job-action').disabled=true;showStatus(await api('/api/cancel',{}));}else if(['complete','demo'].includes(lastState.phase)){$('job').hidden=true;dismissedJob=lastState.job_id;}else{await library();const archive=archives.find(a=>a.slug===lastState.slug);if(archive)openDialog('resume',archive);else error('Сохранённая задача не найдена.');}}catch(e){error(e.message);$('job-action').disabled=false;}};
$('quit').onclick=async()=>{try{await api('/api/quit',{});closed=true;clearTimeout(pollTimer);setBusy(false);$('quit').disabled=true;$('job').hidden=true;error('Приложение закрыто.');}catch(e){error(e.message);}};
poll();

function storageStatus(state){
  if(!state)return;const wasMoving=moving;moving=state.phase==='copying';
  $('storage-submit').disabled=moving;$('storage-path').disabled=moving;
  $('storage-status').textContent=moving?`Перенос · ${state.completed||0} / ${state.total||0} файлов`:state.phase==='idle'?'':state.message||'';
  if(wasMoving&&state.phase==='complete'&&state.message==='Перенесено')$('storage-dialog').close();
  setBusy(busy);
}
$('move-storage').onclick=()=>{if(busy||moving||closed)return;$('storage-path').value=$('output-path').textContent;$('storage-status').textContent='';$('storage-dialog').showModal();$('storage-path').focus();$('storage-path').select();};
$('storage-close').onclick=()=>$('storage-dialog').close();
$('storage-form').onsubmit=async e=>{e.preventDefault();$('storage-submit').disabled=true;try{storageStatus(await api('/api/storage/move',{path:$('storage-path').value}));await library();}catch(err){$('storage-status').textContent=err.message;$('storage-submit').disabled=false;}};
