(() => {
  'use strict';
  const states = new WeakMap();
  const finished = new Set(['complete','error','partial','cancelled','needs_auth','needs_browser','demo','ready','available','current']);
  const labels = {request:'Запрос',response:'Ответ',success:'Получено',failure:'Ошибка',wait:'Пауза',redirect:'Перенаправление',step:'Этап'};
  const reasons = {timeout:'тайм-аут',connection:'нет соединения',transfer:'обрыв передачи',tls:'ошибка TLS',
    api_limit:'ограничение API',incomplete:'неполный ответ',web_login:'нужен вход',redirects:'перенаправление'};
  const clock = new Intl.DateTimeFormat('ru-RU', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  let dialog, heading, summary, list, active, timer, lastEvents;
  const seconds = n => Math.max(0, Math.floor(n)) + ' с';
  function create() {
    dialog = document.createElement('dialog');
    dialog.className = 'dialog operation-log';
    dialog.setAttribute('aria-labelledby','operation-log-title');
    heading = document.createElement('h2');
    heading.id = 'operation-log-title';
    heading.textContent = 'Журнал операции';
    const head = document.createElement('div');
    head.className = 'dialog-head';
    const close = document.createElement('button');
    close.type = 'button'; close.className = 'btn'; close.textContent = 'Закрыть';
    close.onclick = () => dialog.close();
    head.append(heading,close);
    summary = document.createElement('p');
    list = document.createElement('ol');
    dialog.append(head,summary,list); document.body.append(dialog);
    dialog.onclose = () => {clearInterval(timer); active=null; lastEvents=null;};
  }
  function draw() {
    const binding = states.get(active);
    if (!binding) return;
    const {state,received} = binding;
    const now = (state.server_time || received) + Date.now()/1000 - received;
    const events = state.events || [], last = events.at(-1);
    const text = [state.message || active.textContent];
    if (last && !finished.has(state.phase)) {
      if (last.kind === 'request') text.push('Ожидаем ответ ' + seconds(now-last.at));
      if (last.kind === 'response') text.push('Читаем ответ ' + seconds(now-last.at));
      if (last.kind === 'wait' && last.retry_at > now) text.push('До повтора ' + seconds(last.retry_at-now));
    }
    if (state.started_at) {
      const end = state.ended_at || (finished.has(state.phase) ? state.last_progress_at : now);
      if (end) text.push('С начала ' + seconds(end-state.started_at));
    }
    if (state.total > 0) text.push((state.completed || 0) + ' / ' + state.total);
    if (state.requests_used !== undefined) text.push('Запросов: ' + state.requests_used);
    summary.textContent = text.filter(Boolean).join(' · ');
    const key = JSON.stringify(events);
    if (key === lastEvents) return;
    lastEvents = key;
    let requestAt;
    list.replaceChildren(...events.map(event => {
      const row = document.createElement('li');
      const at = event.at;
      const parts = [at && clock.format(at*1000), labels[event.kind] || event.kind,
        event.method, event.request_url || event.host, event.rpc_method, event.message];
      if (event.attempt) parts.push('попытка ' + event.attempt + (event.max_attempts ? ' из '+event.max_attempts : ''));
      if (event.rpc_code != null) parts.push('XML-RPC '+event.rpc_code);
      if (event.http_status) parts.push('HTTP '+event.http_status);
      if (event.bytes !== undefined) parts.push(event.bytes.toLocaleString('ru-RU')+' Б');
      if (event.reason && event.reason !== 'http') parts.push(reasons[event.reason] || event.reason);
      if (event.kind === 'request') requestAt=at;
      if (requestAt && ['success','failure','response'].includes(event.kind)) parts.push(seconds(at-requestAt));
      if (event.kind === 'wait' && event.retry_at) parts.push('до '+clock.format(event.retry_at*1000));
      row.textContent = parts.filter(Boolean).join(' · ');
      return row;
    }));
  }
  function bind(node,state) {
    const button = node.closest('button[data-operation]') || node;
    if (!states.has(button)) {
      button.classList.add('operation-trigger');
      button.setAttribute('aria-haspopup','dialog');
      if (button.tagName !== 'BUTTON') {
        if (button.getAttribute('role')==='status') button.setAttribute('aria-live','polite');
        button.tabIndex = 0; button.setAttribute('role','button');
        button.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') {e.preventDefault();button.click();}
        });
      }
      button.addEventListener('click', () => {
        if (!dialog) create();
        active=button; lastEvents=null; draw(); dialog.showModal(); timer=setInterval(draw,500);
      });
    }
    states.set(button,{state,received:Date.now()/1000});
    if (active===button && dialog?.open) draw();
  }
  // The local server request has its own history, without URL parameters or submitted data.
  async function fetchLocal(node, address, options) {
    const started = Date.now()/1000;
    const event = {kind:'request', at:started, method:options?.method || 'GET',
      request_url:new URL(address,location.href).pathname};
    const state = {phase:'loading', started_at:started, events:[event]};
    bind(node,state);
    try {
      const response = await fetch(address,options);
      state.events.push({kind:'response',at:Date.now()/1000,http_status:response.status});
      state.phase=response.ok?'complete':'error';
      return response;
    } catch (error) {
      state.events.push({kind:'failure',at:Date.now()/1000,reason:'connection'});
      state.phase='error';
      throw error;
    } finally {
      state.ended_at=Date.now()/1000;
      if (states.get(node)?.state===state) bind(node,state);
    }
  }
  window.OperationLog = {bind,fetchLocal};
})();
