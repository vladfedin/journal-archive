'use strict';
document.addEventListener('DOMContentLoaded',()=>{
 const action=document.getElementById('profile-action'),match=location.pathname.match(/^\/archive\/([a-z0-9_-]+)\//);
 if(!action||!match||document.body.dataset.libraryUrl!=='/'||location.protocol!=='http:')return;
 action.hidden=false;
 const button=document.getElementById('profile-load'),form=document.getElementById('profile-form'),password=document.getElementById('profile-password'),status=document.getElementById('profile-status'),cancel=document.getElementById('profile-cancel');let task=null;
 const api=async(path,data)=>{const r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});const value=await r.json();if(!r.ok)throw Error(value.error||'Не удалось загрузить профиль');return value;};
 button.onclick=()=>{form.hidden=false;password.focus();};
 function show(state){status.textContent=state.message||'Загрузка…';if(state.requests_used)status.textContent+=` · ${state.requests_used} запросов`;const ended=['complete','error','needs_auth','cancelled'].includes(state.phase);button.disabled=!ended;cancel.hidden=ended;if(state.phase==='complete')location.reload();if(state.phase==='needs_auth'){form.hidden=false;password.focus();}return ended;}
 async function poll(){try{if(!show(await api('/api/material/'+task)))setTimeout(poll,1000);}catch(e){status.textContent=e.message;button.disabled=false;cancel.hidden=true;}}
 form.onsubmit=async event=>{event.preventDefault();const value=password.value;password.value='';form.hidden=true;button.disabled=true;status.textContent='В очереди…';try{const state=await api('/api/profile',{archive:match[1],password:value});task=state.id;if(!show(state))setTimeout(poll,500);}catch(e){status.textContent=e.message;button.disabled=false;}};
 cancel.onclick=async()=>{try{show(await api('/api/material/cancel',{id:task}));}catch(e){status.textContent=e.message;}};
});
