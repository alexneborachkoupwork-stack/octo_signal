'use strict';

/**
 * IdleWorkflow — log in and idle at a chosen step (login | form | schedule).
 * Sends 'warmup' to the extension.
 */
function buildWarmupPayload({
  username,
  password,
  idleStep     = 'login',
  arrivalDate  = null,
  consulPost   = null,
  realPerson   = null,
  captchaSolver = 'capsolver',
} = {}) {
  return {
    username,
    password,
    idleStep,
    arrivalDate,
    consulPost,
    realPerson,
    captchaSolver,
  };
}

module.exports = { buildWarmupPayload };
