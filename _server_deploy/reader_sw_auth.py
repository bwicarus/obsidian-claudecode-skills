"""Root/PDF Service Worker shared authentication-fence JavaScript.

This is the single source for the origin-wide auth epoch protocol.  Both
``app.py`` and ``pdf_reader.py`` splice the exact snippet into their generated
service workers; keep auth-transition behavior here rather than copying it
between the two scopes.
"""

READER_SW_AUTH_PLACEHOLDER = "__BW_READER_SW_AUTH__"

READER_SW_AUTH_JS = r"""
const AUTH_STATE_CACHE = 'reader-auth-state-v1';
const AUTH_EPOCH_PATH = '/_bw/reader-auth/epoch';
const AUTH_PENDING_PATH = '/_bw/reader-auth/pending/';
const AUTH_PENDING_MAX_AGE_MS = 10 * 60 * 1000;
const AUTH_EPOCH_RE = /^auth-v1-[a-f0-9]{64}$/;
function _validAuthEpoch(value) {
  return AUTH_EPOCH_RE.test(String(value || '').trim());
}
function _newAuthEpoch() {
  const bytes = new Uint8Array(32);
  self.crypto.getRandomValues(bytes);
  return 'auth-v1-' + Array.from(bytes, (value) =>
    Number(value).toString(16).padStart(2, '0')
  ).join('');
}
function _authEpochKey() {
  return self.location.origin + AUTH_EPOCH_PATH;
}
function _authPendingPrefix() {
  return self.location.origin + AUTH_PENDING_PATH;
}
function _authPendingKey(token) {
  return _authPendingPrefix() + encodeURIComponent(String(token || ''));
}
async function _hasLiveAuthPending(cache) {
  const now = Date.now();
  const pendingPrefix = _authPendingPrefix();
  let pending = false;
  const keys = await cache.keys();
  for (const key of keys) {
    if (!String(key.url || '').startsWith(pendingPrefix)) continue;
    try {
      const stored = await cache.match(key);
      const createdAt = stored ? Number(await stored.text()) : 0;
      if (createdAt > 0 && now - createdAt <= AUTH_PENDING_MAX_AGE_MS) {
        pending = true;
      } else {
        await cache.delete(key);
      }
    } catch (_) {
      pending = true;
    }
  }
  return pending;
}
async function _readAuthState() {
  const cache = await caches.open(AUTH_STATE_CACHE);
  // Scan both sides of the epoch read.  This closes the important interleave
  // where another SW publishes pending after the first keys() snapshot but
  // before replacing the epoch.
  let pending = await _hasLiveAuthPending(cache);
  let response = await cache.match(_authEpochKey());
  let epoch = response ? String(await response.text()).trim() : '';
  pending = (await _hasLiveAuthPending(cache)) || pending;
  // First install: create a ready epoch only when no auth transition is active.
  // Legacy identity records contain no epoch and therefore fail closed.
  if (!_validAuthEpoch(epoch) && !pending) {
    const candidate = _newAuthEpoch();
    await cache.put(_authEpochKey(), new Response(candidate, {
      headers: { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }
    }));
    response = await cache.match(_authEpochKey());
    epoch = response ? String(await response.text()).trim() : '';
    pending = await _hasLiveAuthPending(cache);
  }
  return {
    epoch: _validAuthEpoch(epoch) ? epoch : '',
    pending: pending
  };
}
async function _beginAuthTransition() {
  const cache = await caches.open(AUTH_STATE_CACHE);
  const token = _newAuthEpoch();
  const pendingKey = _authPendingKey(token);
  // Pending is published first.  Concurrent transitions use distinct keys, so
  // one completion cannot unblock another transition that is still running.
  let pendingWritten = false;
  try {
    await cache.put(pendingKey, new Response(String(Date.now()), {
      headers: { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }
    }));
    pendingWritten = true;
    await cache.put(_authEpochKey(), new Response(token, {
      headers: { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }
    }));
    return token;
  } catch (error) {
    // A failed epoch write must not leave this transition blocking the origin
    // until the stale-pending TTL expires.
    if (pendingWritten) {
      try { await cache.delete(pendingKey); } catch (_) {}
    }
    throw error;
  }
}
async function _finishAuthTransition(token) {
  try {
    const cache = await caches.open(AUTH_STATE_CACHE);
    await cache.delete(_authPendingKey(token));
  } catch (_) {}
}
function _isAuthTransitionRequest(url, request) {
  const path = String(url && url.pathname || '');
  const method = String(request && request.method || 'GET').toUpperCase();
  let bearer = false;
  try {
    bearer = /^Bearer\s+\S+/i.test(String(request.headers.get('Authorization') || ''));
  } catch (_) {}
  return path === '/login' || path === '/logout' ||
    (path === '/register' && method === 'POST') || bearer;
}
"""
