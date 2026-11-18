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
      const panel = tile.querySelector('.photo-panel');
      for (const property of ['width', 'left', 'top']) panel.style.removeProperty(property);
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
    panel.style.width = Math.min(document.documentElement.clientWidth - 16, Math.max(480, rect.width * 1.25)) + 'px';
    const place = (center, size, viewport) => Math.max(8, Math.min(viewport - size - 8, center - size / 2));
    panel.style.left = place(rect.left + rect.width / 2, panel.offsetWidth, document.documentElement.clientWidth) + 'px';
    panel.style.top = place(rect.top + rect.height / 2, panel.offsetHeight, innerHeight) + 'px';
  }

  const stage = document.querySelector('.slide-view');
  const frame = stage.querySelector('img');
  const source = stage.querySelector('.slide-source');
  const play = stage.querySelector('[data-slide="play"]');
  let slides = [], index = 0, playback;

  function orient(image) {
    if (!image.matches('.gallery-preview img') || !image.naturalHeight) return;
    const ratio = image.naturalWidth / image.naturalHeight;
    image.closest('.gallery-card').dataset.shape = ratio > 1.25 ? 'wide' : ratio < 0.8 ? 'tall' : 'square';
  }
  grid.addEventListener('load', event => {
    if (event.target instanceof HTMLImageElement) orient(event.target);
  }, true);
  grid.querySelectorAll('.gallery-preview img').forEach(orient);

  function stop() {
    clearInterval(playback);
    playback = null;
    play.textContent = 'Автопоказ';
    play.setAttribute('aria-pressed', 'false');
  }
  function show(position) {
    index = (position + slides.length) % slides.length;
    const image = slides[index];
    const card = image.closest('.gallery-card');
    const link = card.querySelector('.gallery-caption > a:not(.material-source)');
    frame.src = image.src;
    frame.alt = image.alt;
    source.href = link.href;
    source.textContent = link.textContent;
    stage.querySelector('.slide-meta').textContent = card.querySelector('.gallery-caption > .muted')?.textContent || '';
    stage.querySelector('.slide-count').textContent = (index + 1) + ' / ' + slides.length;
  }
  function startSlides(image) {
    close();
    stop();
    slides = Array.from(grid.querySelectorAll('.gallery-preview img'));
    show(slides.indexOf(image));
    stage.hidden = false;
    for (const button of stage.querySelectorAll('button:not([data-slide="close"])')) button.disabled = slides.length < 2;
    stage.focus({preventScroll:true});
    // Keep the header in view when opening from far down the gallery.
    const heading = document.querySelector('.gallery-heading');
    (stage.offsetHeight + stage.offsetTop - heading.offsetTop < innerHeight ? heading : stage).scrollIntoView({block:'start', behavior:'instant'});
  }
  function exitSlides() {
    stop();
    stage.hidden = true;
    const link = slides[index]?.closest('a');
    link?.focus({preventScroll:true});
    link?.scrollIntoView({block:'nearest'});
    slides = [];
  }
  stage.addEventListener('click', event => {
    const action = event.target.closest('button')?.dataset.slide;
    if (action === 'close') exitSlides();
    if (action === 'prev') show(index - 1);
    if (action === 'next') show(index + 1);
    if (action === 'play') {
      if (playback) stop();
      else {
        playback = setInterval(() => show(index + 1), 4000);
        play.textContent = 'Пауза';
        play.setAttribute('aria-pressed', 'true');
      }
    }
  });
  stage.addEventListener('keydown', event => {
    if (!['ArrowLeft','ArrowRight','Escape'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Escape') exitSlides();
    else show(index + (event.key === 'ArrowLeft' ? -1 : 1));
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });

  for (const tile of grid.querySelectorAll('.gallery-card')) {
    tile.addEventListener('pointerenter', event => {
      if ((!stage.hidden && stage.contains(document.activeElement)) || event.pointerType !== 'mouse' || pinned || active === tile) return;
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
      const image = tile.querySelector('.gallery-preview img');
      if (image) {
        startSlides(image);
        return;
      }
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
  window.addEventListener('scroll', () => { if (active) close(); }, {passive: true});
  window.addEventListener('resize', () => close());
})();
