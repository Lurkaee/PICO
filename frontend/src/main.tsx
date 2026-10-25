import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles/index.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Register PICO offline application shell service worker
if (typeof window !== 'undefined' && 'serviceWorker' in navigator && !navigator.userAgent.includes('jsdom')) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Service worker registration error is non-fatal
    });
  });
}
