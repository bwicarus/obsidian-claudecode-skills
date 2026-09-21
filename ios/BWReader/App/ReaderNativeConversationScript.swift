import Foundation

/// A read projection of the existing Reader conversation, not a second session
/// or persistence layer. Commands only call registered Reader entry points.
enum ReaderNativeConversationScript {
    static let source = #"""
    (() => {
      'use strict';
      if (window !== window.top || window.__bwNativeConversation) return;
      const handler = window.webkit?.messageHandlers?.bwNativeConversation;
      if (!handler || typeof handler.postMessage !== 'function') return;
      let legacyVisible = false, nativeMode = false;
      let thread = null, threadObserver = null, timer = null, revision = 0;
      let scope = '', scopeKey = '', lastSignature = '', accountSubscription = null;
      let actions = new Map(), nodeIDs = new WeakMap(), previousNodes = [], excludedNodes = new WeakSet();
      let controls = null, controlsObserver = null, suspended = false;
      let drawerElement = null, drawerObserver = null, contextObserver = null, contextElement = null, toolbarObserver = null, toolbarElement = null;
      const navigationID = String(Date.now()) + '-' + Math.random().toString(36).slice(2);
      const hooked = new WeakMap();
      const text = (value, limit = 32000) => typeof value === 'string' ? value.slice(0, limit) : '';
      const hash = value => {
        let a = 2166136261, b = 2246822507;
        for (const char of String(value)) { a = Math.imul(a ^ char.charCodeAt(0), 16777619); b = Math.imul(b ^ char.charCodeAt(0), 3266489909); }
        return (a >>> 0).toString(36) + (b >>> 0).toString(36);
      };
      const rc = () => window.RC || {};
      const pane = () => document.getElementById('side-pane-asst');
      const conversationMode = () => pane()?.dataset.assistantMode === 'review' ? 'review' : 'normal';
      const drawer = () => rc().sidedrawer;
      const account = () => window.BWReaderRuntime?.accountContext;
      const activeTab = () => document.querySelector('#ep-side-tabs .ep-side-tab.active[data-pane],#ep-side .side-tab.active[data-pane],#side-tabs .side-tab.active[data-pane]')?.dataset.pane || 'asst';
      const isOpen = () => { try { return !!drawer()?.isOpen(); } catch (_) { return false; } };
      const isReady = () => !!thread && typeof window.__asstSend === 'function' && !!rc().turnCard;
      const isBusy = () => { try { return !!window.__asstBusy?.(); } catch (_) { return !!window.__asstStreaming; } };
      function cleanText(node, limit = 32000) {
        if (!node) return '';
        const copy = node.cloneNode(true);
        copy.querySelectorAll('button,script,style,.asst-btm,.asst-ctx,.asst-ctx-card,.rc-asst-ctx,.asst-followups,.vc-if-hd,.rc-turn-flow,.rc-turn-status,.vc-card,.mfx-cursor,mjx-assistive-mml').forEach(el => el.remove());
        return text(copy.textContent || '', limit).trim();
      }
      function messageID(node, index) {
        const id = node.getAttribute('data-turn-id') || node.getAttribute('data-turn') || node.getAttribute('data-message-id');
        if (id) return 'm-' + hash(id + ':' + (node.classList.contains('asst-u') ? 'user' : 'assistant'));
        let stable = nodeIDs.get(node);
        if (!stable) { stable = 'legacy-' + index + '-' + hash(node.classList.contains('asst-u') ? 'user' : 'assistant'); nodeIDs.set(node, stable); }
        return stable;
      }
      function partID(part, message, index) {
        return message + '-p-' + hash(String(part.id || part.item_id || part.call_id || part.cid || part.gid || part.card?.cid || (part.seq ?? index)) + ':' + (part.kind || 'artifact'));
      }
      function registerAction(id, node, run) {
        const actionId = scope + ':a:' + hash(id);
        actions.set(actionId, { scope, node, run });
        return actionId;
      }
      function reveal(node, tid, cardIndex) {
        setLegacy(true);
        if (tid && rc().turnCard?.openFlow) rc().turnCard.openFlow(tid);
        if (node?.isConnected) {
          const card = node.matches?.('.vc-card') ? node : node.querySelector?.('.vc-card');
          if (card?.classList.contains('vc-dot')) card.querySelector('.vc-card-dot')?.click();
          if (card?.classList.contains('vc-min')) card.querySelector('.vc-card-hd,.vc-if-hd')?.click();
          if (Number.isInteger(cardIndex)) {
            const group = [node, ...node.querySelectorAll('*')].find(el => el.__fcPager && Array.isArray(el.__fcActive));
            const position = group?.__fcActive.indexOf(cardIndex) ?? -1;
            if (position >= 0) group.__fcPager.goto(position, { reason: 'native-open-original' });
          }
          node.scrollIntoView({ block: 'center', behavior: 'instant' });
        }
      }
      function artifact(id, node, title, body = '', tid = '') {
        return { id, kind: 'artifact', title: text(title, 160) || '生成物', text: text(body, 1600), status: 'saved', data: {},
          actionId: registerAction(id, node, () => reveal(node, tid)), actionLabel: tid ? '查看完整流程' : '打开原件' };
      }
      function safeFields(data, keys) {
        const out = {};
        for (const key of keys) {
          const value = data?.[key];
          if (typeof value === 'string') out[key] = text(value, 24000);
          else if (typeof value === 'boolean' || typeof value === 'number' && Number.isFinite(value)) out[key] = value;
        }
        return out;
      }
      function statusOf(value) {
        if (value?.error || /^(error|failed|failure)$/.test(String(value?.status || ''))) return 'failed';
        if (/^(running|started|in_progress|pending)$/.test(String(value?.status || ''))) return 'running';
        if (/^(done|completed|success|succeeded)$/.test(String(value?.status || '')) || value?.result != null) return 'completed';
        return 'unknown';
      }
      function projectPart(part, id, node, tid) {
        const kind = part.kind;
        if (kind === 'text' || kind === 'meta') return [];
        if (kind === 'tool') {
          const result = artifact(id, node, part.label || part.tool || '工具调用', '', tid);
          result.kind = 'tool'; result.status = statusOf(part);
          result.text = { failed: '操作失败', running: '处理中', completed: '已完成', unknown: '查看处理详情' }[result.status];
          const steps = Array.isArray(part.steps) && part.steps.length ? part.steps : [part];
          result.data = { tool: text(part.tool, 120), stepCount: steps.length,
            successCount: steps.filter(x => statusOf(x) === 'completed').length,
            failureCount: steps.filter(x => statusOf(x) === 'failed').length,
            runningCount: steps.filter(x => statusOf(x) === 'running').length };
          actions.get(result.actionId).inspect = () => ({ kind: 'tool', title: result.title, content: part });
          return [result];
        }
        if (kind === 'cards' && Array.isArray(part.cards)) {
          return part.cards.map((card, index) => {
            const result = artifact(id + '-c-' + index, node, card.title || '学习卡片');
            result.kind = 'anki'; result.status = part.draft ? 'draft' : 'saved';
            result.actionId = registerAction(result.id, node, () => reveal(node, '', index));
            actions.get(result.actionId).inspect = () => ({ kind: 'anki', title: result.title,
              content: { gid: part.gid, cardIndex: index, card: flashGroup(node)?.__fc.cards[index] || card } });
            result.data = { ...safeFields(card, ['front', 'back', 'question', 'answer', 'type', 'cloze', 'text', 'explanation']), draft: !!part.draft, gid: text(part.gid, 160) };
            result.data.front = result.data.front ?? result.data.question ?? result.data.cloze ?? result.data.text ?? '';
            result.data.back = result.data.back ?? result.data.answer ?? '';
            return result;
          });
        }
        if (kind === 'card' && part.card) {
          const card = part.card, data = card.data || {};
          const result = artifact(id, node, card.title || '生成物');
          actions.get(result.actionId).inspect = () => ({ kind: card.kind || 'artifact', title: result.title, content: card });
          if (['fact', 'general', 'weather', 'news'].includes(card.kind)) {
            result.kind = card.kind;
            result.data = safeFields(data, ['answer', 'detail', 'text', 'summary', 'description', 'loc', 'date', 'lo', 'hi', 'cond', 'precip', 'tip']);
            if (card.kind === 'news' && Array.isArray(data.items)) result.data.items = data.items.map(item => safeFields(item, ['t', 's', 'src']));
            if (card.kind === 'general' && !result.data.text) result.data.text = text(card.brief);
            result.text = text(data.answer || data.text || card.brief || '', 1600);
          } else {
            result.text = text(card.brief || '', 1600);
            if (card.kind === 'images' || card.kind === 'videos') result.data = { kind: card.kind, count: Array.isArray(data.items) ? data.items.length : 0 };
          }
          return [result];
        }
        return [artifact(id, node, part.title || part.label || (kind === 'hlcard' ? '操作记录' : '生成物'))];
      }
      function projectMessage(node, index) {
        const id = messageID(node, index), tid = node.getAttribute('data-turn') || '';
        let presentation = null;
        try { if (tid) presentation = rc().turnCard?.presentationOf(tid); } catch (_) {}
        const source = presentation?.parts || [];
        const role = presentation?.role || (node.classList.contains('asst-u') ? 'user' : 'assistant');
        // Structured turns include live drafts without reading a rendered web
        // bubble. Standalone legacy messages remain pending migration.
        const body = presentation
          ? source.filter(part => part.kind === 'text').map(part => text(part.text)).join('\n\n')
          : (!tid && !node.__vcCard && !flashGroup(node) ? cleanText(node) : '');
        const streaming = presentation ? presentation.streaming : (!tid && (node.matches('.mfx-streaming,.mfx-typing') || !!node.querySelector('.mfx-streaming,.mfx-typing')));
        const parts = [];
        const contentNodes = Array.from(node.querySelectorAll(':scope > .rc-turn-bd > .rc-part:not(.rc-part-text)'));
        const usedContent = new Set();
        source.forEach((part, partIndex) => {
          if (!part || typeof part !== 'object') return;
          const content = part.kind === 'tool' || part.kind === 'meta' || part.kind === 'text' ? node
            : (contentNodes.find(el => !usedContent.has(el) && el.classList.contains('rc-part-' + part.kind)) || node);
          usedContent.add(content);
          parts.push(...projectPart(part, partID(part, id, partIndex), content, tid));
        });
        if (!parts.length && flashGroup(node)) {
          const group = flashGroup(node);
          parts.push(...projectPart({ kind: 'cards', cards: group.__fc.cards, gid: group.__fc.gid }, id + '-learning', node, ''));
        }
        if (!parts.length && node.__vcCard) parts.push(...projectPart({ kind: 'card', card: node.__vcCard }, id + '-card', node, ''));
        if (!parts.length && !body && !streaming && (node.matches('.vc-card,.vc-if') || node.querySelector('.vc-card,.fc-wrap,iframe,video'))) {
          parts.push(artifact(id + '-artifact', node, node.querySelector('.vc-card-hd,.vc-if-hd')?.textContent || '生成物'));
        }
        if (body.length >= 32000 || node.querySelector('iframe,video,img,mjx-container,a,.asst-ctx,.asst-ctx-card,.rc-asst-ctx,.asst-pagelink,.actx-page,.asst-btm,.asst-followups,.asst-clip,.asst-jump,.asst-undo')) {
          parts.push(artifact(id + '-original', node, '完整内容与操作', cleanText(node.querySelector('.asst-ctx,.asst-ctx-card,.rc-asst-ctx'), 1200)));
        }
        return body || parts.length || streaming ? { id, role, text: text(body), streaming, parts } : null;
      }
      function flashGroup(node) {
        return [node, ...node.querySelectorAll('*')].find(el => el.__fc && Array.isArray(el.__fc.cards));
      }
      function liveArtifacts(messages) {
        for (const message of messages) {
        for (const part of message.parts) {
          if (part.kind === 'tool' || part.id.endsWith('-original')) continue;
          const target = actions.get(part.actionId), node = target?.node;
          if (!node?.isConnected) continue;
          const group = flashGroup(node);
          const cardIndex = Number(part.id.match(/-c-(\d+)$/)?.[1] || 0);
          if (typeof window.__setFocusSel === 'function') {
            const selectionOwner = part.id;
            part.data.selectId = registerAction(part.id + '-select', node, command => {
              if (typeof command.text !== 'string' || command.text.length > 16000) throw new Error('选区内容无效或过长');
              const selection = command.text.trim();
              if (!selection) {
                if (window.__bwNativeSelection?.owner === selectionOwner) window.__bwNativeSelection.active = false;
                return;
              }
              window.__bwNativeSelection = { text: selection, active: true, owner: selectionOwner, scope };
              window.__setFocusSel(selection, 'text');
              if (window.__focusSel?.text !== selection) throw new Error('选区暂未进入对话，请重试');
            });
          }
          if (group?.__fc.cards[cardIndex]?._removed) { part.removed = true; continue; }
          const interaction = group && rc().flashcard?.interactionState(group, cardIndex);
          if (interaction && (part.kind === 'anki' || part.kind === 'artifact')) {
            part.kind = 'anki';
            part.data.live = true;
            part.data.state = String(interaction.state || '');
            part.data.faces = interaction.presentation.faces;
            part.data.notice = interaction.presentation.notice;
            part.data.editable = interaction.editable;
            part.data.controls = interaction.controls.map(control => ({
              id: registerAction(part.id + '-control-' + control.key, group, () =>
                rc().flashcard.performInteraction(group, cardIndex, control.key)),
              title: control.title, disabled: control.disabled, destructive: control.destructive
            }));
            part.data.fields = interaction.fields.map(field => ({
              id: registerAction(part.id + '-field-' + field.key, group, command =>
                rc().flashcard.performInteraction(group, cardIndex, 'edit', { field: field.key, value: command.text })),
              key: field.key, value: field.value
            }));
          }
          const cardElement = node.matches('.vc-card') ? node : node.querySelector('.vc-card');
          const body = cardElement?.querySelector('.vc-card-bd') || node.querySelector('.vc-if-bd');
          if ((group || body) && rc().stickynote) {
            part.data.dragId = registerAction(part.id + '-place', node, command => {
              if (![command.x, command.y].every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1)) throw new Error('落点无效');
              const x = command.x * innerWidth, y = command.y * innerHeight;
              let accepted = false;
              if (group && rc().flashcard?.snapshot && rc().stickynote.createCardAt) {
                const cards = rc().flashcard.snapshot(group);
                accepted = rc().stickynote.createCardAt(x, y, cards, group.__fc.gid);
              } else if (body && rc().stickynote.createHtmlAt) {
                accepted = rc().stickynote.createHtmlAt(x, y, {
                  content: body.innerHTML, contextText: body.textContent || '', isHtml: true,
                  cid: cardElement?.dataset.vcCid || cardElement?.__vcCard?.cid || '', label: part.title
                });
              }
              if (!accepted) throw new Error('请把卡片拖到书页正文上');
            });
          }
        }
        message.parts = message.parts.filter(part => !part.removed);
        }
      }
      function toolbarActions() {
        const root = document.getElementById('header') || document.getElementById('ep-top');
        if (!root) return [];
        const page = root.querySelector('#page-scrub');
        const paging = page ? [{
          id: registerAction('toolbar-page', page, () => {
            page.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, pointerId:1, clientX:0}));
            page.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, pointerId:1, clientX:0}));
          }), title: page.textContent.trim(), key: 'page', disabled: false
        }] : [];
        return paging.concat(Array.from(root.querySelectorAll('button')).filter(button => !button.hidden && button.getAttribute('aria-hidden') !== 'true').map((button, index) => ({
          id: registerAction('toolbar-' + (button.id || index), button, () => button.click()),
          key: button.id || '', title: text(button.getAttribute('title') || button.getAttribute('aria-label') || button.textContent, 140).trim() || '阅读操作',
          disabled: !!button.disabled
        })));
      }
      function selectedContext() {
        return { text: text(window.__focusSel?.text, 16000), kind: text(window.__focusSel?.kind, 40) };
      }
      function getScopeKey() {
        let identity = '', history = '', mode = pane()?.dataset.assistantMode || 'normal';
        try { const state = account()?.snapshot(); identity = [state?.contextId || '', state?.namespace || '', state?.generation ?? '', state?.active || false].join(':'); } catch (_) {}
        try { history = String(window.__asstHistUrl?.() || ''); } catch (_) {}
        // Scope is opaque. Never send file paths, account namespaces, routes or credentials.
        return [navigationID, location.pathname, location.search, identity, history, mode].join('|');
      }
      function capabilities() {
        const out = ['refresh', 'showLegacy', 'hideLegacy', 'liveAction'];
        if (typeof window.__clearFocusSel === 'function') out.push('clearSelection');
        if (typeof drawer()?.open === 'function' && typeof drawer()?.close === 'function') out.push('toggleAssistant');
        if (typeof window.__asstSend === 'function') out.push('send');
        if (document.getElementById('asst-send')) out.push('stop');
        if (typeof rc().assistant?.openModelSettings === 'function') out.push('openModels');
        if (typeof window.openSettings === 'function' || document.getElementById('ep-set-btn')) out.push('openSettings');
        if (document.getElementById('asst-review-toggle')) out.push('openReview');
        if (typeof window.openSearch === 'function' || document.getElementById('ep-search-btn')) out.push('openSearch');
        if (document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="toc"],#ep-side .side-tab[data-pane="toc"]')) out.push('openTOC');
        if (document.getElementById('asst-call')) out.push('toggleVoice');
        if (document.getElementById('asst-computer')) out.push('toggleComputerVoice');
        if (actions.size) out.push('openArtifact', 'action', 'inspectArtifact');
        return out;
      }
      function applyVisualMode() {
        const root = document.documentElement;
        root.classList.toggle('bw-native-navigation', nativeMode);
        root.classList.toggle('bw-native-conversation-active', nativeMode && !legacyVisible && isOpen() && activeTab() === 'asst');
      }
      function setLegacy(visible) {
        legacyVisible = !!visible;
        if (visible) drawer()?.open('asst');
        applyVisualMode(); schedule();
      }
      function setNativeMode(enabled) {
        nativeMode = !!enabled;
        applyVisualMode(); schedule();
        return { ok: true };
      }
      function snapshot() {
        timer = null;
        if (suspended) return;
        discover();
        const nextKey = getScopeKey();
        if (nextKey !== scopeKey) {
          // An account/book switch can precede asynchronous history replacement.
          // Do not relabel the previous DOM as the new account's conversation.
          if (scopeKey) previousNodes.forEach(node => excludedNodes.add(node));
          scopeKey = nextKey; scope = 'reader-' + hash(nextKey); actions.clear(); nodeIDs = new WeakMap(); lastSignature = '';
          window.__bwNativeSelection = null;
        }
        actions = new Map();
        const all = thread ? Array.from(thread.children).filter(el => el.matches('.asst-msg,.vc-card,.vc-if,.rc-turn')) : [];
        const messages = all.filter(node => !excludedNodes.has(node)).map(projectMessage).filter(Boolean);
        previousNodes = all;
        liveArtifacts(messages);
        const readingTools = toolbarActions();
        const payload = { version: 1, scope, revision: 0, title: text(document.title, 160) || '阅读助手', ready: isReady(), busy: isBusy(),
          legacyVisible, selection: selectedContext(), readingTools, sidebarOpen: isOpen() && activeTab() === 'asst', conversationMode: conversationMode(), voice: voiceState(), messages, capabilities: capabilities() };
        const signature = JSON.stringify(payload);
        if (signature !== lastSignature) {
          lastSignature = signature; payload.revision = ++revision;
          try { handler.postMessage(payload); } catch (_) {}
        }
      }
      function schedule() {
        if (!suspended && timer == null) timer = setTimeout(snapshot, 60);
      }
      function wrapNotifications(owner, names) {
        if (!owner) return;
        let installed = hooked.get(owner);
        if (!installed) { installed = new Set(); hooked.set(owner, installed); }
        names.forEach(name => {
          if (installed.has(name) || typeof owner[name] !== 'function') return;
          const original = owner[name];
          owner[name] = function (...args) { try { return original.apply(this, args); } finally { schedule(); } };
          installed.add(name);
        });
      }
      function voiceState() {
        const computer = document.getElementById('asst-computer'), call = document.getElementById('asst-call');
        const active = el => !!el?.classList.contains('on');
        const busy = el => !!el?.classList.contains('connecting');
        const selected = active(computer) || busy(computer) ? computer : (active(call) || busy(call) ? call : null);
        return { mode: selected === computer && selected ? 'computer' : (selected ? 'realtime' : 'none'),
          active: active(selected), busy: busy(selected),
          label: selected ? text(document.querySelector('#rc-vc .vc-st')?.textContent || (busy(selected) ? '正在连接' : '通话中'), 240) : '未连接' };
      }
      function observeDrawer() {
        const owner = drawer();
        if (!owner || hooked.has(owner)) return;
        hooked.set(owner, new Set());
        ['open', 'setTab', 'close'].forEach(name => {
          if (typeof owner[name] !== 'function') return;
          const original = owner[name];
          owner[name] = function (...args) {
            const result = original.apply(this, args);
            schedule(); return result;
          };
        });
      }
      function discover() {
        const currentDrawer = document.getElementById('ep-side') || document.getElementById('grammar-panel');
        if (currentDrawer !== drawerElement) {
          drawerObserver?.disconnect(); drawerElement = currentDrawer;
          if (drawerElement) {
            drawerObserver = new MutationObserver(schedule);
            drawerObserver.observe(drawerElement, { attributes: true, attributeFilter: ['class'] });
          }
        }
        const current = document.getElementById('asst-thread');
        if (current !== thread) {
          threadObserver?.disconnect(); thread = current;
          if (thread) {
            threadObserver = new MutationObserver(schedule);
            threadObserver.observe(thread, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'hidden', 'data-turn-id', 'data-turn', 'disabled'] });
          }
        }
        const bar = document.getElementById('header') || document.getElementById('ep-top');
        if (toolbarElement !== bar) {
          toolbarObserver?.disconnect(); toolbarElement = bar;
          if (bar) { toolbarObserver = new MutationObserver(schedule); toolbarObserver.observe(bar, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'disabled', 'title'] }); }
        }
        const parent = pane();
        if (contextElement !== parent) {
          contextObserver?.disconnect(); contextElement = parent;
          if (parent) { contextObserver = new MutationObserver(schedule); contextObserver.observe(parent, { childList: true, subtree: true, characterData: true }); }
        }
        wrapNotifications(window, ['__setFocusSel', '__clearFocusSel', '__renderFocusSel']);
        const currentControls = document.getElementById('asst-input');
        if (currentControls !== controls) {
          controlsObserver?.disconnect(); controls = currentControls;
          if (controls) { controlsObserver = new MutationObserver(schedule); controlsObserver.observe(controls, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'disabled', 'title', 'placeholder'] }); }
        }
        observeDrawer();
        applyVisualMode();
        wrapNotifications(rc().turnCard, ['addPart', 'draftText', 'freezeDraft', 'reconcile', 'cliPart', 'busy', 'idle', 'status', 'progress', 'drop', 'rename', 'reset']);
        if (!accountSubscription && account()?.subscribe) accountSubscription = account().subscribe(schedule);
      }
      async function perform(command) {
        if (suspended) return { ok: false, error: '页面已离开，请在当前页面操作' };
        if (!command || typeof command !== 'object' || Array.isArray(command)) return { ok: false, error: '无效操作' };
        if (getScopeKey() !== scopeKey) snapshot();
        if (command.scope && command.scope !== scope) return { ok: false, error: '会话已切换，请重新操作' };
        if (Object.keys(command).some(key => !['action', 'scope', 'text', 'actionId', 'x', 'y'].includes(key))) return { ok: false, error: '不支持的操作参数' };
        const action = command.action;
        try {
          if (action === 'send') {
            if (!isReady()) return { ok: false, error: '阅读器尚未准备好' };
            if (isBusy()) return { ok: false, error: '上一条消息仍在处理中' };
            if (typeof command.text !== 'string' || !command.text.trim() || command.text.length > 32000) return { ok: false, error: '消息为空或过长' };
            const voice = voiceState();
            if (voice.busy) return { ok: false, error: '语音正在连接，请稍候再发送' };
            if (voice.active && conversationMode() === 'normal') {
              // Never fall through into a separate text conversation when the
              // visible current call cannot accept its typed input.
              if (typeof window.__vcSendText !== 'function' || window.__vcSendText(command.text.trim()) !== true) return { ok: false, error: '当前语音未接收文字，请稍后重试' };
            } else {
              // Existing send owns context/tools/history. Review intentionally
              // remains its separate persistent text conversation, as before.
              const pending = window.__asstSend(command.text.trim());
              if (pending?.catch) pending.catch(schedule);
            }
          } else if (action === 'toggleAssistant') {
            if (!drawer()?.open || !drawer()?.close) return { ok: false, error: '侧栏尚未准备好' };
            if (isOpen() && activeTab() === 'asst') { drawer().close(); legacyVisible = false; }
            else drawer().open('asst');
          } else if (action === 'clearSelection') {
            if (typeof window.__clearFocusSel !== 'function') return { ok: false, error: '选区尚未准备好' };
            window.__clearFocusSel();
            window.__bwNativeSelection = null;
          } else if (action === 'liveAction') {
            const target = actions.get(command.actionId);
            if (!command.scope || !target || target.scope !== scope || !target.node?.isConnected) return { ok: false, error: '内容已更新，请重试' };
            await target.run(command);
          } else if (action === 'stop') {
            const button = document.getElementById('asst-send');
            if (!button?.classList.contains('stop') || button.disabled) return { ok: false, error: '当前没有可停止的文字回复' };
            button.click();
          } else if (action === 'toggleVoice' || action === 'toggleComputerVoice') {
            const button = document.getElementById(action === 'toggleVoice' ? 'asst-call' : 'asst-computer');
            if (!button || button.disabled || button.classList.contains('vc-review-disabled')) return { ok: false, error: '当前无法使用这项语音功能' };
            button.click();
          } else if (action === 'showLegacy' || action === 'hideLegacy') {
            setLegacy(action === 'showLegacy');
          } else if (action === 'refresh') {
            rc().assistant?.reloadHistory?.();
          } else if (action === 'inspectArtifact') {
            const target = actions.get(command.actionId);
            if (!command.scope || !target || target.scope !== scope || !target.node?.isConnected || !target.inspect) {
              return { ok: false, error: '此项内容暂不可读取，请刷新后重试' };
            }
            return { ok: true, detail: JSON.parse(JSON.stringify(target.inspect())) };
          } else if (action === 'openArtifact' || action === 'action') {
            const target = actions.get(command.actionId);
            if (!target || target.scope !== scope || !target.node?.isConnected) return { ok: false, error: '内容已更新，请重新打开' };
            target.run();
          } else if (action === 'openModels' && typeof rc().assistant?.openModelSettings === 'function') {
            setLegacy(true); rc().assistant.openModelSettings();
          } else if (action === 'openSettings' && (typeof window.openSettings === 'function' || document.getElementById('ep-set-btn'))) {
            setLegacy(true);
            if (typeof window.openSettings === 'function') window.openSettings(); else document.getElementById('ep-set-btn').click();
          } else if (action === 'openReview' && document.getElementById('asst-review-toggle')) {
            setLegacy(true); document.getElementById('asst-review-toggle').click();
          } else if (action === 'openSearch' && (typeof window.openSearch === 'function' || document.getElementById('ep-search-btn'))) {
            setLegacy(true);
            if (typeof window.openSearch === 'function') window.openSearch(); else document.getElementById('ep-search-btn').click();
          } else if (action === 'openTOC' && document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="toc"],#ep-side .side-tab[data-pane="toc"]')) {
            setLegacy(true); drawer()?.open('toc');
          } else return { ok: false, error: '当前页面不支持此操作' };
          schedule();
          return { ok: true };
        } catch (error) { return { ok: false, error: error?.message || '操作未完成，请重试' }; }
      }
      document.getElementById('bw-native-conversation-style')?.remove();
      const style = document.createElement('style');
      style.id = 'bw-native-conversation-style';
      style.textContent = `
        .bw-native-navigation #header,.bw-native-navigation #ep-top,.bw-native-navigation #fs-restore {display:none!important}
        .bw-native-conversation-active #ep-side,.bw-native-conversation-active #grammar-panel,
        .bw-native-conversation-active #side-handle,.bw-native-conversation-active #ep-side-handle {visibility:hidden!important;pointer-events:none!important}
        .bw-native-conversation-active body.grammar-open #main,.bw-native-conversation-active body.grammar-open #header {padding-right:0!important}
        .bw-native-conversation-active body.ep-side-open #ep-content {padding-right:0!important}
        .bw-native-conversation-active body.ep-side-open #ep-viewer,.bw-native-conversation-active body.ep-side-open #html-content {margin-right:0!important}
      `;
      document.documentElement.appendChild(style);
      const mountObserver = new MutationObserver(records => {
        if (!thread || !thread.isConnected || records.some(record => Array.from(record.addedNodes).some(node => node.nodeType === 1 && (['asst-thread', 'asst-input', 'asst-computer', 'asst-call'].includes(node.id) || node.querySelector?.('#asst-thread,#asst-input,#asst-computer,#asst-call'))))) schedule();
      });
      mountObserver.observe(document.documentElement, { childList: true, subtree: true });
      ['DOMContentLoaded', 'popstate', 'hashchange', 'bw:native-local-runtime-ready', 'rc:assistant-mode-changed', 'bw-native-computer-voice-state'].forEach(name => window.addEventListener(name, schedule));
      window.addEventListener('pageshow', () => { suspended = false; mountObserver.observe(document.documentElement, { childList: true, subtree: true }); thread = null; controls = null; drawerElement = null; contextElement = null; toolbarElement = null; schedule(); });
      window.addEventListener('pagehide', () => { suspended = true; threadObserver?.disconnect(); controlsObserver?.disconnect(); drawerObserver?.disconnect(); contextObserver?.disconnect(); toolbarObserver?.disconnect(); mountObserver.disconnect(); if (timer != null) clearTimeout(timer); timer = null; });
      window.__bwNativeConversation = Object.freeze({ perform, setNativeMode, snapshot: () => { lastSignature = ''; snapshot(); } });
      schedule();
    })();
    """#
}
