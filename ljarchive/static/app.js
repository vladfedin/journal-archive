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
async function api(path,data,logNode){
  const method=data===undefined?'GET':'POST';
  const request=logNode?OperationLog.fetchLocal.bind(null,logNode):fetch;
  const at=Date.now()/1000;
  let response;
  try{
    response=await request(path,{method,headers:data===undefined?{}:{'Content-Type':'application/json'},
      body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',
      signal:path.startsWith('/api/communities')?AbortSignal.timeout(15000):undefined});
    const value=await response.json();
    if(!response.ok)throw new Error(value.error||'Не удалось выполнить запрос.');
    return value;
  }catch(err){
    err.localEvents=[{kind:'request',method,request_url:path.split('?')[0],at},
      {kind:'failure',at:Date.now()/1000,http_status:response?.status,reason:response?'http':err.name==='TimeoutError'?'timeout':'connection'}];
    throw err;
  }
}
function error(message,form=false){const target=$(form?'form-error':'app-error');target.textContent=message;target.hidden=!message;}
function setBusy(value){busy=value;document.querySelectorAll('[data-new],[data-update],[data-resume],[data-busy]').forEach(b=>b.disabled=value||moving||closed);$('submit-export').disabled=value||moving||submitting||preparingResume||needsCommunity()||closed;$('move-storage').disabled=value||moving||closed;$('move-storage').title=value?'После завершения загрузки':'';}
function setScope(value){scope=value;document.querySelectorAll('[data-scope]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scope===value)));$('period-fields').hidden=value!=='period';$('url-fields').hidden=value!=='urls';$('entry-urls').required=value==='urls';$('entry-urls').disabled=value!=='urls';if(!limitEdited)$('request-limit').value=value==='latest'?'50':'';$('request-limit').required=value==='latest';$('account').checked=value==='full';setSource();}
let communityRequest=0, communityTask=null;
function cancelCommunity(){
  communityRequest++;
  if(communityTask)api('/api/communities/cancel',{id:communityTask}).catch(()=>{});
  communityTask=null;
}
const choosingCommunity=()=>mode==='new'&&$('source-type').value==='community';
const needsCommunity=()=>choosingCommunity()&&!$('journal').value;
function resetCommunities(){
  cancelCommunity();
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
  OperationLog.bind(status,{message:'Запускаем поиск сообществ…',requests_used:0});
  let state={events:[]};
  try{
    state=await api('/api/communities',{username:$('username').value,password:$('password').value,background:true},status);
    if(token!==communityRequest){if(state.id)api('/api/communities/cancel',{id:state.id}).catch(()=>{});return;}
    communityTask=state.id;
    while(state.id){
      OperationLog.bind(status,state);
      if(status.textContent!==state.message)status.textContent=state.message;
      if(['complete','error'].includes(state.phase)){communityTask=null;break;}
      await new Promise(resolve=>setTimeout(resolve,500));
      if(token!==communityRequest)return;
      state=await api('/api/communities/'+communityTask);
      if(token!==communityRequest)return;
    }
    if(state.phase==='error')throw Error(state.message);
    const {communities}=state;
    if(communities.length){
      const select=$('journal');
      select.replaceChildren(...communities.map(name=>new Option(name,name)));
      if(communities.length>1){select.insertBefore(new Option('Выберите сообщество',''),select.firstChild);select.value='';}
      select.hidden=false;status.hidden=!state.id;
    }else{status.textContent='ЖЖ не вернул управляемых сообществ.';button.hidden=false;}
  }catch(e){
    if(token===communityRequest){
      status.textContent=e.message;button.hidden=false;
      OperationLog.bind(status,{...state,events:[...state.events||[],...e.localEvents||[]],phase:'error',message:e.message});
      if(communityTask)cancelCommunity();
      setBusy(busy);
    }
  }
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
    const checking=el('p','fine','Проверяем сохранённый прогресс…');$('resume-plan').append(checking);
    api('/api/recovery/'+encodeURIComponent(archive.slug),undefined,checking).then(data=>{if(token!==planToken)return;$('resume-plan').replaceChildren(gapsView(data,{plan:true,videos:archive.parameters?.videos!==false}));if(data.attempts?.length)$('resume-plan').append(attemptHistory(data.attempts));}).catch(()=>{if(token===planToken)$('resume-plan').replaceChildren(el('p','fine','Список пропусков недоступен. Можно продолжить с сохранённого этапа.'));}).finally(()=>{if(token===planToken){preparingResume=false;setBusy(busy);}});
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
  // The stage already says what is fetched: «Получаем комментарии» + «к записи «…» · страница 1».
  const text=s.message.replace(/^(Комментарии|Записи) (?=(к|за) )/,'');if(text!==s.message)s={...s,message:text};
  const entry=mapSlug===s.slug&&s.entry_id&&mapView?.items.find(i=>i.id===s.entry_id),mark='#'+s.entry_id;
  if(entry&&s.message.includes(mark)){const [before,...rest]=s.message.split(mark),link=el('a','',`«${entry.title}»`);link.href='/archive/'+encodeURIComponent(s.slug)+'/'+entry.url;return [before,link,rest.join(mark)];}
  return [s.message];
}
// What a card says about its archive: years, entries, comments, images, videos.
const ENTRY_FORMS=['запись','записи','записей'],COMMENT_FORMS=['комментарий','комментария','комментариев'],IMAGE_FORMS=['изображение','изображения','изображений'],VIDEO_FORMS=['видео','видео','видео'];
const kinds={full:'весь журнал',period:'за период',urls:'по ссылкам',latest:'тест · 10 записей'};
function yearsOf(data){const dates=(data?.entries||[]).map(e=>e.date).concat((data?.gaps||[]).map(g=>g.date)).filter(d=>/^\d{4}-/.test(d||'')).sort();if(!dates.length)return '';const a=dates[0].slice(0,4),z=dates.at(-1).slice(0,4);return a===z?a:`${a} — ${z}`;}
function updatedText(iso){const d=new Date(iso||'');if(isNaN(d))return '';const months='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(' ');return `${d.getDate()} ${months[d.getMonth()]}${d.getFullYear()!==new Date().getFullYear()?' '+d.getFullYear():''} в ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;}
function unavailableText(a){const parts=[];if(a.unavailable_discussions)parts.push(count(a.unavailable_discussions,['обсуждение','обсуждения','обсуждений']));const images=(a.unavailable_images||0)+(a.removed_images||0);if(images)parts.push(count(images,IMAGE_FORMS));return parts.length?'недоступно: '+parts.join(', '):'';}
async function confirmDelete(slug){
  const dialog=$('delete-dialog');
  let target;
  try{target=await api('/api/archives/delete-preview',{archive:slug});}
  catch(err){error(err.message);return;}
  if(dialog.open)return;
  dialog.dataset.slug=slug;
  dialog.dataset.token=target.token;
  $('delete-title').textContent='Удалить архив «'+slug+'»?';
  $('delete-path').textContent=target.path;
  $('delete-error').hidden=true;
  dialog.showModal();
  $('delete-cancel').focus();
}
$('delete-cancel').onclick=()=>$('delete-dialog').close();
$('delete-dialog').oncancel=e=>{if($('delete-confirm').disabled)e.preventDefault();};
$('delete-form').onsubmit=async e=>{
  e.preventDefault();
  const dialog=$('delete-dialog'),slug=dialog.dataset.slug,submit=$('delete-confirm');
  if(submit.disabled)return;
  submit.disabled=$('delete-cancel').disabled=true;
  submit.textContent='Удаляем…';
  try{
    await api('/api/archives/delete',{archive:slug,token:dialog.dataset.token});
    dialog.close();
    timelines.delete(slug);
    showStatus(await api('/api/status'));
    await library();
    announce('Архив «'+slug+'» удалён.');
    document.querySelector('[data-new]').focus();
  }catch(err){
    if(dialog.open){$('delete-error').textContent=err.message;$('delete-error').hidden=false;}
    else error(err.message);
  }finally{
    submit.disabled=$('delete-cancel').disabled=false;
    submit.textContent='Удалить с диска';
  }
};
// The archive's menu: its sections, downloads and maintenance. Ids only on the running card.
function archiveMenu(a,{job=false,resume=false,update=false}={}){
  const menu=el('div','menu'),group=title=>menu.append(el('h5','menu-title',title));const base=a.url?a.url.replace(/index\.html$/,''):'';
  const link=(text,href,id,download)=>{const n=el('a','',text);n.href=href;if(id)n.id=id;if(download)n.download=download;menu.append(n);return n;};
  const action=(text,fn,id)=>{const n=el('button','',text);n.type='button';if(id)n.id=id;n.onclick=()=>{n.closest('details')&&(n.closest('details').open=false);fn();};menu.append(n);return n;};
  if(a.url){
    group('Разделы архива');for(const [text,page] of [['Календарь','calendar.html'],['Хронология','timeline.html'],['Материалы','materials.html'],['Профиль','account.html'],['Переписка','messages.html']])link(text,base+page);
    menu.append(el('hr'));group('Скачать');link('ZIP-архив',a.zip_url||`/download/${a.slug}.zip`);link('JSON (ActivityStreams)',a.activitystreams_url||`/download/${a.slug}.activitystreams.json`);link('Исходный JSON',a.json_url||`/download/${a.slug}.json`);
    menu.append(el('hr'));
  }
  group('Обслуживание');
  if(resume)action('Докачать пропуски',()=>openDialog('resume',a)).dataset.busy='';
  if(update)action('Обновить архив',()=>{const archive=archives.find(x=>x.slug===lastState.slug);if(archive)openDialog('update',archive);else error('Архив не найден.');},'job-update').dataset.busy='';
  if(a.parameters)action('Параметры загрузки',()=>showParameters(job?lastState:a),job?'job-settings':'');
  action('Изображения: файлы и докачка',()=>window.ArchiveImages.open(a.slug),job?'job-images':'');
  if(a.url){link('Состояние архива',a.report_url||base+'report.html',job?'job-report':'');if(job)link('Пропущенные материалы и обсуждения',base+'report.html#skipped','job-skips');}
  action('Диагностика',()=>showDiagnostics(a.slug),job?'job-diagnostics':'');
  if(!job||terminal.has(lastState.phase)){menu.append(el('hr'));action('Удалить архив…',()=>confirmDelete(a.slug)).classList.add('delete-action');}
  return menu;
}
function gapsSummary(data,{running,plan}={}){
  if(!data?.available)return '';const rows=data.rows||[],open=rows.reduce((n,r)=>n+(r.remaining||0),0),shut=rows.reduce((n,r)=>n+(r.unavailable||0),0);
  return [open?`${running?'докачиваем':plan?'будем докачивать':'осталось докачать'} ${num(open)}`:'',shut?`недоступно ${num(shut)}`:''].filter(Boolean).join(' · ');
}
let jobVisible=false,jobMenuKey='',pendingFocus='';
// After a finished job card hides, keyboard focus goes to its archive's main action instead of the page body.
// The card is rebuilt with the finished data a moment later; until then focus waits on the journal name, which the rebuild keeps.
function holdFocus(){const card=pendingFocus&&cards.get(pendingFocus);if(!card||card.li.hidden)return;const name=card.li.querySelector('a.journal-name')||card.li.querySelector('.vol-name');if(!name)return;if(!name.matches('a'))name.tabIndex=-1;name.focus({preventScroll:true});}
function announce(text){const live=$('library-status');live.textContent='';setTimeout(()=>{live.textContent=text;},60);}
function focusPending(){const card=pendingFocus&&cards.get(pendingFocus);if(!card||card.li.hidden)return false;const target=card.li.querySelector('.vol-actions .btn.primary')||card.li.querySelector('.vol-actions .btn:not(:disabled)');if(!target)return false;target.focus({preventScroll:true});pendingFocus='';return true;}
function showStatus(s){lastState=s;showContact(s);
  const material=s.material_task,materialRunning=material&&!['complete','error','needs_browser','cancelled'].includes(material.phase);
  const clock=s.server_time||Date.now()/1000,done=terminal.has(s.phase),stalled=!done&&s.worker_alive===false;
  const finishedWithSkips=s.phase==='partial'&&s.finished_with_skips&&!s.request_limit_reached;
  const ready=['complete','demo'].includes(s.phase)&&!s.request_limit_reached;
  // A finished archive goes back to being an ordinary card; the job card is for work in progress and what needs a decision.
  const show=!!s.phase&&s.phase!=='idle'&&s.slug!=='_demo'&&!(s.job_id&&s.job_id===dismissedJob)&&!((ready||finishedWithSkips)&&!materialRunning);
  if(jobVisible!==show){
    if(!show&&jobVisible&&(ready||finishedWithSkips)){announce(`Архив ${s.username||''} готов`);if($('job').contains(document.activeElement))pendingFocus=s.slug;}
    jobVisible=show;syncCards();
  }
  if(s.slug)OperationLog.bind($('job-stage'),material||s);
  $('job').hidden=!show;if(!show){setBusy(!done||materialRunning);holdFocus();return;}
  $('job-title').textContent=s.username||'';if(s.archive_url)$('job-title').href=s.archive_url;else $('job-title').removeAttribute('href');
  const request=s.request||{},activity=s.last_activity||{},silent=!done&&network.has(s.phase)&&request.at&&request.at>(s.last_response_at||s.last_success_at||0)&&activity.kind==='request'&&clock-request.at>60;
  let stage=s.cancelling?'Останавливаем…':stalled?'Загрузка прервалась':s.request_limit_reached?'Достигнут лимит запросов':done?{...finished,partial:'Не завершено'}[s.phase]||labels[s.phase]:doing[s.phase]||'Загружаем';
  if(finishedWithSkips)stage='Архив готов';
  if(s.phase==='browser_login'&&!s.cancelling)stage='Вход в браузере';
  if(s.phase==='waiting'&&!s.cancelling&&!stalled)stage=s.retry_at>clock?'Пауза до '+new Date(s.retry_at*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'Продолжаем после паузы';
  if(materialRunning)stage=material.phase==='waiting'?'Пауза перед запросом':material.phase==='saving'?'Обновляем страницы архива':'Скачиваем изображения';
  if(material?.phase==='complete'&&!s.request_limit_reached&&s.phase==='partial')stage='Докачка изображений завершена';
  // Only this sentence is announced; it changes with the stage, not every second.
  if($('job-stage').textContent!==stage)$('job-stage').textContent=stage;
  const tone=stalled||s.phase==='error'?'error':s.phase==='waiting'||silent?'wait':(['complete','demo'].includes(s.phase)||finishedWithSkips)&&!s.request_limit_reached?'done':done?'warn':'run';
  $('job-state').dataset.tone=tone;$('job').dataset.tone=tone;$('job').classList.toggle('active',!done||materialRunning);
  const kind=s.operation==='resume'?'докачка пропусков':s.parameters?.entry_limit?`тест · ${count(s.parameters.entry_limit,ENTRY_FORMS)}`:kinds[s.parameters?.mode]||'';
  $('job-kind').hidden=!kind||(done&&!materialRunning);$('job-kind').textContent=kind;
  const now=material?[material.message||'']:nowContent(s,clock);if(material?.total)now.push(` · ${material.completed||0} / ${material.total}`);if(silent&&!material)now.unshift(`Ожидаем ответ${request.host?' от '+request.host:''} · ${minutes(clock-request.at)}. `);
  else if(!done&&network.has(s.phase)&&activity.kind==='failure'&&activity.reason==='web_login')now.splice(0,now.length,'Не удалось подтвердить вход для изображений. Полученное сохраняется.');
  else if(!done&&network.has(s.phase)&&activity.kind==='redirect')now.unshift('ЖЖ перенаправляет запрос · '+(activity.http_status||302)+'. ');
  // «Входим в ЖЖ» + «Входим в ЖЖ и проверяем доступ…» → «Входим в ЖЖ · проверяем доступ…».
  if(typeof now[0]==='string'&&now[0].toLowerCase().startsWith(stage.toLowerCase()))now[0]=now[0].slice(stage.length).replace(/^[\s,.:;—–-]*(и\s+)?/,'');
  if(now.length===1&&!now[0])now.length=0;
  $('job-now').hidden=!now.length;$('job-message').replaceChildren(...now);$('job-now').classList.toggle('is-problem',['error','partial'].includes(s.phase)||stalled);
  // Facts: what is already there, not promises. Before the calendar — what was asked for.
  // Period and link updates cover part of the history; a whole-journal run (or its resume) covers all of it.
  const data=mapSlug===s.slug&&mapView?.data,facts=[],narrow=['period','urls'].includes(s.parameters?.mode)&&!!data?.task;
  if(data?.expected){const years=yearsOf(data),own=data.task||data;if(years)facts.push(years);
    // A period or link update covers part of the history: the whole archive and this run are told apart.
    // Entries of this run saved earlier also count as ready, so a run's share is its size, not a fraction done.
    if(narrow){facts.push(count(data.known,ENTRY_FORMS));if(own.expected)facts.push(`в этой загрузке ${count(own.expected,ENTRY_FORMS)}`);}
    else facts.push(`${num(data.ready)} из ${count(data.expected,ENTRY_FORMS)}`);
    facts.push(count(s.comments,COMMENT_FORMS));}
  else{const p=s.parameters||{};if(s.entries)facts.push(count(s.entries,ENTRY_FORMS));if(s.comments)facts.push(count(s.comments,COMMENT_FORMS));if(!facts.length){if(p.images)facts.push('изображения');if(p.videos)facts.push('видео');if(p.account)facts.push('профиль, друзья, юзерпики');if(p.messages)facts.push('переписка');}}
  $('job-facts').textContent=facts.join(' · ');
  const meta=[];
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
  if(!done&&!s.cancelling)meta.push('полученное сохраняется, продолжить можно позже');
  $('job-meta').textContent=meta.length?meta.join(' · ').replace(/^./,c=>c.toUpperCase()):'';
  // The thin line on top of the card: the stage's own count when it has one, otherwise entries saved.
  const progress=$('progress');progress.hidden=done&&!materialRunning;
  if(s.total&&localProgress(s.phase)){progress.max=s.total;progress.value=s.completed||0;}
  else if(data?.expected&&!narrow){progress.max=data.expected;progress.value=data.ready||0;}
  else if(s.total){progress.max=s.total;progress.value=s.completed||0;}else progress.removeAttribute('value');
  $('job-wait').hidden=!!data?.expected||done||['preparing','verify'].includes(s.phase)&&!!s.archive_url;
  $('open-result').hidden=!s.archive_url;if(s.archive_url)$('open-result').href=s.archive_url;
  $('job-statistics').hidden=!s.archive_url;if(s.archive_url)$('job-statistics').href=s.archive_url.replace(/index\.html$/,'statistics.html');
  const menuKey=JSON.stringify([s.slug,s.archive_url,!!s.parameters,done]);
  if(menuKey!==jobMenuKey){jobMenuKey=menuKey;const info={slug:s.slug,username:s.username,url:s.archive_url,report_url:s.archive_url&&s.archive_url.replace(/index\.html$/,'report.html'),parameters:s.parameters};$('job-menu').replaceChildren(...archiveMenu(info,{job:true,update:done&&!!s.archive_url}).childNodes);setBusy(busy);}
  $('job-more').hidden=!s.slug||s.slug==='_demo';
  const recoveryBox=$('job-recovery');
  if(recoveryBox.dataset.job!==s.job_id){recoveryBox.open=false;recoveryBox.dataset.job=s.job_id||'';}
  const summary=gapsSummary(s.recovery,{running:!done});
  recoveryBox.hidden=!s.recovery?.available||!summary&&!(s.operation==='resume'&&resolvedText(s.recovery.rows||[]));
  $('job-gaps-summary').textContent=summary||'пропусков нет';
  const recoveryKey=JSON.stringify([s.job_id,s.operation,done,s.recovery]);if(recoveryBox.dataset.key!==recoveryKey){recoveryBox.dataset.key=recoveryKey;$('job-recovery-content').replaceChildren(gapsView(s.recovery,{attempt:s.operation==='resume',running:!done,videos:s.parameters?.videos!==false}));}
  const action=$('job-action');
  action.disabled=!!s.cancelling;action.textContent=!done?'Остановить':s.phase==='cancelled'||s.phase==='error'?'Продолжить':'Докачать пропуски';
  action.className='btn'+(done?' primary':'');
  $('job-browser-cancel').hidden=s.phase!=='browser_login';$('job-browser-cancel').disabled=!!s.cancelling;
  if(s.phase==='browser_login'){action.textContent='Продолжить';action.className='btn primary';progress.hidden=true;}
  if(materialRunning){action.disabled=true;$('job-state').dataset.tone='run';}
  setBusy(!done||materialRunning);
}
// One card per journal. Cards are kept between refreshes while their data is the same, so open menus,
// focus and the position of the entries survive polling.
const cards=new Map(),timelines=new Map();
function syncCards(){
  for(const [slug,card] of cards)card.li.hidden=jobVisible&&slug===lastState.slug;
  const visible=[...cards.values()].filter(c=>!c.li.hidden).length+(jobVisible?1:0);
  $('library').hidden=!visible;$('empty').hidden=!!visible||!libraryKey;
}
function justFinished(){const s=lastState,ago=(s.server_time||Date.now()/1000)-(s.last_progress_at||s.last_saved_at||0);return !!s.job_id&&(s.phase==='complete'||s.phase==='partial'&&s.finished_with_skips)&&ago<1800;}
function freshFor(a){return lastState.slug===a.slug&&justFinished()?lastState.job_id:'';}
function cardStatus(a){
  const status=el('div','vol-status'),state=el('span','vol-state'),fresh=freshFor(a);state.append(el('span','job-dot'));
  const ready=a.status==='complete'||a.finished_with_skips||a.status==='demo';
  status.dataset.tone=ready?'done':a.status==='error'?'error':'warn';
  state.append(document.createTextNode(ready?'Готов':a.status==='cancelled'?'Остановлено':a.status==='error'?'Загрузка прервалась':'Не завершено'));status.append(state);
  const text=[];
  if(ready){
    if(fresh){const took=(lastState.working_seconds||0)+(lastState.waiting_seconds||0);text.push(el('span','vol-fresh','обновлён только что'));if(took)text.push(document.createTextNode('\u00a0· заняло '+minutes(took)));}
    else if(a.created_at)text.push(document.createTextNode('обновлён '+updatedText(a.created_at)));
    const lost=unavailableText(a);if(lost)text.push(document.createTextNode((text.length?'\u00a0· ':'')+lost));
  }else if(a.message)text.push(document.createTextNode(a.message));
  // Finished, but some gaps can still be fetched: the way to them is right here, not in a menu.
  const resume=ready&&a.resumable&&a.finished_with_skips&&a.open_gaps;
  if(resume)text.push(document.createTextNode((text.length?'\u00a0· ':'')+'можно докачать: '+num(a.open_gaps)));
  if(text.length){const span=el('span','vol-text');span.append(...text);status.append(span);}
  if(resume){const go=el('button','link-button vol-resume','Докачать');go.type='button';go.dataset.busy='';go.onclick=()=>openDialog('resume',a);status.append(go);}
  return status;
}
function cardFacts(a,data){
  const facts=[],years=yearsOf(data);if(years)facts.push(years);
  facts.push(count(a.entries_saved||0,ENTRY_FORMS),count(a.comments_saved||0,COMMENT_FORMS));
  if(a.images_saved)facts.push(count(a.images_saved,IMAGE_FORMS));if(a.videos_saved)facts.push(count(a.videos_saved,VIDEO_FORMS));
  const mode=a.parameters?.mode;const scope=a.entry_limit?`тест · ${count(a.entry_limit,ENTRY_FORMS)}`:mode==='period'?[day(a.parameters.date_from),day(a.parameters.date_to)].filter(Boolean).join(' — '):'';
  if(scope)facts.push(scope);
  return facts.join(' · ');
}
// Restoring focus after a rebuild looks an action up by its text.
function labelActions(root){root.querySelectorAll('a,button,summary').forEach(action=>action.dataset.action=action.textContent||action.getAttribute('aria-label'));}
function cardGaps(a){
  if(!a.unavailable_discussions&&!a.unavailable_images&&!a.removed_images)return null;
  const gaps=el('details','gaps'),sum=el('summary','','Что не удалось получить'),body=el('div');gaps.append(sum,body);
  gaps.ontoggle=()=>{if(!gaps.open||body.childNodes.length)return;const checking=el('p','fine','Проверяем…');body.append(checking);api('/api/recovery/'+encodeURIComponent(a.slug)+'?whole=1',undefined,checking).then(data=>{body.replaceChildren(gapsView(data,{videos:a.parameters?.videos!==false}));const settled=[[data.confirmed?.images_missing,'нет в ЖЖ'],[data.confirmed?.images_refused,'доступ закрыт']].filter(([n])=>n).map(([n,why])=>`${count(n,IMAGE_FORMS)} — ${why}`);if(settled.length)body.append(el('p','gaps-note',`Браузер с вашим входом не получил: ${settled.join(', ')}. На страницах они остались ссылками на оригинал, докачка их не вернёт.`));}).catch(()=>body.replaceChildren(el('p','fine','Список пропусков недоступен.')));};
  return gaps;
}
// Counts arrive after the card (the server counts them in the background): only the status and the details change,
// the strip keeps its place and its open state.
function refreshCounts(card,a){
  const status=cardStatus(a);labelActions(status);card.li.querySelector(':scope>.vol-status')?.replaceWith(status);
  const old=card.li.querySelector(':scope>details.gaps'),gaps=cardGaps(a);
  if(gaps){labelActions(gaps);if(old){old.replaceWith(gaps);gaps.open=old.open;}else card.li.append(gaps);}else old?.remove();
}
function buildCard(a,index,firstFolded){
  const li=el('li','volume');li.dataset.archive=a.slug;
  if(a.deletion_pending){
    const retry=el('button','btn','Завершить удаление');
    retry.onclick=()=>confirmDelete(a.slug);
    const head=el('div','vol-head');head.append(el('h2','vol-name',a.slug),retry);
    li.append(head);return {li,view:null};
  }
  const head=el('div','vol-head'),title=el('div','vol-title'),name=el('h2','vol-name'),link=el(a.url?'a':'span','journal-name',a.username);if(a.url)link.href=a.url;name.append(link);title.append(name);
  if(a.url){const stat=el('a','vol-stat');stat.href=a.url.replace(/index\.html$/,'statistics.html');stat.innerHTML=svg('statistics');stat.append('Статистика');title.append(stat);}
  const actions=el('div','vol-actions'),resumable=a.resumable&&!a.finished_with_skips;
  const main=el('button','btn'+(resumable?' primary':''));main.type='button';
  if(resumable){main.textContent=resumeLabel(a);main.dataset.resume='';main.onclick=()=>openDialog('resume',a);}
  else{main.innerHTML=svg('refresh');main.append('Обновить');main.dataset.update='';main.onclick=()=>openDialog('update',a);}
  actions.append(main);
  if(a.url){const open=el('a','btn'+(resumable?'':' primary'));open.href=a.url;open.innerHTML=svg('open');open.append('Открыть архив');actions.append(open);}
  const more=el('details','more'),summary=el('summary','','⋯');summary.setAttribute('aria-label','Ещё действия с архивом '+a.username);more.append(summary,archiveMenu(a));actions.append(more);
  const facts=el('p','vol-facts',cardFacts(a));
  head.append(title,actions,facts);li.append(head);
  let view=null;
  if(a.url){
    const root=el('div','timeline');li.append(root);
    view=new window.ArchiveTimeline(root,a.url.replace(/index\.html$/,''),{summary:false,collapsible:true,open:index===0,hint:firstFolded});
    const cached=timelines.get(a.slug);
    const show=data=>{view.set(data,null);facts.textContent=cardFacts(a,data);root.hidden=!data.expected;};
    if(cached?.key===a.created_at)show(cached.data);
    else api('/api/timeline/'+encodeURIComponent(a.slug)).then(data=>{timelines.set(a.slug,{key:a.created_at,data});if(li.isConnected||cards.get(a.slug)?.li===li)show(data);}).catch(()=>{root.hidden=true;});
  }
  li.append(cardStatus(a));
  // Just finished with a browser image check: its result stays on the card.
  const photos=freshFor(a)&&lastState.browser_images_progress;
  if(photos)li.append(el('p','vol-photos',`Изображения · сохранено ${num(photos.saved)} · недоступно ${num(photos.unavailable)} · к повтору ${num(photos.pending)}`));
  const gaps=cardGaps(a);if(gaps)li.append(gaps);
  labelActions(li);
  return {li,view};
}
let gapsTimer=0;
async function library(){const data=await api('/api/archives');storageStatus(data.relocation);
  // The server counts gaps in the background; ask again soon instead of waiting for the next idle poll.
  if(!gapsTimer&&data.archives.some(a=>a.gaps_pending))gapsTimer=setTimeout(()=>{gapsTimer=0;library().catch(()=>{});},1500);
  const key=JSON.stringify([data,freshKey()]);if(key===libraryKey){setBusy(busy);focusPending();return;}libraryKey=key;
  archives=data.archives.filter(a=>a.slug!=='_demo');$('output-path').textContent=data.output;
  $('library-count').textContent=archives.length?count(archives.length,['журнал','журнала','журналов']):'';
  const list=$('archives'),seen=new Set();let index=jobVisible?1:0,firstFolded=true,position=0;
  // Focus is restored after the cards are in place: moving a node in the DOM drops its focus.
  const active=document.activeElement,focusSlug=active?.closest?.('#archives .volume')?.dataset.archive,focusAction=active?.dataset?.action;
  for(const a of archives){
    seen.add(a.slug);const {gaps_pending,unavailable_discussions,unavailable_images,removed_images,open_gaps,...stable}=a,cardKey=JSON.stringify([stable,freshFor(a)]),counts=JSON.stringify([unavailable_discussions,unavailable_images,removed_images,open_gaps]);let card=cards.get(a.slug);
    const visibleIndex=jobVisible&&a.slug===lastState.slug?-1:index++;
    if(!card||card.key!==cardKey){
      const wasOpen=card?.li.querySelector('details.more')?.open;
      card?.view?.destroy();const built=buildCard(a,visibleIndex,firstFolded&&visibleIndex>0);
      if(built.view&&visibleIndex>0)firstFolded=false;
      const nextMenu=built.li.querySelector('details.more');
      if(wasOpen&&nextMenu)nextMenu.open=true;
      if(card)card.li.replaceWith(built.li);
      card={...built,key:cardKey,counts};cards.set(a.slug,card);
    }else{
      if(card.counts!==counts){refreshCounts(card,a);card.counts=counts;}
      if(card.view&&visibleIndex>0)firstFolded=false;
    }
    const at=list.children[position];if(at!==card.li)list.insertBefore(card.li,at||null);position++;
  }
  for(const [slug,card] of cards)if(!seen.has(slug)){card.view?.destroy();card.li.remove();cards.delete(slug);}
  // The same action again, or, when it is gone (nothing left to fetch), the card's main action.
  const holder=focusSlug&&cards.get(focusSlug)?.li;
  if(!focusPending()&&holder&&!holder.hidden&&!holder.contains(document.activeElement))(Array.from(holder.querySelectorAll('[data-action]')).find(n=>n.dataset.action===focusAction)||holder.querySelector('.vol-actions .btn.primary:not(:disabled)')||holder.querySelector('.vol-actions .btn:not(:disabled)'))?.focus({preventScroll:true});
  syncCards();setBusy(busy);
}
function freshKey(){return justFinished()?lastState.slug+':'+lastState.job_id:'';}
let mapView=null,mapSlug='',mapTime=0,mapPhase='';
async function refreshMap(state){
  if(!state.slug||state.slug==='_demo'||!jobVisible)return;
  if(mapSlug!==state.slug){mapView?.destroy();mapSlug=state.slug;mapTime=0;mapView=new window.ArchiveTimeline($('job-timeline'),'/archive/'+encodeURIComponent(state.slug)+'/',{summary:false});}
  if(Date.now()-mapTime<8000&&mapPhase===state.phase){mapView.active=terminal.has(state.phase)?null:state.entry_id;mapView.draw();return;}
  mapTime=Date.now();mapPhase=state.phase;
  const data=await api('/api/timeline/'+encodeURIComponent(state.slug));
  const first=$('job-timeline').hidden&&data.expected;$('job-timeline').hidden=!data.expected;mapView.set(data,data.active);
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
  if(phase!=='idle')OperationLog.bind($('update-status'),{...update,message:messages[phase],completed:update.bytes});
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
$('export-dialog').addEventListener('close',()=>{$('password').value='';planToken++;cancelCommunity();preparingResume=false;setBusy(busy);});
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
$('job-action').onclick=async()=>{try{if(lastState.phase==='browser_login'){$('job-action').disabled=true;showStatus(await api('/api/browser-confirm',{job_id:lastState.job_id}));}else if(busy){$('job-action').disabled=true;showStatus(await api('/api/cancel',{}));}else{await library();const archive=archives.find(a=>a.slug===lastState.slug);if(archive)openDialog('resume',archive);else error('Сохранённая задача не найдена.');}}catch(e){error(e.message);$('job-action').disabled=false;}};
$('job-browser-cancel').onclick=async()=>{try{showStatus(await api('/api/cancel',{}));}catch(e){error(e.message);}};
$('settings-close').onclick=()=>$('settings-dialog').close();
let diagnosticURL='';
async function showDiagnostics(slug){
  const checking=el('span','fine','Подготовка отчёта…');$('diagnostics-content').replaceChildren(checking);$('diagnostics-download').hidden=true;$('diagnostics-dialog').showModal();
  try{const report=await api('/api/diagnostics/'+encodeURIComponent(slug),undefined,checking),text=JSON.stringify(report,null,2);
    $('diagnostics-content').textContent=text;
    if(diagnosticURL)URL.revokeObjectURL(diagnosticURL);
    diagnosticURL=URL.createObjectURL(new Blob([text],{type:'application/json'}));
    $('diagnostics-download').href=diagnosticURL;$('diagnostics-download').hidden=false;
  }catch(e){$('diagnostics-content').textContent=e.message;}
}
$('diagnostics-close').onclick=()=>$('diagnostics-dialog').close();
$('quit').onclick=async()=>{try{await api('/api/quit',{});closed=true;clearTimeout(pollTimer);setBusy(false);$('quit').disabled=true;$('job').hidden=true;error('Приложение закрыто.');}catch(e){error(e.message);}};
poll();

function storageStatus(state){
  if(!state)return;const wasMoving=moving;moving=state.phase==='copying';
  $('storage-submit').disabled=moving;$('storage-path').disabled=moving;
  if(state.phase!=='idle')OperationLog.bind($('storage-status'),state);
  $('storage-status').textContent=moving?`Перенос · ${state.completed||0} / ${state.total||0} файлов`:state.phase==='idle'?'':state.message||'';
  if(wasMoving&&state.phase==='complete'&&state.message==='Перенесено')$('storage-dialog').close();
  setBusy(busy);
}
$('move-storage').onclick=()=>{if(busy||moving||closed)return;$('storage-path').value=$('output-path').textContent;$('storage-status').textContent='';$('storage-dialog').showModal();$('storage-path').focus();$('storage-path').select();};
$('storage-close').onclick=()=>$('storage-dialog').close();
$('storage-form').onsubmit=async e=>{e.preventDefault();$('storage-submit').disabled=true;try{storageStatus(await api('/api/storage/move',{path:$('storage-path').value}));await library();}catch(err){$('storage-status').textContent=err.message;$('storage-submit').disabled=false;}};
