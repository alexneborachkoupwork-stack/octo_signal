/**
 * Runs on hub /worker-init?botId=… — stores hub URL + botId and connects Octo Probe to hub.
 */
(async function () {
  const params = new URLSearchParams(window.location.search);
  const botId = params.get('botId') ?? params.get('profileId');

  function setStatus(text, color) {
    const el = document.getElementById('status');
    if (el) {
      el.textContent = text;
      if (color) el.style.color = color;
    }
  }

  function sendWorkerInit(hubUrl, attempt) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'WORKER_INIT', botId, hubUrl }, () => {
        const err = chrome.runtime.lastError;
        if (err && attempt < 5) {
          setTimeout(() => resolve(sendWorkerInit(hubUrl, attempt + 1)), 400 * attempt);
          return;
        }
        resolve(err ? err.message : null);
      });
    });
  }

  if (!botId) {
    console.warn('[OctoProbe] worker-init: no botId in URL');
    setStatus('Missing botId in URL', '#f87171');
    return;
  }

  const hubUrl = window.location.origin.replace(/^http/, 'ws') + '/ext';

  setStatus(`Connecting bot ${botId}…`, '#f0c040');

  await skSet({ [SK.BOT_ID]: botId, [SK.HUB_URL]: hubUrl, [SK.HUB_WS_URL]: hubUrl });
  const errMsg = await sendWorkerInit(hubUrl, 1);
  if (errMsg) {
    console.error('[OctoProbe] WORKER_INIT failed:', errMsg);
    setStatus(`Extension error: ${errMsg}`, '#f87171');
    return;
  }
  setStatus(`Octo Probe connected (bot ${botId})`, '#34d399');
})();
