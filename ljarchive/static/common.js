"use strict";
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

document.addEventListener('DOMContentLoaded', () => {
  icons();
  const popups='.more[open],.reaction-detail[open]';
  document.addEventListener('click',event=>document.querySelectorAll(popups).forEach(menu=>{if(!menu.contains(event.target))menu.open=false;}));
  document.addEventListener('toggle',event=>{if(event.target.matches?.(popups))document.querySelectorAll(popups).forEach(menu=>{if(menu!==event.target)menu.open=false;});},true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelectorAll(popups).forEach(menu=>{menu.open=false;menu.querySelector('summary').focus();});});
});
