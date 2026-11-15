"use strict";
// Подключается в <head> без defer: запомненная тема применяется до первой отрисовки, без мигания.
(() => {
  const KEY = "ljarchive-theme", root = document.documentElement;
  try { const saved = localStorage.getItem(KEY); if (saved === "dark" || saved === "light") root.dataset.theme = saved; } catch {}
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const system = matchMedia("(prefers-color-scheme: dark)");
    const current = () => root.dataset.theme || (system.matches ? "dark" : "light");
    // Кнопка называет действие: в тёмной теме предлагает светлую, и наоборот.
    const paint = () => {
      const next = current() === "dark" ? "light" : "dark";
      button.dataset.next = next;
      button.querySelector("span").textContent = next === "dark" ? "Тёмная тема" : "Светлая тема";
      button.setAttribute("aria-label", next === "dark" ? "Включить тёмную тему" : "Включить светлую тему");
    };
    button.addEventListener("click", () => {
      root.dataset.theme = button.dataset.next;
      try { localStorage.setItem(KEY, root.dataset.theme); } catch {}
      paint();
    });
    system.addEventListener("change", paint);
    button.hidden = false;
    paint();
  });
})();
