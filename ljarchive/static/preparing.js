(() => {
  'use strict';
  const root = document.querySelector('[data-preparing]');
  if (!root) return;
  const stage = document.getElementById('preparing-stage'), progress = document.getElementById('preparing-progress');
  const count = document.getElementById('preparing-count'), retry = document.getElementById('preparing-retry');
  retry.onclick = () => location.reload();
  async function poll() {
    try {
      const response = await fetch('/api/preparation/' + encodeURIComponent(root.dataset.preparing), {cache: 'no-store'});
      if (!response.ok) throw new Error();
      const state = await response.json();
      if (state.phase === 'complete') {location.reload(); return;}
      window.OperationLog?.bind(stage,state);
      stage.textContent = state.message || 'Подготовка архива';
      if (state.phase === 'error') {progress.hidden = true; count.textContent = ''; retry.hidden = false; return;}
      if (state.total) {progress.max = state.total; progress.value = state.completed || 0; count.textContent = `${state.completed || 0} / ${state.total}`;}
      else {progress.removeAttribute('value'); count.textContent = '';}
      setTimeout(poll, 400);
    } catch (_) {
      stage.textContent = 'Приложение недоступно. Запустите его снова.';
      progress.hidden = true; retry.hidden = false;
    }
  }
  poll();
})();
