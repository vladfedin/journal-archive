'use strict';

(() => {
  const grid = document.querySelector('[data-photos]');
  if (!grid) return;
  grid.classList.add('photo-grid');
  let active = null;
  let timer;
  let pinned = false;

  function close(restoreFocus = false) {
    clearTimeout(timer);
    if (active) {
      const tile = active;
      active = null;
      tile.classList.remove('photo-open');
      if (restoreFocus) tile.querySelector('.gallery-preview a')?.focus({preventScroll: true});
    }
    pinned = false;
  }

  function open(tile, pin) {
    close();
    active = tile;
    pinned = pin;
    const rect = tile.getBoundingClientRect();
    tile.classList.add('photo-open');
    const panel = tile.querySelector('.photo-panel');
    const place = (center, size, viewport) => Math.max(8, Math.min(viewport - size - 8, center - size / 2));
    panel.style.left = place(rect.left + rect.width / 2, panel.offsetWidth, document.documentElement.clientWidth) + 'px';
    panel.style.top = place(rect.top + rect.height / 2, panel.offsetHeight, innerHeight) + 'px';
  }

  for (const tile of grid.querySelectorAll('.gallery-card')) {
    tile.addEventListener('pointerenter', event => {
      if (event.pointerType !== 'mouse' || pinned || active === tile) return;
      clearTimeout(timer);
      timer = setTimeout(() => open(tile, false), 350);
    });
    tile.addEventListener('pointerleave', () => {
      clearTimeout(timer);
      if (active === tile && !pinned && !tile.contains(document.activeElement)) close();
    });
    tile.querySelector('.photo-close').addEventListener('click', () => close(true));
    tile.querySelector('.gallery-preview').addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      open(tile, true);
      tile.querySelector('.photo-close').focus({preventScroll: true});
    });
    tile.addEventListener('focusout', event => {
      if (active === tile && !tile.contains(event.relatedTarget)) close();
    });
  }

  document.addEventListener('pointerdown', event => {
    if (active && !active.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) close(active.contains(document.activeElement));
  });
  window.addEventListener('scroll', () => close(), {passive: true});
  window.addEventListener('resize', () => close());
})();
