'use strict';

/**
 * ApplyWorkflow — resume from idle and complete the booking to PDF.
 * Sends 'apply' to the extension (uses stored idle state).
 */
function buildApplyPayload() {
  return {}; // no payload needed — extension reads idle state from storage
}

module.exports = { buildApplyPayload };
