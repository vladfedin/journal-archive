'use strict';
(() => {
  const key='ljarchive-theme',root=document.documentElement,system=matchMedia('(prefers-color-scheme: dark)');let saved;
  try{saved=localStorage.getItem(key);}catch(_){}
  root.dataset.theme=saved==='dark'||saved==='light'?saved:system.matches?'dark':'light';
  document.addEventListener('DOMContentLoaded',()=>{
    const button=document.getElementById('theme-toggle');if(!button)return;
    const paint=()=>{const dark=root.dataset.theme==='dark';button.innerHTML=svg(dark?'sun':'moon');button.setAttribute('aria-label',dark?'Включить светлую тему':'Включить тёмную тему');button.title=dark?'Светлая тема':'Тёмная тема';button.hidden=false;};
    button.onclick=()=>{root.dataset.theme=root.dataset.theme==='dark'?'light':'dark';saved=root.dataset.theme;try{localStorage.setItem(key,saved);}catch(_){}paint();};
    system.addEventListener('change',()=>{if(!saved){root.dataset.theme=system.matches?'dark':'light';paint();}});paint();
  });
})();
