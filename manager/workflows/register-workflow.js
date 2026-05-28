'use strict';

/**
 * RegisterWorkflow — register an account, then optionally warm up to idle.
 *
 * The session sends 'register' to the extension. On 'register-done',
 * optionally sends 'warmup' to bring the session to an idle state.
 *
 * For simplicity this workflow is expressed as a payload factory.
 * The Session class handles the single command; multi-step chains (register → warmup)
 * are handled by overriding onMessage inside a subclass if needed.
 */
function buildRegisterPayload({ realPerson = null, emailProvider = 'mailtm', captchaSolver = 'capsolver' } = {}) {
  return {
    realPerson,
    emailProvider,
    captchaSolver,
  };
}

module.exports = { buildRegisterPayload };
