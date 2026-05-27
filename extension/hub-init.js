/**
 * Runs on hub /worker-init?botId=… — stores hub URL + botId and connects Octo Signal to hub.
 */
(function () {
  const params = new URLSearchParams(window.location.search);
  const botId = params.get('botId') ?? params.get('profileId');

  function setStatus(text, color) {
    const el = document.getElementById('status');
    if (el) {
      el.textContent = text;
      if (color) el.style.color = color;
    }
  }

  if (!botId) {
    console.warn('[OctoSignal] worker-init: no botId in URL');
    setStatus('Missing botId in URL', '#f87171');
    return;
  }

  const hubUrl = window.location.origin.replace(/^http/, 'ws') + '/ext';
  const initKey = `octo-signal-init:${botId}`;

  setStatus(`Connecting bot ${botId}…`, '#f0c040');

  chrome.storage.local.set({ botId, hubUrl, hubWsUrl: hubUrl }, () => {
    const alreadySent = sessionStorage.getItem(initKey) === '1';
    if (!alreadySent) {
      sessionStorage.setItem(initKey, '1');
      chrome.runtime.sendMessage({ type: 'WORKER_INIT', botId, hubUrl }, () => {
        const err = chrome.runtime.lastError;
        if (err) {
          console.error('[OctoSignal] WORKER_INIT failed:', err.message);
          setStatus(`Extension error: ${err.message}`, '#f87171');
          return;
        }
        setStatus(`Octo Signal connected (bot ${botId})`, '#34d399');
      });
    } else {
      chrome.runtime.sendMessage({ type: 'WORKER_INIT', botId, hubUrl }, () => void chrome.runtime.lastError);
      setStatus(`Octo Signal active (bot ${botId})`, '#34d399');
    }
  });
})();
