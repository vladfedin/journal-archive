'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['idle','complete','partial','cancelled','error','demo']);
const labels = {preparing:'Подготовка',verify:'Проверка',login:'Вход',posts:'Записи',comments:'Комментарии',reactions:'Реакции',videos:'Видео',materials:'Материалы',personal:'Личные данные',userpics:'Юзерпики',render:'Страницы',waiting:'Пауза',retrying:'Повтор запроса',complete:'Готов',partial:'Не завершён',cancelled:'Остановлено',error:'Ошибка',demo:'Пример'};
const localProgress = phase => ['preparing','verify','render'].includes(phase);
const scopes = {full:'весь журнал',period:'за период',urls:'по ссылкам',latest:'тест (последние 10 записей)'};
const el = (tag, cls, text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const day = value => {const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(value||'');return m?`${Number(m[3])} ${'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(' ')[Number(m[2])-1]} ${m[1]}`:'';};
const dateValue = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
function parameterTable(params={},known=Object.keys(params)){
  const table=el('table','report-table'),body=el('tbody');table.append(body);
  const value=(key,format=v=>String(v))=>known.includes(key)?format(params[key]):'Не сохранено';
  const yes=v=>v?'Да':'Нет';
  const rows=[['Объём',value('mode',v=>scopes[v]||v||'Не сохранено')]];
  if(params.mode==='period')rows.push(['С',value('date_from',v=>day(v)||'С начала журнала')],['По',value('date_to',v=>day(v)||'По последнюю запись')]);
  if(params.mode==='urls')rows.push(['Ссылки',value('urls',v=>Array.isArray(v)?v.join('\n'):v||'—')]);
  rows.push(['Изображения',value('images',yes)],['Видео',value('videos',yes)],['Профиль, друзья, настройки',value('account',yes)],['Переписка',value('messages',yes)],['Пауза',value('delay',v=>v+' сек.')],['Лимит запросов',value('max_requests',v=>v??'Без лимита')]);
  for(const [label,text] of rows){const row=el('tr');row.append(el('td','',label),el('td','',text));body.append(row);}
  return table;
}
function showParameters(archive){
  document.querySelectorAll('.more[open]').forEach(menu=>menu.open=false);
  $('settings-title').textContent='Параметры · '+(archive.username||'');
  $('settings-content').replaceChildren(parameterTable(archive.parameters,archive.parameters_known));
  $('settings-dialog').showModal();
}
let moving=false,libraryKey='',dismissedJob=null,busy=false,closed=false,submitting=false,preparingResume=false,planToken=0,pollTimer,mode='new',scope='full',selected=null,limitEdited=false,lastState={},archives=[];
const resumeLabel=archive=>archive.status==='cancelled'?'Продолжить':'Докачать пропуски';
function recoveryView(data,attempt=false){
  const box=el('div','recovery-view');
  if(!data?.available){box.append(el('p','fine',data?.message||'Список пропусков пока недоступен.'));return box;}
  const rows=data.rows||[];
  if(rows.length){
    const table=el('table','recovery-table'),head=el('thead'),hr=el('tr');
    for(const title of attempt?['','Было','Закрыто','Осталось']:['Что не получено','Осталось'])hr.append(el('th','',title));
    head.append(hr);table.append(head);const body=el('tbody');
    for(const row of rows){const tr=el('tr');tr.dataset.recoveryKind=row.key;tr.append(el('th','',row.label));for(const value of attempt?[row.before??'—',row.resolved??'—',row.remaining]:[row.remaining])tr.append(el('td','mono',String(value)));body.append(tr);if(attempt&&row.new){const note=el('tr'),td=el('td','fine',`Обнаружено дополнительно: ${row.new}`);td.colSpan=4;note.append(td);body.append(note);}}
    table.append(body);box.append(table);
  }else box.append(el('p','fine','Известных пропусков не осталось.'));
  if(!data.discovery_complete)box.append(el('p','fine','Обход записей ещё не завершён. Список может пополниться.'));
  if(data.reasons?.length){const details=el('details','recovery-reasons'),list=el('ul');details.append(el('summary','','Причины оставшихся пропусков'));for(const reason of data.reasons)list.append(el('li','',`${reason.message} · ${reason.count}`));details.append(list);box.append(details);}
  return box;
}
function attemptHistory(attempts){
  const details=el('details','recovery-history');details.append(el('summary','','Предыдущие попытки'));
  for(const attempt of [...attempts].reverse()){
    const row=el('div','recovery-attempt');row.append(el('strong','',new Date(attempt.finished_at).toLocaleString('ru-RU')+' · '+(labels[attempt.status]||'Остановлено')));
    const closed=(attempt.recovery?.rows||[]).filter(r=>r.resolved).map(r=>`${r.label.toLowerCase()}: ${r.resolved}`).join(' · ');
    row.append(el('p','fine',attempt.operation==='resume'?(closed?'Закрыто: '+closed:'Новых закрытых пропусков не отмечено.'):`${attempt.entries||0} записей · ${attempt.comments||0} комментариев`));
    if(attempt.message)row.append(el('p','fine',attempt.message));details.append(row);
  }return details;
}
async function api(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});const value=await r.json();if(!r.ok)throw new Error(value.error||'Не удалось выполнить запрос.');return value;}
function error(message,form=false){const target=$(form?'form-error':'app-error');target.textContent=message;target.hidden=!message;}
function setBusy(value){busy=value;document.querySelectorAll('[data-new],[data-update],[data-resume]').forEach(b=>b.disabled=value||moving||closed);$('submit-export').disabled=value||moving||submitting||preparingResume||closed;$('move-storage').disabled=value||moving||closed;$('move-storage').title=value?'После завершения загрузки':'';}
function setScope(value){scope=value;document.querySelectorAll('[data-scope]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scope===value)));$('period-fields').hidden=value!=='period';$('url-fields').hidden=value!=='urls';$('entry-urls').required=value==='urls';$('entry-urls').disabled=value!=='urls';if(!limitEdited)$('request-limit').value=value==='latest'?'50':'';$('request-limit').required=value==='latest';$('account').checked=value==='full';}
function openDialog(kind,archive=null){
  if(busy||moving||closed)return;mode=kind;selected=archive;error('',true);const resume=kind==='resume',isNew=kind==='new';
  document.querySelectorAll('.more[open]').forEach(menu=>menu.open=false);
  $('export-form').reset();limitEdited=false;
  $('dialog-title').textContent=resume?resumeLabel(archive):isNew?'Новый архив':'Обновить архив';
  $('submit-export').textContent=resume?resumeLabel(archive):isNew?'Создать':'Обновить';
  const token=++planToken;preparingResume=resume;$('resume-plan').hidden=!resume;$('resume-plan').replaceChildren();
  if(resume){
    $('resume-plan').append(el('p','fine','Проверяем сохранённый прогресс…'));
    api('/api/recovery/'+encodeURIComponent(archive.slug)).then(data=>{if(token!==planToken)return;$('resume-plan').replaceChildren(recoveryView(data));if(data.attempts?.length)$('resume-plan').append(attemptHistory(data.attempts));}).catch(()=>{if(token===planToken)$('resume-plan').replaceChildren(el('p','fine','Список пропусков недоступен. Можно продолжить с сохранённого этапа.'));}).finally(()=>{if(token===planToken){preparingResume=false;setBusy(busy);}});
  }
  setBusy(busy);
  $('username').value=isNew?'':archive.username;$('username').readOnly=!isNew;$('username-field').hidden=resume;
  $('credentials').classList.toggle('is-resume',resume);$('password').type='password';$('show-password').setAttribute('aria-label','Показать пароль');
  $('scope-fields').hidden=resume;$('resume-note').hidden=!resume;
  $('resume-settings').hidden=!resume;$('resume-settings').open=false;
  $('resume-parameters').replaceChildren(...(resume?[parameterTable(archive.parameters,archive.parameters_known)]:[]));
  document.querySelector('[data-scope=latest]').hidden=!isNew;document.querySelector('[data-scope=urls]').hidden=isNew;
  const to=new Date(),from=new Date(to);from.setDate(from.getDate()-29);$('date-from').value=dateValue(from);$('date-to').value=dateValue(to);
  if(!resume){setScope(isNew?'full':'period');if(archive?.parameters){$('images').checked=!!archive.parameters.images;$('videos').checked=!!archive.parameters.videos;}}
  else{$('request-limit').required=false;$('entry-urls').required=false;$('entry-urls').disabled=true;$('resume-note').textContent=archive.username+' · '+(scopes[archive.parameters?.mode]||'загрузка');}
  $('export-dialog').showModal();(isNew?$('username'):$('password')).focus();
}
function showStatus(s){lastState=s;if(!s.phase||s.phase==='idle'||s.job_id&&s.job_id===dismissedJob)return;$('job').hidden=false;$('job-title').textContent=s.username||'';
  let phase=s.request_limit_reached?'Лимит запросов':labels[s.phase]||'Загрузка';
  if(s.phase==='waiting'&&s.retry_at)phase=s.retry_at*1000>Date.now()?'Пауза до '+new Date(s.retry_at*1000).toLocaleTimeString('ru-RU'):'Повтор запроса';
  $('job-stage').textContent=s.cancelling?'Остановка…':phase;
  $('job-stage').classList.toggle('paused-message',['error','partial'].includes(s.phase));
  $('posts-count').textContent=mapSlug===s.slug&&mapView?.data?.expected?`${mapView.data.known} / ${mapView.data.expected}`:s.entries||0;$('comments-count').textContent=s.comments||0;
  $('requests-count').textContent=`${s.requests_used||0}${s.request_limit?' / '+s.request_limit:''} запросов`;
  $('progress').hidden=terminal.has(s.phase)||(!$('job-timeline').hidden&&!localProgress(s.phase));if(s.total){$('progress').max=s.total;$('progress').value=s.completed||0;}else $('progress').removeAttribute('value');
  $('open-result').hidden=!s.archive_url;if(s.archive_url)$('open-result').href=s.archive_url;
  $('job-statistics').hidden=!s.archive_url;if(s.archive_url)$('job-statistics').href=s.archive_url.replace(/index\.html$/,'statistics.html');
  $('job-report').hidden=!s.archive_url;if(s.archive_url)$('job-report').href=s.archive_url.replace(/index\.html$/,'report.html');
  $('job-settings').hidden=!s.parameters;
  $('job-message').textContent=s.message||'';$('job-message').hidden=!s.message||['complete','demo'].includes(s.phase)||!!s.request_limit_reached;
  $('job-recovery').hidden=!s.recovery;
  const recoveryKey=JSON.stringify([s.job_id,s.operation,s.recovery]);if($('job-recovery').dataset.key!==recoveryKey){const reasonsOpen=$('job-recovery-content').querySelector('.recovery-reasons')?.open;$('job-recovery').dataset.key=recoveryKey;$('job-recovery-content').replaceChildren(recoveryView(s.recovery,s.operation==='resume'));const reasons=$('job-recovery-content').querySelector('.recovery-reasons');if(reasons)reasons.open=!!reasonsOpen;}
  $('job-recovery-title').textContent=terminal.has(s.phase)?'Результат запуска':'Докачка пропусков';
  const entry=mapSlug===s.slug&&s.entry_id&&mapView?.items.find(i=>i.id===s.entry_id);$('job-entry').hidden=!entry||terminal.has(s.phase);if(entry){$('job-entry').href='/archive/'+encodeURIComponent(s.slug)+'/'+entry.url;$('job-entry').textContent=entry.title;}
  $('job-action').disabled=!!s.cancelling;$('job-action').textContent=!terminal.has(s.phase)?'Остановить':['complete','demo'].includes(s.phase)?'Закрыть':s.phase==='cancelled'?'Продолжить':'Докачать пропуски';
  setBusy(!terminal.has(s.phase));
}
async function library(){const data=await api('/api/archives');storageStatus(data.relocation);const key=JSON.stringify(data);if(key===libraryKey){setBusy(busy);return;}libraryKey=key;
  const open=new Set(Array.from(document.querySelectorAll('#archives details[open]'),menu=>menu.closest('tr').dataset.archive));
  const focused=document.activeElement,focusSlug=focused.closest('#archives tr')?.dataset.archive,focusAction=focused.dataset.action;
  archives=data.archives.filter(a=>a.slug!=='_demo');$('output-path').textContent=data.output;$('archives').replaceChildren();$('empty').hidden=!!archives.length;$('library').hidden=!archives.length;
  for(const archive of archives){const row=el('tr'),name=el('td'),link=el(archive.url?'a':'strong','journal-name',archive.username);row.dataset.archive=archive.slug;if(archive.url)link.href=archive.url;name.append(link);
    const mode=archive.parameters?.mode;const description=archive.entry_limit?`Тест · ${archive.entry_limit} записей`:mode==='period'?[day(archive.parameters.date_from),day(archive.parameters.date_to)].filter(Boolean).join(' — '):'';
    if(description)name.append(el('span','journal-scope',description));row.append(name,el('td','mono',String(archive.entries_saved||0)),el('td','mono',String(archive.comments_saved||0)),el('td','mono',day(archive.created_at)));
    const state=el('td'),status=el(archive.report_url?'a':'span','status'+(archive.status==='complete'?'':' partial'),labels[archive.status]||'Не завершён');if(archive.report_url)status.href=archive.report_url;state.append(status);row.append(state);
    const cell=el('td'),actions=el('div','row-actions');if(archive.url){const open=el('a','btn','Открыть');open.href=archive.url;actions.append(open);}
    const more=el('details','more'),summary=el('summary','','⋯');summary.setAttribute('aria-label','Действия с архивом '+archive.username);const menu=el('div','menu');
    const settings=el('button','','Параметры');settings.type='button';settings.onclick=()=>showParameters(archive);menu.append(settings);
    if(archive.resumable){const resume=el('button','',resumeLabel(archive));resume.type='button';resume.dataset.resume='';resume.onclick=()=>openDialog('resume',archive);menu.append(resume);}
    const update=el('button','','Обновить');update.type='button';update.dataset.update='';update.onclick=()=>openDialog('update',archive);menu.append(update);
    if(archive.url){menu.append(el('hr'));for(const [text,url] of [['Скачать JSON',archive.activitystreams_url],['Исходный JSON',archive.json_url],['Скачать ZIP',archive.zip_url],['Статистика',archive.url.replace('index.html','statistics.html')],['Календарь',archive.url.replace('index.html','calendar.html')],['Хронология',archive.url.replace('index.html','timeline.html')],['Профиль',archive.url.replace('index.html','account.html')],['Переписка',archive.url.replace('index.html','messages.html')],['Состояние архива',archive.report_url]]){const a=el('a','',text);a.href=url;menu.append(a);}}
    more.open=open.has(archive.slug);more.append(summary,menu);actions.append(more);cell.append(actions);row.append(cell);
    row.querySelectorAll('a,button,summary').forEach(action=>action.dataset.action=action.textContent);
    $('archives').append(row);
    if(archive.slug===focusSlug)Array.from(row.querySelectorAll('[data-action]')).find(action=>action.dataset.action===focusAction)?.focus({preventScroll:true});
  }setBusy(busy);
}
let mapView=null,mapSlug='',mapTime=0,mapPhase='';
async function refreshMap(state){
  if(!state.slug||state.slug==='_demo')return;
  if(mapSlug!==state.slug){mapView?.destroy();mapSlug=state.slug;mapTime=0;mapView=new window.ArchiveTimeline($('job-timeline'),'/archive/'+encodeURIComponent(state.slug)+'/');}
  if(Date.now()-mapTime<8000&&mapPhase===state.phase){mapView.active=terminal.has(state.phase)?null:state.entry_id;mapView.draw();return;}
  mapTime=Date.now();mapPhase=state.phase;
  const data=await api('/api/timeline/'+encodeURIComponent(state.slug));
  $('job-timeline').hidden=!data.expected;mapView.set(data,data.active);
  if(data.expected){$('progress').hidden=terminal.has(state.phase)||!localProgress(state.phase);$('posts-count').textContent=`${data.known} / ${data.expected}`;}
}
let previousKey='';async function poll(){if(closed)return;try{const state=await api('/api/status');showStatus(state);await refreshMap(state);error('');const key=JSON.stringify([state.phase,state.slug,state.archive_url]);if(key!==previousKey||!busy){await library();previousKey=key;}}catch(e){error('Приложение недоступно. Запустите его снова.');}finally{if(!closed)pollTimer=setTimeout(poll,busy?1200:5000);}}
document.querySelectorAll('[data-new]').forEach(b=>b.onclick=()=>openDialog('new'));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$('export-dialog').close());
document.querySelectorAll('[data-scope]').forEach(b=>b.onclick=()=>setScope(b.dataset.scope));
$('export-dialog').addEventListener('close',()=>{$('password').value='';planToken++;preparingResume=false;setBusy(busy);});
$('request-limit').oninput=()=>limitEdited=true;
$('show-password').onclick=()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('show-password').setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');};
$('export-form').onsubmit=async e=>{e.preventDefault();if(busy||submitting||preparingResume||closed)return;error('',true);let data;
  if(mode==='resume')data={resume:selected.slug,password:$('password').value};
  else data={username:$('username').value,password:$('password').value,entry_limit:scope==='latest'?10:null,mode:scope==='latest'?null:scope,images:$('images').checked,videos:$('videos').checked,account:$('account').checked,messages:$('messages').checked,delay:Number($('delay').value),max_requests:$('request-limit').value?Number($('request-limit').value):null,date_from:scope==='period'?$('date-from').value||null:null,date_to:scope==='period'?$('date-to').value||null:null,urls:scope==='urls'?$('entry-urls').value:[],archive:selected?.slug||null};
  submitting=true;$('submit-export').disabled=true;
  try{const state=await api('/api/export',data);$('export-dialog').close();showStatus(state);await library();}
  catch(e){error(e.message,true);$('password').value='';$('password').focus();}
  finally{data.password='';submitting=false;$('submit-export').disabled=busy;}
};
$('job-action').onclick=async()=>{try{if(busy){$('job-action').disabled=true;showStatus(await api('/api/cancel',{}));}else if(['complete','demo'].includes(lastState.phase)){$('job').hidden=true;dismissedJob=lastState.job_id;}else{await library();const archive=archives.find(a=>a.slug===lastState.slug);if(archive)openDialog('resume',archive);else error('Сохранённая задача не найдена.');}}catch(e){error(e.message);$('job-action').disabled=false;}};
$('job-settings').onclick=()=>showParameters(lastState);
$('settings-close').onclick=()=>$('settings-dialog').close();
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
