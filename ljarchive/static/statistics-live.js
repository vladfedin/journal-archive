(() => {
  'use strict';
  const status = document.getElementById('stats-live');
  if (!status) return;
  const discussions = !!window.ARCHIVE_DISCUSSIONS;
  let stamp = (window.ARCHIVE_STATS || window.ARCHIVE_DISCUSSIONS)?.generated_at, version = '';
  const time = () => stamp ? 'Снимок ' + new Date(stamp).toLocaleString('ru-RU') : 'Время снимка неизвестно';
  status.textContent = time();
  const match = /^\/archive\/([a-z0-9_-]+)\//.exec(location.pathname);
  if (!match || !['127.0.0.1','localhost'].includes(location.hostname) || match[1] === '_demo') return;
  async function refresh() {
    try {
      const response = await fetch('/api/statistics/' + match[1] + (discussions ? '/discussions' : '') + '?version=' + encodeURIComponent(version), {cache:'no-store'});
      if (!response.ok) throw new Error();
      const state = await response.json();
      if (state.data) {
        version = state.version; stamp = state.data.generated_at;
        const footer = document.querySelector('.footer > span');
        if (footer && stamp) footer.textContent = footer.textContent.split(' · ')[0] + ' · ' + time();
        window.dispatchEvent(new CustomEvent('archive-statistics', {detail:state.data}));
      }
      status.textContent = (stamp ? time() + ' · ' : '') +
        (state.phase === 'error' ? 'Пересчёт не удался, повторим' : state.phase === 'updating' ? 'Пересчёт…' : state.active ? 'Загрузка продолжается' : 'Данные обновлены');
    } catch (_) {
      status.textContent = time() + ' · Нет связи с приложением';
    } finally {
      setTimeout(refresh, document.hidden ? 15000 : 3000);
    }
  }
  refresh();
})();
