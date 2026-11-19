'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['idle','complete','partial','cancelled','error','demo']);
const contactStorage = 'ljarchive.contact-job';
let contactJob = null;
try { contactJob = JSON.parse(localStorage.getItem(contactStorage)); } catch {}

function saveContactJob(job) {
  contactJob = job;
  try { localStorage.setItem(contactStorage, JSON.stringify(job)); } catch {}
}

let composeJob, composeToken, composeStatus;
const answerButtons = document.querySelectorAll('#contact-question [data-answer]');
let contactRequested = new URLSearchParams(location.search).has('contact');

function updateContact(state) {
  const sameJob = composeJob === state.job_id;
  const ready = sameJob && state.contact_ready;
  const canSend = ready && composeStatus === 'editing';
  const asking = !$('contact-question').hidden;
  $('contact-send').disabled = asking || !canSend || !$('contact-body').value.trim();
  for (const button of answerButtons) button.disabled = !asking || !canSend;
  $('contact-dismiss').textContent = asking && composeStatus === 'editing' ? 'Отмена (без отправки)' : 'Закрыть';
  const sender = state.parameters?.username || '';
  $('contact-account').textContent = !sameJob ? 'Аккаунт изменился. Откройте форму заново.' :
    ready ? 'От ' + sender + ' → staerum' : terminal.has(state.phase) ? 'Для отправки войдите в ЖЖ снова.' : 'Ожидаем входа в ЖЖ' + (sender ? ' · ' + sender : '');
}

function openContact(state, stage) {
  const dialog = $('contact-dialog');
  composeJob = state.job_id;
  composeToken = crypto.randomUUID();
  composeStatus = 'editing';
  const asking = stage === 'started' && state.contact_question && state.contact_ready;
  $('contact-question').hidden = !asking;
  $('contact-editor').hidden = !!asking;
  $('contact-body').disabled = !!asking;
  $('contact-send').hidden = !!asking;
  $('contact-body').value = '';
  $('contact-body').readOnly = false;
  $('contact-status').textContent = '';
  $('contact-title').textContent = asking ? 'Вы играете в Шент?' :
    ({started:'Копирование началось', complete:'Архив готов'}[stage] || 'Написать автору');
  updateContact(state);
  if (!dialog.open) dialog.showModal();
  (asking ? $('contact-dismiss') : $('contact-body')).focus();
}

function showContact(state) {
  if ($('contact-dialog').open) { updateContact(state); return; }
  if (document.visibilityState !== 'visible' || document.querySelector('dialog[open]')) return;
  if (contactRequested && state.phase) {
    contactRequested = false;
    history.replaceState(null, '', location.pathname);
    openContact(state, 'manual');
    return;
  }
  if (!contactJob || !state.job_id || state.job_id !== contactJob.id) return;
  const ready = !state.request_limit_reached && (state.phase === 'complete' || (state.phase === 'partial' && state.finished_with_skips));
  if (terminal.has(state.phase) && !ready) {
    saveContactJob(null);
    return;
  }
  const stage = !contactJob.started ? 'started' : ready ? 'complete' : null;
  if (!stage || contactJob.complete) return;
  if (stage === 'started' && state.contact_question && !state.contact_ready && !terminal.has(state.phase)) return;
  openContact(state, stage);
  saveContactJob({...contactJob, [stage]: true});
}
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
  if(params.source_type==='community')rows.unshift(['Сообщество',params.journal],['Аккаунт для входа',params.username]);
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
const num=n=>Number(n||0).toLocaleString('ru-RU'),count=(n,forms)=>window.ArchiveTimeline.count(n,forms||['','','']);
const NOUNS={days:['день','дня','дней'],posts:['запись','записи','записей'],discussions:['обсуждение','обсуждения','обсуждений'],images:['изображение','изображения','изображений'],videos:['видео','видео','видео'],reactions:['счётчик реакций','счётчика реакций','счётчиков реакций'],authors:['список отреагировавших','списка отреагировавших','списков отреагировавших'],personal:['раздел профиля','раздела профиля','разделов профиля']};
const resolvedText=rows=>rows.filter(r=>r.resolved).map(r=>NOUNS[r.key]?count(r.resolved,NOUNS[r.key]):`${r.label.toLowerCase()}: ${num(r.resolved)}`).join(', ');
// One reason reads as a sentence; several are listed with their counts.
const reasonsText=(reasons,total)=>reasons.map(x=>reasons.length>1||x.count!==total?`${x.message} — ${num(x.count)}`:x.message).join(' · ');
function gapItem(row,value,why,progress){
  const li=el('li','gap'),head=el('div','gap-head');li.dataset.recoveryKind=row.key;
  head.append(el('span','gap-name',row.label),el('span','gap-count',num(value)));li.append(head);
  if(progress!=null){const bar=el('span','gap-bar'),fill=el('i');fill.style.width=Math.round(100*progress)+'%';bar.append(fill);li.append(bar);}
  if(why)li.append(el('p','gap-why',why));return li;
}
function gapsView(data,{attempt=false,running=false,plan=false,videos=true}={}){
  const box=el('div','gaps-view');
  if(!data?.available){box.append(el('p','gaps-note',data?.message||'Список пропусков пока недоступен.'));return box;}
  const rows=data.rows||[],open=rows.filter(r=>r.remaining&&(r.key!=='videos'||videos)),shut=rows.filter(r=>r.unavailable),got=attempt?resolvedText(rows):'';
  const group=(title,cls,items)=>{const section=el('section','gaps-group '+cls),list=el('ul','gap-list');section.append(el('h3','',title));list.append(...items);section.append(list);return section;};
  if(got)box.append(el('p','gaps-done',(running?'Уже докачано: ':'Докачано: ')+got+'.'));
  if(open.length)box.append(group(running?'Докачиваем':plan?'Будем докачивать':'Осталось докачать','is-open',open.map(r=>{
    const notes=[];if(attempt&&r.new)notes.push('найдено новых: '+num(r.new));
    const why=(r.reasons||[]).filter(x=>!x.final);if(why.length)notes.push(reasonsText(why,r.remaining));
    return gapItem(r,r.remaining,notes.join(' · '),attempt&&r.before?(r.before-Math.min(r.before,r.remaining))/r.before:null);})));
  const extra=rows.find(r=>r.key==='videos'&&r.remaining&&!videos);
  if(extra)box.append(group('Видео вне этой выгрузки','is-extra',[gapItem(extra,extra.remaining,reasonsText(extra.reasons.filter(r=>!r.final),extra.remaining))]));
  if(shut.length){
    const section=group('Автоповторы завершены','is-closed',shut.map(r=>gapItem(r,r.unavailable,reasonsText((r.reasons||[]).filter(x=>x.final),r.unavailable))));
    section.append(el('p','gaps-note',`Недоступные изображения остались ссылками на оригинал. Кнопка «Загрузить» или обновление записи по ссылке запустит новую проверку.`));
    box.append(section);
  }
  if(!open.length&&!shut.length&&!extra)box.append(el('p','gaps-done',running?'Пропусков пока нет.':'Пропусков нет: всё, что ЖЖ отдаёт, сохранено.'));
  if(!data.discovery_complete&&(running||plan))box.append(el('p','gaps-note','Записи получены ещё не все — список может пополниться.'));
  const pics=data.pictures||{};
  if(!running&&(pics.assumed||pics.unknown)){const parts=[];if(pics.assumed)parts.push(`у ваших записей и комментариев (${num(pics.assumed)}) показан основной`);if(pics.unknown)parts.push(`у чужих комментариев (${num(pics.unknown)}) — нейтральная заглушка`);box.append(el('p','gaps-note','ЖЖ не сообщил, какой юзерпик был выбран: '+parts.join(', ')+'. Это не пропуск.'));}
  return box;
}
function attemptHistory(attempts){
  const details=el('details','recovery-history');details.append(el('summary','','Предыдущие запуски'));
  for(const attempt of [...attempts].reverse()){
    const row=el('div','recovery-attempt');row.append(el('strong','',new Date(attempt.finished_at).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'})+' · '+(labels[attempt.status]||'Остановлено')));
    const got=resolvedText(attempt.recovery?.rows||[]);
    row.append(el('p','fine',attempt.operation==='resume'?(got?'Докачано: '+got+'.':'Ничего нового не докачано.'):`${count(attempt.entries,['запись','записи','записей'])} · ${count(attempt.comments,['комментарий','комментария','комментариев'])}`));
    details.append(row);
  }return details;
}
async function api(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});const value=await r.json();if(!r.ok)throw new Error(value.error||'Не удалось выполнить запрос.');return value;}
function error(message,form=false){const target=$(form?'form-error':'app-error');target.textContent=message;target.hidden=!message;}
function setBusy(value){busy=value;document.querySelectorAll('[data-new],[data-update],[data-resume]').forEach(b=>b.disabled=value||moving||closed);$('submit-export').disabled=value||moving||submitting||preparingResume||needsCommunity()||closed;$('move-storage').disabled=value||moving||closed;$('move-storage').title=value?'После завершения загрузки':'';}
function setScope(value){scope=value;document.querySelectorAll('[data-scope]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scope===value)));$('period-fields').hidden=value!=='period';$('url-fields').hidden=value!=='urls';$('entry-urls').required=value==='urls';$('entry-urls').disabled=value!=='urls';if(!limitEdited)$('request-limit').value=value==='latest'?'50':'';$('request-limit').required=value==='latest';$('account').checked=value==='full';setSource();}
let communityRequest=0;
const choosingCommunity=()=>mode==='new'&&$('source-type').value==='community';
const needsCommunity=()=>choosingCommunity()&&!$('journal').value;
function resetCommunities(){
  communityRequest++;
  $('journal').replaceChildren();
  $('journal').hidden=true;
  $('load-communities').hidden=false;
  $('community-status').hidden=true;
  setBusy(busy);
}
async function loadCommunities(){
  if(!choosingCommunity())return;
  for(const id of ['username','password'])if(!$(id).reportValidity())return;
  const token=++communityRequest,status=$('community-status'),button=$('load-communities');
  button.hidden=true;status.textContent='Загружаем сообщества…';status.hidden=false;
  try{
    const {communities}=await api('/api/communities',{username:$('username').value,password:$('password').value});
    if(token!==communityRequest)return;
    if(communities.length){
      const select=$('journal');
      select.replaceChildren(...communities.map(name=>new Option(name,name)));
      if(communities.length>1){select.insertBefore(new Option('Выберите сообщество',''),select.firstChild);select.value='';}
      select.hidden=false;status.hidden=true;
    }else{status.textContent='ЖЖ не вернул управляемых сообществ.';button.hidden=false;}
  }catch(e){if(token===communityRequest){status.textContent=e.message;button.hidden=false;}}
  finally{if(token===communityRequest)setBusy(busy);}
}
function setSource(){
  const community=$('source-type').value==='community';
  $('journal-field').hidden=!community;$('journal').required=choosingCommunity();$('journal').disabled=!choosingCommunity();
  for(const key of ['account','messages']){$(key).closest('label').hidden=community;$(key).disabled=community;if(community)$(key).checked=false;}
  setBusy(busy);
}
$('source-type').onchange=()=>{
  resetCommunities();setSource();
  if(choosingCommunity()&&$('username').validity.valid&&$('password').value)loadCommunities();
};
$('load-communities').onclick=loadCommunities;
$('journal').onchange=()=>setBusy(busy);
for(const id of ['username','password'])$(id).addEventListener('input',()=>{if(choosingCommunity())resetCommunities();});
function openDialog(kind,archive=null){
  if(busy||moving||closed)return;mode=kind;selected=archive;error('',true);const resume=kind==='resume',isNew=kind==='new';
  document.querySelectorAll('.more[open]').forEach(menu=>menu.open=false);
  $('export-form').reset();limitEdited=false;
  $('dialog-title').textContent=resume?resumeLabel(archive):isNew?'Новый архив':'Обновить архив';
  $('submit-export').textContent=resume?resumeLabel(archive):isNew?'Создать':'Обновить';
  const token=++planToken;preparingResume=resume;$('resume-plan').hidden=!resume;$('resume-plan').replaceChildren();
  if(resume){
    $('resume-plan').append(el('p','fine','Проверяем сохранённый прогресс…'));
    api('/api/recovery/'+encodeURIComponent(archive.slug)).then(data=>{if(token!==planToken)return;$('resume-plan').replaceChildren(gapsView(data,{plan:true,videos:archive.parameters?.videos!==false}));if(data.attempts?.length)$('resume-plan').append(attemptHistory(data.attempts));}).catch(()=>{if(token===planToken)$('resume-plan').replaceChildren(el('p','fine','Список пропусков недоступен. Можно продолжить с сохранённого этапа.'));}).finally(()=>{if(token===planToken){preparingResume=false;setBusy(busy);}});
  }
  setBusy(busy);
  $('source-type').value=archive?.parameters?.source_type||'own';$('source-type').disabled=!isNew;
  resetCommunities();
  if(!isNew){
    const name=archive.parameters?.journal||archive.username;
    $('journal').replaceChildren(new Option(name,name));$('journal').hidden=false;$('load-communities').hidden=true;
  }
  $('source-fields').hidden=resume;
  $('username').value=isNew?'':archive.parameters?.username||archive.username;$('username').readOnly=!isNew;$('username-field').hidden=resume;
  $('credentials').classList.toggle('is-resume',resume);$('password').type='password';$('show-password').setAttribute('aria-label','Показать пароль');
  $('scope-fields').hidden=resume;$('resume-note').hidden=!resume;
  $('resume-settings').hidden=!resume;$('resume-settings').open=false;
  $('resume-parameters').replaceChildren(...(resume?[parameterTable(archive.parameters,archive.parameters_known)]:[]));
  document.querySelector('[data-scope=latest]').hidden=!isNew;document.querySelector('[data-scope=urls]').hidden=isNew;
  const to=new Date(),from=new Date(to);from.setDate(from.getDate()-29);$('date-from').value=dateValue(from);$('date-to').value=dateValue(to);
  if(!resume){setScope(isNew?'full':'period');if(archive?.parameters){$('images').checked=!!archive.parameters.images;$('videos').checked=!!archive.parameters.videos;}}
  else{$('request-limit').required=false;$('entry-urls').required=false;$('entry-urls').disabled=true;$('resume-note').textContent=archive.username+' · '+(scopes[archive.parameters?.mode]||'загрузка');}
  setSource();$('export-dialog').showModal();(isNew?$('username'):$('password')).focus();
}
// Minutes for the long work; seconds only for a countdown the person is watching.
function minutes(seconds){const n=Math.max(0,Math.round((seconds||0)/60));return n<1?'меньше минуты':n<60?n+' мин':Math.floor(n/60)+' ч'+(n%60?' '+n%60+' мин':'');}
function countdown(seconds){const n=Math.max(0,Math.ceil(seconds||0));return n<60?n+' с':minutes(n);}
const doing={preparing:'Готовим загрузку',verify:'Проверяем сохранённые файлы',login:'Входим в ЖЖ',posts:'Получаем записи',comments:'Получаем комментарии',reactions:'Получаем реакции',materials:'Скачиваем изображения',userpics:'Скачиваем юзерпики',videos:'Скачиваем видео',personal:'Сохраняем профиль и переписку',render:'Собираем страницы архива',retrying:'Повторяем недополученное',loading:'Получаем данные'};
const finished={complete:'Архив готов',partial:'Загрузка не завершена',cancelled:'Остановлено',error:'Загрузка прервалась',demo:'Пример готов'};
const network=new Set(['login','posts','comments','reactions','materials','userpics','videos','personal','loading','retrying']);
function waitText(s,clock){
  const w=s.wait||{};
  const why=w.reason==='http'?(w.http_status===429?'ЖЖ просит подождать':'Сервер не отвечает'):w.reason==='api_limit'?'ЖЖ просит подождать':['timeout','connection','transfer','tls'].includes(w.reason)?'Нет связи с сервером':'Пауза перед повтором';
  const parts=[why];if(w.host)parts.push(w.host);if(w.attempt)parts.push(`попытка ${w.attempt} из ${w.max_attempts}`);
  if(s.retry_at>clock)parts.push('повтор через '+countdown(s.retry_at-clock));if(w.server_delay)parts.push('срок назначил сервер');
  return parts.join(' · ');
}
function nowContent(s,clock){
  if(s.phase==='waiting')return [waitText(s,clock)];
  if(s.request_limit_reached)return ['Полученное сохранено. Докачать можно новым запуском со своим лимитом.'];
  if(s.phase==='cancelled')return ['Полученное сохранено. Продолжить можно в любой момент.'];
  if(['complete','demo'].includes(s.phase)||(s.phase==='partial'&&s.finished_with_skips)||!s.message)return [];
  // «Комментарии к записи #232» → the entry's title, linked to its page.
  const entry=mapSlug===s.slug&&s.entry_id&&mapView?.items.find(i=>i.id===s.entry_id),mark='#'+s.entry_id;
  if(entry&&s.message.includes(mark)){const [before,...rest]=s.message.split(mark),link=el('a','',`«${entry.title}»`);link.href='/archive/'+encodeURIComponent(s.slug)+'/'+entry.url;return [before,link,rest.join(mark)];}
  return [s.message];
}
function showStatus(s){lastState=s;showContact(s);if(!s.phase||s.phase==='idle'||s.job_id&&s.job_id===dismissedJob)return;$('job').hidden=false;$('job-title').textContent=s.username||'';
  const material=s.material_task,materialRunning=material&&!['complete','error','needs_browser','cancelled'].includes(material.phase);
  const clock=s.server_time||Date.now()/1000,done=terminal.has(s.phase),stalled=!done&&s.worker_alive===false;
  const request=s.request||{},activity=s.last_activity||{},silent=!done&&network.has(s.phase)&&request.at&&request.at>(s.last_response_at||s.last_success_at||0)&&activity.kind==='request'&&clock-request.at>60;
  let stage=s.cancelling?'Останавливаем…':stalled?'Загрузка прервалась':s.request_limit_reached?'Достигнут лимит запросов':done?finished[s.phase]||labels[s.phase]:doing[s.phase]||'Загружаем';
  const finishedWithSkips=s.phase==='partial'&&s.finished_with_skips&&!s.request_limit_reached;
  if(finishedWithSkips)stage='Архив готов';
  if(s.phase==='browser_login'&&!s.cancelling)stage='Вход в браузере';
  if(s.phase==='waiting'&&!s.cancelling&&!stalled)stage=s.retry_at>clock?'Пауза до '+new Date(s.retry_at*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'Продолжаем после паузы';
  if(materialRunning)stage=material.phase==='waiting'?'Пауза перед запросом':material.phase==='saving'?'Обновляем страницы архива':'Скачиваем изображения';
  if(material?.phase==='complete' && !s.request_limit_reached && s.phase==='partial')stage='Докачка изображений завершена';
  // Only this sentence is announced; it changes with the stage, not every second.
  if($('job-stage').textContent!==stage)$('job-stage').textContent=stage;
  $('job-state').dataset.tone=stalled||s.phase==='error'?'error':s.phase==='waiting'||silent?'wait':(['complete','demo'].includes(s.phase)||finishedWithSkips)&&!s.request_limit_reached?'done':done?'warn':'run';
  $('job-kind').hidden=s.operation!=='resume';$('job-kind').textContent='докачка пропусков';
  const now=material?[material.message||'']:nowContent(s,clock);if(material?.total)now.push(` · ${material.completed||0} / ${material.total}`);if(silent&&!material)now.unshift(`Ожидаем ответ${request.host?' от '+request.host:''} · ${minutes(clock-request.at)}. `);
  else if(!done&&network.has(s.phase)&&activity.kind==='failure'&&activity.reason==='web_login')now.splice(0,now.length,'Не удалось подтвердить вход для изображений. Полученное сохраняется.');
  else if(!done&&network.has(s.phase)&&activity.kind==='redirect')now.unshift('ЖЖ перенаправляет запрос · '+(activity.http_status||302)+'. ');
  $('job-now').hidden=!now.length;$('job-message').replaceChildren(...now);$('job-now').classList.toggle('is-problem',['error','partial'].includes(s.phase)||stalled);
  const meta=[],data=mapSlug===s.slug&&mapView?.data;
  if(!data?.expected)meta.push(count(s.entries,['запись','записи','записей']));
  meta.push(count(s.comments,['комментарий','комментария','комментариев']));
  if(s.started_at&&s.working_seconds!=null){const pause=s.waiting_seconds||0;meta.push((done?'заняло ':'идёт ')+minutes((s.working_seconds||0)+pause)+(pause>=60?`, из них пауз ${minutes(pause)}`:''));}
  if(s.request_limit)meta.push(`запросов ${num(s.requests_used)} из ${num(s.request_limit)}`);
  const photos=material?.browser_images_progress||s.browser_images_progress;
  const photoSummary=photos?`Изображения · сохранено ${num(photos.saved)} · недоступно ${num(photos.unavailable)} · к повтору ${num(photos.pending)}`:'';
  $('job-photo-result').hidden=!photos||!done||materialRunning;
  $('job-photo-result').textContent=photoSummary;
  if(photos&&(!done||materialRunning)){
    meta.unshift(photoSummary);
    if((materialRunning&&material.phase!=='saving'||!done&&['running','waiting','materials','retrying','loading'].includes(s.phase))&&photos.current){
      const line=el('div','material-source',photos.current);
      $('job-message').append(line);$('job-now').hidden=false;
      if(photos.last){
        const previous=photos.last,label={saved:'Сохранено',unavailable:'Недоступно',pending:'К повтору'}[previous.outcome];
        $('job-message').append(el('div','material-source',`${label}${previous.http_status?' · HTTP '+previous.http_status:''} · ${previous.url}`));
      }
    }
  }
  $('job-meta').textContent=meta.join(' · ');
  $('progress').hidden=done||(!$('job-timeline').hidden&&!localProgress(s.phase));if(s.total){$('progress').max=s.total;$('progress').value=s.completed||0;}else $('progress').removeAttribute('value');
  const ready=['complete','demo'].includes(s.phase);
  $('open-result').hidden=!s.archive_url;if(s.archive_url)$('open-result').href=s.archive_url;$('open-result').className='btn'+(ready?' primary':'');
  $('job-statistics').hidden=!s.archive_url;if(s.archive_url)$('job-statistics').href=s.archive_url.replace(/index\.html$/,'statistics.html');
  $('job-report').hidden=!s.archive_url;if(s.archive_url)$('job-report').href=s.archive_url.replace(/index\.html$/,'report.html');
  $('job-settings').hidden=!s.parameters;
  $('job-diagnostics').hidden=!s.slug||s.slug==='_demo';
  const recoveryBox=$('job-recovery');
  if(recoveryBox.dataset.job!==s.job_id){recoveryBox.open=false;recoveryBox.dataset.job=s.job_id||'';}
  recoveryBox.hidden=!s.recovery&&!s.archive_url&&!s.parameters&&$('job-diagnostics').hidden&&s.phase!=='partial';
  $('job-recovery-content').hidden=!s.recovery;
  $('job-skips').hidden=!s.archive_url;
  if(s.archive_url)$('job-skips').href=s.archive_url.replace(/index\.html$/,'report.html#skipped');
  const recoveryKey=JSON.stringify([s.job_id,s.operation,done,s.recovery]);if($('job-recovery').dataset.key!==recoveryKey){$('job-recovery').dataset.key=recoveryKey;$('job-recovery-content').replaceChildren(gapsView(s.recovery,{attempt:s.operation==='resume',running:!done,videos:s.parameters?.videos!==false}));}
  let imageAction=$('job-images');if(!imageAction){imageAction=el('button','btn','Изображения: файлы и докачка');imageAction.type='button';imageAction.id='job-images';$('job-recovery').append(imageAction);}
  imageAction.hidden=!s.slug||!s.recovery?.rows?.some(r=>r.key==='images'&&(r.remaining||r.unavailable));imageAction.onclick=()=>window.ArchiveImages.open(s.slug);
  $('job-hint').hidden=done||!!s.cancelling;
  const actionSlot=$(s.phase==='partial'?'job-tech-actions':'job-run-actions');
  if($('job-action').parentNode!==actionSlot)actionSlot.append($('job-action'));
  $('job-action').disabled=!!s.cancelling;$('job-action').textContent=!done?'Остановить':ready?'Скрыть':s.phase==='cancelled'||s.phase==='error'?'Продолжить':'Докачать пропуски';
  $('job-action').className='btn'+(ready?' text':done?' primary':'');
  $('job-browser-cancel').hidden=s.phase!=='browser_login';$('job-browser-cancel').disabled=!!s.cancelling;
  if(s.phase==='browser_login'){$('job-action').textContent='Продолжить';$('job-action').className='btn primary';$('job-hint').hidden=true;$('progress').hidden=true;}
  if(materialRunning){$('job-action').disabled=true;$('job-state').dataset.tone='run';}
  setBusy(!done||materialRunning);
}
async function library(){const data=await api('/api/archives');storageStatus(data.relocation);const key=JSON.stringify(data);if(key===libraryKey){setBusy(busy);return;}libraryKey=key;
  const open=new Set(Array.from(document.querySelectorAll('#archives details[open]'),menu=>menu.closest('tr').dataset.archive));
  const focused=document.activeElement,focusSlug=focused.closest('#archives tr')?.dataset.archive,focusAction=focused.dataset.action;
  archives=data.archives.filter(a=>a.slug!=='_demo');$('output-path').textContent=data.output;$('archives').replaceChildren();$('empty').hidden=!!archives.length;$('library').hidden=!archives.length;
  for(const archive of archives){const row=el('tr'),name=el('td'),link=el(archive.url?'a':'strong','journal-name',archive.username);row.dataset.archive=archive.slug;if(archive.url)link.href=archive.url;name.append(link);
    const mode=archive.parameters?.mode;const description=archive.entry_limit?`Тест · ${archive.entry_limit} записей`:mode==='period'?[day(archive.parameters.date_from),day(archive.parameters.date_to)].filter(Boolean).join(' — '):'';
    if(description)name.append(el('span','journal-scope',description));row.append(name,el('td','mono',String(archive.entries_saved||0)),el('td','mono',String(archive.comments_saved||0)),el('td','mono',day(archive.created_at)));
    const state=el('td'),status=el(archive.report_url?'a':'span','status'+(archive.status==='complete'||archive.finished_with_skips?'':' partial'),archive.finished_with_skips?'Готов':labels[archive.status]||'Не завершён');if(archive.report_url)status.href=archive.report_url;state.append(status);row.append(state);
    const cell=el('td'),actions=el('div','row-actions');if(archive.url){
      const shortcuts=el('span','library-shortcuts');
      for(const [label,icon,href] of [['Открыть архив','open',archive.url],['Статистика','statistics',archive.url.replace('index.html','statistics.html')]]){
        const a=el('a','icon');a.href=href;a.title=label;a.setAttribute('aria-label',label);a.innerHTML=svg(icon);shortcuts.append(a);
      }
      link.after(shortcuts);
    }
    const more=el('details','more'),summary=el('summary','','⋯');summary.setAttribute('aria-label','Действия с архивом '+archive.username);const menu=el('div','menu');
    const settings=el('button','','Параметры');settings.type='button';settings.onclick=()=>showParameters(archive);menu.append(settings);
    const diagnostics=el('button','','Диагностика');diagnostics.type='button';diagnostics.onclick=()=>showDiagnostics(archive.slug);menu.append(diagnostics);
    const images=el('button','','Изображения');images.type='button';images.onclick=()=>{more.open=false;window.ArchiveImages.open(archive.slug);};menu.append(images);
    if(archive.resumable){const resume=el('button','',resumeLabel(archive));resume.type='button';resume.dataset.resume='';resume.onclick=()=>openDialog('resume',archive);menu.append(resume);}
    const update=el('button','','Обновить');update.type='button';update.dataset.update='';update.onclick=()=>openDialog('update',archive);menu.append(update);
    if(archive.url){menu.append(el('hr'));for(const [text,url] of [['Скачать JSON',archive.activitystreams_url],['Исходный JSON',archive.json_url],['Скачать ZIP',archive.zip_url],['Календарь',archive.url.replace('index.html','calendar.html')],['Хронология',archive.url.replace('index.html','timeline.html')],['Профиль',archive.url.replace('index.html','account.html')],['Переписка',archive.url.replace('index.html','messages.html')],['Состояние архива',archive.report_url]]){const a=el('a','',text);a.href=url;menu.append(a);}}
    more.open=open.has(archive.slug);more.append(summary,menu);actions.append(more);cell.append(actions);row.append(cell);
    row.querySelectorAll('a,button,summary').forEach(action=>action.dataset.action=action.textContent||action.getAttribute('aria-label'));
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
  const first=$('job-timeline').hidden&&data.expected;$('job-timeline').hidden=!data.expected;mapView.set(data,data.active);
  if(data.expected)$('progress').hidden=terminal.has(state.phase)||!localProgress(state.phase);
  if(first)showStatus(lastState);
}
function showUpdate(update) {
  if (!update || !$('app-update')) return;
  const {phase, version} = update;
  const messages = {
    checking:'Проверяем GitHub Releases…', current:'Установлена последняя версия.',
    available:'Можно загрузить версию ' + version + '.',
    downloading:'Загрузка ' + version + ' · ' + Math.floor(100 * (update.bytes || 0) / (update.total || 1)) + '%',
    ready:'Версия ' + version + ' готова. После завершения работы закройте приложение кнопкой внизу и запустите его снова.',
    error:update.message
  };
  $('update-summary').textContent = phase === 'available' ? 'Доступна версия ' + version :
    phase === 'ready' ? 'Обновление готово · ' + version : phase === 'downloading' ? messages.downloading : 'Обновления приложения';
  $('update-status').textContent = messages[phase] || '';
  if (typeof update.automatic === 'boolean') $('update-auto').checked = update.automatic;
  const action = $('update-action');
  action.disabled = ['checking','downloading','ready'].includes(phase);
  action.hidden = phase === 'ready';
  action.dataset.action = phase === 'available' ? 'install' : 'check';
  action.textContent = phase === 'available' ? 'Загрузить ' + version : 'Проверить';
}
if ($('app-update')) {
  $('update-action').onclick = async () => {
    $('update-action').disabled = true;
    try { showUpdate(await api('/api/update/' + $('update-action').dataset.action, {})); }
    catch (e) { $('update-status').textContent = e.message; $('update-action').disabled = false; }
  };
  $('update-auto').onchange = async () => {
    const box = $('update-auto');
    box.disabled = true;
    try { showUpdate(await api('/api/update/settings', {automatic:box.checked})); }
    catch (e) { box.checked = !box.checked; $('update-status').textContent = e.message; }
    finally { box.disabled = false; }
  };
}
let previousKey='';async function poll(){if(closed)return;let received=false;try{const state=await api('/api/status');received=true;showUpdate(state.app_update);showStatus(state);await refreshMap(state);error('');const key=JSON.stringify([state.phase,state.slug,state.archive_url]);if(key!==previousKey||!busy){await library();previousKey=key;}}catch(e){if(!received){$('job-stage').textContent='Нет связи';error('Нет связи с приложением. Показан последний полученный статус.');}else error(e.message);}finally{if(!closed)pollTimer=setTimeout(poll,busy?1200:5000);}}
document.querySelectorAll('[data-new]').forEach(b=>b.onclick=()=>openDialog('new'));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$('export-dialog').close());
document.querySelectorAll('[data-scope]').forEach(b=>b.onclick=()=>setScope(b.dataset.scope));
$('export-dialog').addEventListener('close',()=>{$('password').value='';planToken++;communityRequest++;preparingResume=false;setBusy(busy);});
window.addEventListener('storage', event => {
  if (event.key === contactStorage) {
    try { contactJob = JSON.parse(event.newValue); } catch {}
  }
});
document.addEventListener('visibilitychange', () => showContact(lastState));
document.addEventListener('close', () => showContact(lastState), true);
document.querySelectorAll('[data-contact-close]').forEach(button => button.onclick = () => $('contact-dialog').close());
document.querySelector('.library-footer a').addEventListener('click', event => {
  if (!lastState.contact_ready) return;
  event.preventDefault();
  openContact(lastState, 'manual');
});
$('contact-body').oninput = () => updateContact(lastState);
$('contact-form').onsubmit = event => {
  event.preventDefault();
  if (!$('contact-send').disabled) sendContact($('contact-body').value, false);
};
for (const button of answerButtons) {
  button.onclick = () => {
    if (!button.disabled) sendContact($('contact-title').textContent + ' — ' + button.textContent, true);
  };
}

async function sendContact(body, answering) {
  const token = composeToken;
  composeStatus = 'sending';
  $('contact-status').textContent = 'Отправляем…';
  $('contact-body').readOnly = true;
  updateContact(lastState);
  let result;
  try {
    result = await api('/api/contact', {job_id:composeJob, token, body});
  } catch (error) {
    result = {status:'unknown', message:error.message + ' Проверьте папку «Отправленные» в ЖЖ.'};
  }
  if (token !== composeToken || !$('contact-dialog').open) return;
  if (answering && result.status === 'sent' && composeJob === lastState.job_id) {
    openContact(lastState, 'manual');
    $('contact-status').textContent = 'Ответ отправлен.';
    return;
  }
  composeStatus = result.status;
  $('contact-status').textContent = result.status === 'sent' ? 'Сообщение отправлено.' : result.message;
  if (result.status === 'sent') $('contact-body').value = '';
  if (result.status === 'rejected') {
    composeToken = crypto.randomUUID();
    composeStatus = 'editing';
    $('contact-body').readOnly = false;
  }
  updateContact(lastState);
}
$('request-limit').oninput=()=>limitEdited=true;
$('show-password').onclick=()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('show-password').setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');};
$('export-form').onsubmit=async e=>{e.preventDefault();if(busy||submitting||preparingResume||needsCommunity()||closed)return;error('',true);let data;
  if(mode==='resume')data={resume:selected.slug,password:$('password').value};
  else data={username:$('username').value,journal:$('source-type').value==='community'?$('journal').value:$('username').value,source_type:$('source-type').value,password:$('password').value,entry_limit:scope==='latest'?10:null,mode:scope==='latest'?null:scope,images:$('images').checked,videos:$('videos').checked,account:$('account').checked,messages:$('messages').checked,delay:Number($('delay').value),max_requests:$('request-limit').value?Number($('request-limit').value):null,date_from:scope==='period'?$('date-from').value||null:null,date_to:scope==='period'?$('date-to').value||null:null,urls:scope==='urls'?$('entry-urls').value:[],archive:selected?.slug||null};
  submitting=true;$('submit-export').disabled=true;
  try{const state=await api('/api/export',data);$('export-dialog').close();
    saveContactJob({id:state.job_id});
    showStatus(state);await library();}
  catch(e){error(e.message,true);$('password').value='';if(choosingCommunity())resetCommunities();$('password').focus();}
  finally{data.password='';submitting=false;setBusy(busy);}
};
$('job-action').onclick=async()=>{try{if(lastState.phase==='browser_login'){$('job-action').disabled=true;showStatus(await api('/api/browser-confirm',{job_id:lastState.job_id}));}else if(busy){$('job-action').disabled=true;showStatus(await api('/api/cancel',{}));}else if(['complete','demo'].includes(lastState.phase)){$('job').hidden=true;dismissedJob=lastState.job_id;}else{await library();const archive=archives.find(a=>a.slug===lastState.slug);if(archive)openDialog('resume',archive);else error('Сохранённая задача не найдена.');}}catch(e){error(e.message);$('job-action').disabled=false;}};
$('job-browser-cancel').onclick=async()=>{try{showStatus(await api('/api/cancel',{}));}catch(e){error(e.message);}};
$('job-settings').onclick=()=>showParameters(lastState);
$('settings-close').onclick=()=>$('settings-dialog').close();
let diagnosticURL='';
async function showDiagnostics(slug){
  $('diagnostics-content').textContent='Подготовка отчёта…';$('diagnostics-download').hidden=true;$('diagnostics-dialog').showModal();
  try{const report=await api('/api/diagnostics/'+encodeURIComponent(slug)),text=JSON.stringify(report,null,2);
    $('diagnostics-content').textContent=text;
    if(diagnosticURL)URL.revokeObjectURL(diagnosticURL);
    diagnosticURL=URL.createObjectURL(new Blob([text],{type:'application/json'}));
    $('diagnostics-download').href=diagnosticURL;$('diagnostics-download').hidden=false;
  }catch(e){$('diagnostics-content').textContent=e.message;}
}
$('job-diagnostics').onclick=()=>showDiagnostics(lastState.slug);
$('diagnostics-close').onclick=()=>$('diagnostics-dialog').close();
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
