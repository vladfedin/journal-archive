'use strict';
document.addEventListener('DOMContentLoaded', () => {
  const match=location.pathname.match(/^\/archive\/([a-z0-9_-]+)\//);
  const onlineApp=!!match&&document.body.dataset.libraryUrl==='/'&&location.protocol==='http:';
  const root=match?'/archive/'+encodeURIComponent(match[1])+'/':'';
  const seen=new WeakSet();
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  async function api(path,payload){
    const response=await fetch(path,{method:payload?'POST':'GET',cache:'no-store',headers:payload?{'Content-Type':'application/json'}:{},body:payload?JSON.stringify(payload):undefined});
    const data=await response.json();if(!response.ok)throw Error(data.error||'Не удалось выполнить запрос');return data;
  }
  const previousTasks=onlineApp?api('/api/materials/'+encodeURIComponent(match[1])).then(r=>r.tasks||[]).catch(()=>[]):Promise.resolve([]);
  function player(path){const video=el('video');video.controls=true;video.preload='none';video.src=path;return video;}
  function widget(slot){
    if(seen.has(slot))return;seen.add(slot);
    const value=slot.dataset.widgetUrl;
    if(!/^https:\/\/music\.yandex\.ru\/iframe\/(?:album\/\d+(?:\/track\/\d+)?|track\/\d+)$/.test(value||''))return;
    const button=el('button','btn material-download','Онлайн-плеер');button.type='button';
    button.onclick=()=>{const frame=el('iframe');frame.src=value;frame.title='Яндекс Музыка · онлайн';frame.allow='autoplay; encrypted-media';frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-popups');frame.referrerPolicy='no-referrer';slot.append(frame);button.remove();};
    slot.append(button);
  }
  function setup(slot){
    if(seen.has(slot)||!onlineApp)return;seen.add(slot);
    const kind=slot.dataset.materialKind,value=slot.dataset.materialValue;
    const eid=Number(slot.closest('[data-entry-id]')?.dataset.entryId||location.pathname.match(/\/entries\/(\d+)\.html$/)?.[1]);
    if(!eid)return;
    if(!slot.querySelector('.material-source')&&!slot.closest('.gallery-card')?.querySelector('.material-source')){const source=el('a','material-source',value);try{const u=new URL(value);if(['http:','https:'].includes(u.protocol))source.href=value;}catch{}source.target='_blank';source.rel='noreferrer noopener';slot.append(source);}
    const controls=el('span','material-controls'),button=el('button','btn material-download',kind==='embed'?'Загрузить вставку':'Загрузить');
    button.type='button';const status=el('span','material-status');status.setAttribute('role','status');
    const browser=el('button','btn material-download','Через браузер');browser.type='button';
    let browserEligible=kind==='embed';try{const u=new URL(value);browserEligible=(kind==='image'&&['ic.pics.livejournal.com','pics.livejournal.com','l-userpic.livejournal.com','userpic.livejournal.com'].includes(u.hostname))||(kind==='video'&&u.hostname==='vc.videos.livejournal.com'&&u.pathname==='/index/player');}catch{}
    browser.hidden=!browserEligible;
    const cancel=el('button','btn text','Отмена');cancel.type='button';cancel.hidden=true;
    const evidence=el('details','material-evidence');evidence.hidden=true;
    const meter=el('progress','material-progress');meter.hidden=true;meter.setAttribute('aria-label','Загрузка файла');
    controls.append(button,browser,cancel,status,meter,evidence);slot.append(controls);
    let taskId=null,inserted=false,auth=null,browserConfirmed=false,checkBrowser=false;
    const size=value=>(value/1000000).toLocaleString('ru-RU',{maximumFractionDigits:1})+' МБ';
    const stages={source:'Источник',access:'Доступ',download:'Скачивание',file:'Файл',index:'Архив'};
    function details(error){
      if(!error)return;evidence.hidden=false;evidence.replaceChildren(el('summary','','Подробности'));
      evidence.append(el('p','',error.updated_at?new Date(error.updated_at).toLocaleString('ru-RU'):''));
      for(const item of error.trace||[])evidence.append(el('p','',`${stages[item.stage]?stages[item.stage]+' · ':''}${item.transport==='browser'?'Браузер':'HTTP'} · ${item.http_status||item.network_error||'ответ не получен'} · ${item.url}`));
      if(!error.trace?.length)evidence.append(el('p','',error.requests===0?'Запрос файла не отправлен':`Запросов: ${error.requests??'нет данных'} · HTTP ${error.http_status??'нет данных'}`));
    }
    function showAsset(asset){
      if(inserted||!asset||asset.kind==='unavailable')return;
      if(asset.kind==='external'){
        const recognized=el('span','external-widget');recognized.dataset.widgetUrl=asset.embed_url;
        const link=el('a','',asset.provider+' ↗');link.href=asset.url;link.target='_blank';link.rel='noreferrer noopener';
        recognized.append(link,el('small','','Онлайн · аудиофайл не сохранён'));slot.insertBefore(recognized,controls);widget(recognized);
      }else if(/^media\/[a-f0-9]{64}\.(?:png|jpg|gif|webp|avif|mp4|webm|ogv|mov|m4v)$/.test(asset.path||'')){
        const item=asset.kind==='image'?el('img'):player(root+asset.path);
        if(asset.kind==='image'){item.src=root+asset.path;item.alt=slot.querySelector('a')?.textContent||'Изображение';}
        const preview=slot.closest('.gallery-card')?.querySelector('.gallery-preview');
        const photoLink = asset.kind === 'image' && preview?.querySelector('a.gallery-empty');
        if (photoLink) {
          photoLink.classList.remove('gallery-empty');
          photoLink.replaceChildren(item);
        } else if (preview) preview.replaceChildren(item);
        else slot.insertBefore(item,controls);
      }else{throw Error('Некорректный путь сохранённого материала');}
      slot.closest('.gallery-card')?.querySelector('.material-source')?.remove();slot.querySelector('.material-source')?.remove();
      inserted=true;button.hidden=true;browser.hidden=true;cancel.hidden=true;auth?.remove();slot.querySelector('.material-last-result')?.remove();
    }
    function login(){
      if(auth)return;auth=el('form','material-login');
      const label=el('label','','Пароль ЖЖ '),password=el('input','input');password.type='password';password.autocomplete='current-password';password.required=true;password.maxLength=1024;label.append(password);
      const submit=el('button','btn','Повторить');auth.append(label,submit);controls.append(auth);
      auth.onsubmit=event=>{event.preventDefault();const value=password.value;password.value='';auth.remove();auth=null;start(value);};password.focus();
    }
    function show(state){
      showAsset(state.asset);
      details(state.error);
      let text=state.message||'Загрузка…';
      if(state.phase==='waiting'&&state.retry_at){const left=Math.max(0,Math.ceil(state.retry_at-Date.now()/1000));text=`Повтор через ${left} с`;}
      else if(state.progress_unit==='bytes'){const mb=v=>(v/1000000).toLocaleString('ru-RU',{maximumFractionDigits:1});text+=` · ${mb(state.completed||0)} МБ`+(state.total>0?` / ${mb(state.total)} МБ`:'');}
      else if(state.total>0)text+=` · ${state.completed||0} / ${state.total}`;
      meter.hidden=state.progress_unit!=='bytes'||state.phase!=='running';meter.max=state.total||1;if(state.total)meter.value=state.completed||0;else meter.removeAttribute('value');
      if(state.requests_used)text+=` · ${state.requests_used}${state.request_limit?' / '+state.request_limit:''} запросов`;
      status.textContent=text;
      const done=['complete','error','needs_auth','needs_browser','cancelled'].includes(state.phase);
      button.disabled=!done;browser.disabled=!done;cancel.hidden=done||inserted;
      if(state.phase==='complete'&&state.asset?.kind==='unavailable'){
        button.textContent='Проверить снова';
      }else if(state.phase==='complete'){
        if(state.asset?.kind==='video'){const result=el('div','material-result','Сохранено'+(state.asset.bytes?' · '+size(state.asset.bytes):''));details(state.asset);result.append(evidence);slot.append(result);}
        controls.remove();
      }
      else if(state.phase==='needs_auth'){button.hidden=true;login();}
      else if(state.phase==='needs_browser'){browserConfirmed=true;checkBrowser=false;browser.textContent='Продолжить';browser.hidden=false;}
      else if(done&&!inserted){button.textContent='Повторить';button.hidden=false;}
      if(state.phase==='error'&&state.error?.session_type==='browser'){
        checkBrowser=['browser_login','browser_closed'].includes(state.error.reason);
        browser.textContent=checkBrowser?'Проверить вход':'Через браузер';
      }
      return done;
    }
    async function poll(){
      try{const done=show(await api('/api/material/'+taskId));if(!done)setTimeout(poll,1000);}
      catch(error){status.textContent=error.message;button.disabled=false;button.textContent='Повторить';cancel.hidden=true;}
    }
    async function start(password,transport){
      button.disabled=true;browser.disabled=true;status.textContent='В очереди…';
      const payload={archive:match[1],entry_id:eid,kind,value};if(password)payload.password=password;
      if(transport){payload.transport=transport;payload.confirmed=browserConfirmed;payload.open_browser=checkBrowser;auth?.remove();auth=null;}
      try{const state=await api('/api/material',payload);taskId=state.id;if(!show(state))setTimeout(poll,600);}
      catch(error){status.textContent=error.message;button.disabled=false;browser.disabled=false;}
      finally{payload.password='';}
    }
    previousTasks.then(tasks=>{if(taskId)return;const previous=tasks.filter(t=>t.entry_id===eid&&t.kind===kind&&t.value===value).at(-1);if(previous){taskId=previous.id;if(!show(previous))setTimeout(poll,300);}});
    button.onclick=()=>start();
    browser.onclick=()=>start(null,'browser');
    try{details(JSON.parse(slot.dataset.materialError||'null'));}catch{}
    cancel.onclick=async()=>{if(taskId){try{show(await api('/api/material/cancel',{id:taskId}));}catch(error){status.textContent=error.message;}}};
  }
  function scan(){
    for(const a of document.querySelectorAll('a[data-video-url]')){
      if(seen.has(a))continue;seen.add(a);
      const path=a.dataset.videoPath;
      if(path&&/^(?:\.\.\/)?media\/[a-f0-9]{64}\.(?:mp4|webm|ogv|mov|m4v)$/.test(path)){a.after(player(path));}
      else if(onlineApp){const slot=el('span','material-slot');slot.dataset.materialKind='video';slot.dataset.materialValue=a.dataset.videoUrl;const source=a.nextElementSibling?.matches('.material-source')?a.nextElementSibling:null;a.after(slot);if(source)slot.append(source);setup(slot);}
    }
    document.querySelectorAll('.material-slot').forEach(setup);
    document.querySelectorAll('.external-widget').forEach(widget);
  }
  let scheduled=false;
  new MutationObserver(()=>{if(!scheduled){scheduled=true;requestAnimationFrame(()=>{scheduled=false;scan();});}}).observe(document.body,{childList:true,subtree:true});
  scan();
});
