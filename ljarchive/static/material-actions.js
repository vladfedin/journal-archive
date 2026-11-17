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
    const controls=el('span','material-controls'),button=el('button','btn material-download',kind==='embed'?'Загрузить вставку':'Загрузить');
    button.type='button';const status=el('span','material-status');status.setAttribute('role','status');
    const cancel=el('button','btn text','Отмена');cancel.type='button';cancel.hidden=true;
    controls.append(button,cancel,status);slot.append(controls);
    let taskId=null,inserted=false,auth=null;
    function showAsset(asset){
      if(inserted||!asset)return;
      if(asset.kind==='external'){
        const recognized=el('span','external-widget');recognized.dataset.widgetUrl=asset.embed_url;
        const link=el('a','',asset.provider+' ↗');link.href=asset.url;link.target='_blank';link.rel='noreferrer noopener';
        recognized.append(link,el('small','','Онлайн · аудиофайл не сохранён'));slot.insertBefore(recognized,controls);widget(recognized);
      }else if(/^media\/[a-f0-9]{64}\.(?:png|jpg|gif|webp|avif|mp4|webm|ogv|mov|m4v)$/.test(asset.path||'')){
        const item=asset.kind==='image'?el('img'):player(root+asset.path);
        if(asset.kind==='image'){item.src=root+asset.path;item.alt=slot.querySelector('a')?.textContent||'Изображение';}
        slot.insertBefore(item,controls);
      }else{throw Error('Некорректный путь сохранённого материала');}
      inserted=true;button.hidden=true;cancel.hidden=true;auth?.remove();
    }
    function login(){
      if(auth)return;auth=el('form','material-login');
      const label=el('label','','Пароль ЖЖ '),password=el('input','input');password.type='password';password.autocomplete='current-password';password.required=true;password.maxLength=1024;label.append(password);
      const submit=el('button','btn','Повторить');auth.append(label,submit);controls.append(auth);
      auth.onsubmit=event=>{event.preventDefault();const value=password.value;password.value='';auth.remove();auth=null;start(value);};password.focus();
    }
    function show(state){
      showAsset(state.asset);
      let text=state.message||'Загрузка…';
      if(state.phase==='waiting'&&state.retry_at){const left=Math.max(0,Math.ceil(state.retry_at-Date.now()/1000));text=`Повтор через ${left} с`;}
      else if(state.total>0)text+=` · ${Math.min(100,Math.floor(100*(state.completed||0)/state.total))}%`;
      if(state.requests_used)text+=` · ${state.requests_used}${state.request_limit?' / '+state.request_limit:''} запросов`;
      status.textContent=text;
      const done=['complete','error','needs_auth','cancelled'].includes(state.phase);
      button.disabled=!done;cancel.hidden=done||inserted;
      if(state.phase==='complete'){controls.remove();}
      else if(state.phase==='needs_auth'){button.hidden=true;login();}
      else if(done&&!inserted){button.textContent='Повторить';button.hidden=false;}
      return done;
    }
    async function poll(){
      try{const done=show(await api('/api/material/'+taskId));if(!done)setTimeout(poll,1000);}
      catch(error){status.textContent=error.message;button.disabled=false;button.textContent='Повторить';cancel.hidden=true;}
    }
    async function start(password){
      button.disabled=true;status.textContent='В очереди…';
      const payload={archive:match[1],entry_id:eid,kind,value};if(password)payload.password=password;
      try{const state=await api('/api/material',payload);taskId=state.id;if(!show(state))setTimeout(poll,600);}
      catch(error){status.textContent=error.message;button.disabled=false;}
      finally{payload.password='';}
    }
    button.onclick=()=>start();
    cancel.onclick=async()=>{if(taskId){try{show(await api('/api/material/cancel',{id:taskId}));}catch(error){status.textContent=error.message;}}};
  }
  function scan(){
    for(const a of document.querySelectorAll('a[data-video-url]')){
      if(seen.has(a))continue;seen.add(a);
      const path=a.dataset.videoPath;
      if(path&&/^(?:\.\.\/)?media\/[a-f0-9]{64}\.(?:mp4|webm|ogv|mov|m4v)$/.test(path)){a.after(player(path));}
      else if(onlineApp){const slot=el('span','material-slot');slot.dataset.materialKind='video';slot.dataset.materialValue=a.dataset.videoUrl;a.after(slot);setup(slot);}
    }
    document.querySelectorAll('.material-slot').forEach(setup);
    document.querySelectorAll('.external-widget').forEach(widget);
  }
  let scheduled=false;
  new MutationObserver(()=>{if(!scheduled){scheduled=true;requestAnimationFrame(()=>{scheduled=false;scan();});}}).observe(document.body,{childList:true,subtree:true});
  scan();
});
