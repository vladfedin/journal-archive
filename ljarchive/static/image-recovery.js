'use strict';
window.ArchiveImages=(()=>{
  const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const api=async(path,data)=>{const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});const v=await r.json();if(!r.ok)throw Error(v.error||'Ошибка запроса');return v;};
  let dialog,archive,task,confirmed=false,checkBrowser=false,timer;
  async function open(slug){
    if(!dialog){
      dialog=node('dialog');dialog.className='dialog image-recovery';
      const head=node('div');head.className='dialog-head';const close=node('button','Закрыть');close.className='btn text';close.onclick=()=>dialog.close();head.append(node('h2','Изображения'),close);
      const body=node('div');body.className='dialog-body';
      const run=node('button','Докачать через браузер');run.className='btn primary';run.dataset.action='download';
      const cancel=node('button','Остановить');cancel.className='btn';cancel.dataset.action='cancel';cancel.hidden=true;
      const status=node('p');status.dataset.role='status';status.setAttribute('role','status');
      const progress=node('progress');progress.hidden=true;
      const list=node('div');list.dataset.role='list';
      run.onclick=()=>start();cancel.onclick=async()=>{try{show(await api('/api/material/cancel',{id:task}));}catch(e){status.textContent=e.message;}};
      body.append(run,cancel,status,progress,list);dialog.append(head,body);document.body.append(dialog);
    }
    if(archive!==slug){clearTimeout(timer);task=null;confirmed=false;checkBrowser=false;archive=slug;show({phase:'idle',message:''});}
    dialog.showModal();await refresh();
  }
  async function refresh(){
    const slug=archive,list=dialog.querySelector('[data-role=list]');
    try{
      const data=await api('/api/image-failures/'+encodeURIComponent(slug));if(slug!==archive)return;
      if(data.task&&data.task.id!==task){task=data.task.id;clearTimeout(timer);if(!show(data.task))timer=setTimeout(poll,1000);}
      list.replaceChildren();
      if(!data.items.length)list.append(node('p','Все изображения сохранены.'));
      for(const item of data.items){
        const row=node('section');row.className='image-failure';
        const link=node('a',item.url);try{const u=new URL(item.url);if(['http:','https:'].includes(u.protocol))link.href=u.href;}catch{}link.target='_blank';link.rel='noopener noreferrer';row.append(link);
        const error=item.error||{},parts=[item.message];
        if(error.updated_at)parts.push(new Date(error.updated_at).toLocaleString('ru-RU'));
        if(error.requests!==undefined)parts.push('запросов: '+error.requests);
        row.append(node('p',parts.join(' · ')));
        for(const entry of item.entries||[]){const a=node('a',[entry.date?.slice(0,10),entry.title].filter(Boolean).join(' · '));a.href='/archive/'+encodeURIComponent(slug)+'/entries/'+entry.id+'.html';a.target='_blank';a.rel='noopener';a.className='image-post';row.append(a);}
        if(error.trace?.length){const details=node('details');details.append(node('summary','Запросы'));for(const t of error.trace)details.append(node('p',`${t.transport==='browser'?'Браузер':'HTTP'} · ${t.http_status||t.network_error||'ответ не получен'} · ${t.url}`));row.append(details);}
        list.append(row);
      }
      const button=dialog.querySelector('[data-action=download]');button.hidden=!data.items.some(i=>i.browser);
    }catch(e){list.replaceChildren(node('p',e.message));}
  }
  function show(state){
    const done=['idle','complete','error','needs_browser','cancelled'].includes(state.phase);
    const run=dialog.querySelector('[data-action=download]');run.disabled=!done;run.textContent=state.phase==='needs_browser'?'Продолжить':'Докачать через браузер';
    if(state.phase==='needs_browser'){confirmed=true;checkBrowser=false;}
    if(state.phase==='error'&&state.error?.session_type==='browser'){
      checkBrowser=[401,403].includes(state.error.http_status)||['browser_login','browser_redirect','browser_closed'].includes(state.error.reason);
      if(checkBrowser)run.textContent='Проверить вход';
    }
    const status=dialog.querySelector('[data-role=status]');status.textContent=state.message||'';
    if(!done&&state.total)status.textContent+=` · ${state.completed||0} / ${state.total}`;
    const event=state.last_activity;
    if(!done&&event?.request_url)status.append(node('span',event.request_url));
    dialog.querySelector('[data-action=cancel]').hidden=done;
    const progress=dialog.querySelector('progress');progress.hidden=done||!state.total;progress.max=state.total||1;progress.value=state.completed||0;
    return done;
  }
  async function poll(){
    try{const done=show(await api('/api/material/'+task));if(done)await refresh();else timer=setTimeout(poll,1000);}
    catch(e){show({phase:'error',message:e.message});}
  }
  async function start(){
    show({phase:'queued',message:'В очереди…'});
    try{const state=await api('/api/browser-images',{archive,confirmed,open_browser:checkBrowser});task=state.id;if(!show(state))timer=setTimeout(poll,300);}
    catch(e){show({phase:'error',message:e.message});}
  }
  return {open};
})();
