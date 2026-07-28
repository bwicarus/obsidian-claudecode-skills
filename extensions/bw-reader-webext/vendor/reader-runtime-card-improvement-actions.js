/* card-improvement-actions.js
 * Shared transport contract between review surfaces and the single card
 * improvement workspace.  This module only carries identity/mode metadata and
 * opens the workspace; prompts and Anki/note writes belong to the backend.
 */
(function () {
  'use strict';

  var RC = (window.RC = window.RC || {});
  if (RC.cardImprovementActions) return;

  var CONTRACT = 'card-improvement-action/1';
  var MODES = ['verbose', 'concise'];

  function normalizeMode(value) {
    return value === 'concise' ? 'concise' : 'verbose';
  }

  function normalize(card) {
    card = card || {};
    var raw = card.improvement_action || {};
    var workspaceUrl = String(raw.workspace_url || card.improve_url || '');
    if (!workspaceUrl) return null;
    return {
      contract: CONTRACT,
      delivery: 'workspace',
      workspace_url: workspaceUrl,
      entity_id: String(raw.entity_id || card.entity_id || ''),
      entity_index: raw.entity_index != null ? raw.entity_index : card.entity_index,
      source_ref: String(raw.source_ref || card.source_ref || ''),
      modes: MODES.slice(),
      default_mode: normalizeMode(raw.default_mode)
    };
  }

  function workspaceUrl(card, mode) {
    var action = normalize(card);
    if (!action) return '';
    var url;
    try { url = new URL(action.workspace_url, window.location.href); }
    catch (e) { return ''; }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';

    // The old page and the review drawer deliberately share this one identity
    // shape.  No client-side prompt or mutation payload is constructed here.
    if (action.entity_id) url.searchParams.set('card', action.entity_id);
    if (action.entity_index !== null && action.entity_index !== undefined && action.entity_index !== '') {
      url.searchParams.set('index', String(action.entity_index));
    }
    if (action.source_ref) url.searchParams.set('source', action.source_ref);
    url.searchParams.set('verbosity', normalizeMode(mode || action.default_mode));
    url.searchParams.set('entry', 'review');
    return url.toString();
  }

  function openWorkspace(card, mode) {
    var url = workspaceUrl(card, mode);
    if (!url) return false;
    // Keep the navigation inside the user click stack.  iPad Safari otherwise
    // treats the delayed window as a popup and silently blocks it.
    try {
      var target = window.open('', '_blank');
      if (target) {
        try { target.opener = null; } catch (ignore) {}
        target.location = url;
      } else {
        window.location.href = url;
      }
      return true;
    } catch (e) {
      window.location.href = url;
      return true;
    }
  }

  RC.cardImprovementActions = {
    contract: CONTRACT,
    modes: MODES.slice(),
    normalize: normalize,
    normalizeMode: normalizeMode,
    workspaceUrl: workspaceUrl,
    openWorkspace: openWorkspace
  };
})();
