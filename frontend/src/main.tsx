import React from 'react';
import ReactDOM from 'react-dom/client';
import { isTauri } from '@tauri-apps/api/core';
import { App } from './App';
import './styles/index.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Register PICO offline application shell service worker ONLY in browser mode;
// inside Tauri, embedded assets already ship inside the binary and SW caching breaks bundle upgrades.
if (typeof window !== 'undefined' && 'serviceWorker' in navigator && !navigator.userAgent.includes('jsdom')) {
  window.addEventListener('load', () => {
    if (isTauri()) {
      navigator.serviceWorker
        .getRegistrations()
        .then((regs) => Promise.all(regs.map((r) => r.unregister())))
        .catch(() => {});
      if ('caches' in window) {
        caches
          .keys()
          .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
          .catch(() => {});
      }
      return;
    }

    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Service worker registration error is non-fatal
    });
  });
}
