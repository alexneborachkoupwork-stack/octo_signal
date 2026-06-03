// Injected at document_start in MAIN world — runs before any page JS.
// Removes CDP/automation artifacts that BotDetectorLib (bd.js) checks at weight 1.0.
// navigator.webdriver is NOT patched here — Octo Browser sets it natively at the
// browser level without a detectable JS override; patching it in JS would add a
// detectable own-property (hasOwnProperty true) and a non-native getter toString.
// Do NOT patch window.alert or Function.prototype.toString — bd.js checks native code.

(function () {
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

// ── WebGL renderer normalisation ─────────────────────────────────────────────
// Some WebGL contexts created by reCAPTCHA's fingerprinting code fall back to
// SwiftShader even when hardware GL is available — "Google SwiftShader" is an
// unambiguous bot signal.  We probe a fresh canvas here (before any page JS)
// to read the real Intel hardware renderer, then patch getParameter on all
// WebGL context prototypes so every canvas — including reCAPTCHA's — reports
// the same hardware string.  getExtension is patched to always return the
// WEBGL_debug_renderer_info object so the renderer constants are reachable
// even on contexts that normally hide it.
(function () {
  var _VENDOR   = 0x9245;  // UNMASKED_VENDOR_WEBGL
  var _RENDERER = 0x9246;  // UNMASKED_RENDERER_WEBGL

  // Read the real hardware renderer before any page script can interfere.
  // Falls back to a plausible Intel string if the probe fails.
  var _vendor   = 'Intel Inc.';
  var _renderer = 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
  try {
    var _c  = document.createElement('canvas');
    var _gl = _c.getContext('webgl') || _c.getContext('experimental-webgl');
    if (_gl) {
      var _ext = _gl.getExtension('WEBGL_debug_renderer_info');
      if (_ext) {
        var _v = _gl.getParameter(_ext.UNMASKED_VENDOR_WEBGL);
        var _r = _gl.getParameter(_ext.UNMASKED_RENDERER_WEBGL);
        // Only use if hardware — if even this canvas is SwiftShader keep fallback.
        if (_v && _r && _r.indexOf('SwiftShader') === -1) {
          _vendor   = _v;
          _renderer = _r;
        }
      }
    }
    _gl = null; _c = null;
  } catch (_) {}

  function _native(fn, name) {
    // Non-enumerable, non-configurable toString so Object.getOwnPropertyDescriptor(fn,'toString')
    // returns undefined — matching real native functions (which inherit toString, not own it).
    try {
      Object.defineProperty(fn, 'toString', {
        value: function() { return 'function ' + name + '() { [native code] }'; },
        writable: false, enumerable: false, configurable: false,
      });
    } catch(_) {}
    try { Object.defineProperty(fn, 'name', {value: name, writable: false, enumerable: false, configurable: true}); } catch(_) {}
    return fn;
  }

  // Session-unique seed for pixel noise — different each page load so
  // canvas hashes vary across sessions and don't match the known SwiftShader hash.
  var _seed = (Math.random() * 0x7fffffff) | 0;
  function _prng() {
    _seed ^= _seed << 13; _seed ^= _seed >> 17; _seed ^= _seed << 5;
    return (_seed >>> 0) / 0x100000000;
  }

  function _patchCtx(Ctx) {
    if (!Ctx) return;
    var _gExt  = Ctx.prototype.getExtension;
    var _gParm = Ctx.prototype.getParameter;
    var _rPix  = Ctx.prototype.readPixels;
    Ctx.prototype.getExtension = _native(function getExtension(name) {
      if (name === 'WEBGL_debug_renderer_info')
        return _gExt.call(this, name) || {UNMASKED_VENDOR_WEBGL: _VENDOR, UNMASKED_RENDERER_WEBGL: _RENDERER};
      return _gExt.call(this, name);
    }, 'getExtension');
    Ctx.prototype.getParameter = _native(function getParameter(pname) {
      if (pname === _VENDOR)   return _vendor;
      if (pname === _RENDERER) return _renderer;
      return _gParm.call(this, pname);
    }, 'getParameter');
    // Inject ±1 LSB noise into readPixels output so the canvas hash differs from
    // the known deterministic SwiftShader fingerprint on every session.
    // The magnitude (1 step in the LSB of each channel) is invisible to human
    // inspection but breaks any hash-based bot detection keyed on SwiftShader output.
    Ctx.prototype.readPixels = _native(function readPixels(x, y, w, h, fmt, type, buf) {
      _rPix.call(this, x, y, w, h, fmt, type, buf);
      if (buf instanceof Uint8Array || buf instanceof Uint8ClampedArray) {
        for (var i = 0; i < buf.length; i++) {
          if ((i & 3) !== 3) { // skip alpha channel
            var v = buf[i] + ((_prng() < 0.5) ? 1 : -1);
            buf[i] = v < 0 ? 0 : v > 255 ? 255 : v;
          }
        }
      }
    }, 'readPixels');
  }

  try { _patchCtx(window.WebGLRenderingContext);  } catch (_) {}
  try { _patchCtx(window.WebGL2RenderingContext); } catch (_) {}
})();

// navigator.language / navigator.languages — NOT patched here.
// Patching to 'pt-PT' while the browser profile's real Accept-Language header differs
// creates a detectable inconsistency: reCAPTCHA Enterprise compares the JS value against
// the Accept-Language it observes on its own requests to google.com.  Matching the real
// profile value keeps the signals consistent; set the profile's language to pt-PT at the
// Octo Browser level to eliminate the Estonian-profile mismatch at the source.

// ── Page Visibility API ───────────────────────────────────────────────────────
// reCAPTCHA Enterprise monitors document.visibilityState and document.hidden as
// behavioral signals. Background tabs, minimised windows, or OS-level window
// occlusion return "hidden"/true — indistinguishable from a bot running off-screen.
// Patching Document.prototype (not the document instance) keeps
// Object.getOwnPropertyDescriptor(document, 'visibilityState') === undefined,
// matching a real unpatched browser.
(function () {
  function _native(fn, name) {
    try {
      Object.defineProperty(fn, 'toString', {
        value: function() { return 'function get ' + name + '() { [native code] }'; },
        writable: false, enumerable: false, configurable: false,
      });
    } catch(_) {}
    try { Object.defineProperty(fn, 'name', {value: name, writable: false, enumerable: false, configurable: true}); } catch(_) {}
    return fn;
  }
  try {
    Object.defineProperty(Document.prototype, 'visibilityState', {
      get: _native(function visibilityState() { return 'visible'; }, 'visibilityState'),
      configurable: true,
    });
    Object.defineProperty(Document.prototype, 'hidden', {
      get: _native(function hidden() { return false; }, 'hidden'),
      configurable: true,
    });
  } catch (_) {}
})();

