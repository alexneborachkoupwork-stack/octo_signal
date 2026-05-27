// Injected at document_start in MAIN world — runs before any page JS.
// Suppresses navigator.webdriver and removes CDP/automation artifacts
// that BotDetectorLib (bd.js) checks at weight 1.0.
// Do NOT patch window.alert or Function.prototype.toString — bd.js checks these for native code.

(function () {
  // Suppress webdriver flag (BotDetectorLib "webdriver" signal, weight 1.0).
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (_) {}

  // Remove known CDP / Puppeteer / automation bindings (weight 1.0 each).
  var _artifacts = [
    '__puppeteer', '__puppeteer_evaluation_script__',
    '__cdpSession__', '__pw_date_intercepted', '__pw_geolocation__',
    '__pw_permissions__', '__playwright', '__playwright__binding__',
    '_selenium', '__selenium_evaluate', 'callSelenium', 'webdriverCallback',
    'domAutomation', 'domAutomationController',
    '$wdc_', '__nightmare', 'callPhantom', '_phantom', 'phantom',
    '__phantomas', '__casper', 'casper', 'slimer',
  ];
  _artifacts.forEach(function (k) {
    try { if (k in window) delete window[k]; } catch (_) {}
  });

  // Remove all cdc_* and $cdc_* prefixed properties (Puppeteer CDP traces).
  try {
    Object.getOwnPropertyNames(window)
      .filter(function (k) { return k.startsWith('cdc_') || k.startsWith('$cdc_'); })
      .forEach(function (k) { try { delete window[k]; } catch (_) {} });
  } catch (_) {}
})();
