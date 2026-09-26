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
      window.__BW_NATIVE_REVIEW_CONTROL__ = true;
      let legacyVisible = false, nativeMode = false;
      // 原生侧栏的开合**由原生自己记**，不再借网页抽屉的开合来表示。
      // ⚠ 这是 2026-09-22 反复栽跟头之后的收口：只要还调 `drawer().open()`，
      //   网页那套侧栏就会按它自己的样式滑出来，能不能看见就取决于一堆条件
      //   （nativeMode 送到没、tab 是不是 asst、CSS 落了没）——
      //   任何一条不成立，用户看到的就是**两个侧栏并排**。
      //   不开它，这一整类条件就都不存在了。
      let nativeAssistantOpen = false;
      // Swift 那边到底说没说过话。没说之前，网页外壳保持
      // documentStart 定的默认（藏着），不能拿初值 false 当“已知关闭”。
      let nativeModeKnown = false;
      let thread = null, threadObserver = null, timer = null, revision = 0;
      let scope = '', scopeKey = '', lastSignature = '', accountSubscription = null, selectionSubscription = null, selectionRegistry = null;
      let actions = new Map(), nodeIDs = new WeakMap();
      // 对话流里用到的学习卡组（gid）：卡面/操作的状态仍在 rc-flashcard，快照按需带上（迁出 P4）。
      let watchedCardGroups = [];
      let controls = null, controlsObserver = null, suspended = false;
      let captionElement = null, captionObserver = null;
      let settingsModels = null, settingsVoice = null;
      let nativePageSelectionSequence = 0;
      let searchController = null, searchResults = new Map(), searchQuery = '', searchSequence = 0;
      let tocController = null, tocEntries = new Map(), tocSequence = 0, tocOwner = null;
      let placementScopeKey = '', previousPlacementNodes = [], excludedPlacementNodes = new WeakSet();
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
        const known = nodeIDs.get(node);
        if (known) return known;
        const id = node.getAttribute('data-turn-id') || node.getAttribute('data-turn') || node.getAttribute('data-message-id');
        if (id) { const value = 'm-' + hash(id + ':' + (node.classList.contains('asst-u') ? 'user' : 'assistant')); nodeIDs.set(node,value); return value; }
        let stable = nodeIDs.get(node);
        if (!stable) { stable = 'legacy-' + index + '-' + hash(node.classList.contains('asst-u') ? 'user' : 'assistant'); nodeIDs.set(node, stable); }
        return stable;
      }
      // 迁出 P4（2026-09-26）：侧栏消息全部由原生对话流给（历史、实时事件、语音轮次、复习选用），
      // 这里不再按 DOM 抓取投影。网页生产者仍会调用 __bwNativeMessages 的挂载/移除钩子 ——
      // 留一个薄壳：只把直接写进对话区的提示（语音出错等）在挂载那一刻交给原生，其余钩子不再有意义。
      const forwardedNotes = new WeakSet();
      function forwardNote(node) {
        if (window.__bwNativeConversationFeed !== true || !node || forwardedNotes.has(node) ||
            !node.classList?.contains('asst-note') || node.getAttribute?.('data-turn')) return;
        forwardedNotes.add(node);
        try { handler.postMessage({ version: 1, type: 'feed-note', text: cleanText(node, 2000) }); }
        catch (error) { try { window.dlog?.('[对话流] 提示没交到原生：' + (error && error.message || error)); } catch (_) {} }
      }
      window.__bwNativeMessages = Object.freeze({
        publish(node) { forwardNote(node); }, replace(node) { forwardNote(node); },
        remove() {}, clear() {}, stage() {}, commit() {}
      });
      function registerAction(id, node, run) {
        const actionId = scope + ':a:' + hash(id);
        actions.set(actionId, { scope, node, run });
        return actionId;
      }
      // 「打开原件」原来是 `setLegacy(true)` + 滚到那张卡 —— 也就是**把旧网页界面
      // 端出来**。那是原生外壳里最后一条通往旧界面的路，已删除
      // （2026-09-22 用户：“我要的是把旧的内容用原生功能直接代替后把原版删除”）。
      // 取而代之：原件的内容就地交给原生检视器（下面 artifact 注册的 inspect）。
      function revealContent(node) {
        if (!node?.isConnected) return '';
        // 只取正文，把网页自己的操作件剔掉 —— 它们在原生里既点不了、
        // 显示出来也只是一堆没用的按钮文字。
        const copy = node.cloneNode(true);
        copy.querySelectorAll('button,input,select,textarea,script,style,.asst-btm,.asst-followups,.asst-undo,.asst-clip,.asst-jump')
          .forEach(el => el.remove());
        return String(copy.innerHTML || '').slice(0, 120000);
      }
      function artifact(id, node, title, body = '', tid = '') {
        // ⚠ run 是空的：没有任何原生界面会去点它（openArtifact/action 已从
        //   命令白名单里去掉）。原件改为**就地在原生里看**，所以这里必须
        //   注册 inspect —— 不注册的话侧栏那个「内容资料」按钮点下去只会
        //   得到"此项内容暂不可读取"，等于摆了个坏按钮。
        const actionId = registerAction(id, node, () => {});
        actions.get(actionId).inspect = () => ({
          kind: 'html', title: text(title, 160) || '生成物',
          content: { html: revealContent(node) ||
            text(body, 1600).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') },
        });
        return { id, kind: 'artifact', title: text(title, 160) || '生成物', text: text(body, 1600),
          status: 'saved', data: {}, actionId, actionLabel: tid ? '查看完整流程' : '查看原件' };
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
      function nativePartHandles(part, id, node, tid) {
        const descriptor = (key, kind, title, original) => {
          const detail = { kind, title, content: original };
          const result = { id: key, kind, title: '', text: '', status: 'unknown',
            data: { nativeDetail: detail, nativeActionKey: key }, actionLabel: tid ? '查看完整流程' : '查看原件' };
          if (tid && part.nativePartID) result.data.nativeTurnPart = {id:part.nativePartID};
          if (tid) result.data.nativeParentContextId = 'turn:' + tid;
          return result;
        };
        if (part.kind === 'tool') return [descriptor(id, 'tool', part.label || part.tool || '工具调用', part)];
        if (part.kind === 'hlcard') {
          const result = descriptor(id, 'operations', '操作记录', part);
          result.data.nativeOperation = {tid,partID:part.nativePartID};
          return [result];
        }
        if (part.kind === 'cards' && Array.isArray(part.cards)) return part.cards.map((card, index) => {
          const result = descriptor(id + '-c-' + index, 'anki', card.title || '学习卡片',
            { gid: part.gid, cardIndex: index, card });
          result.data.gid = String(part.gid || ''); result.data.draft = !!part.draft;
          if (result.data.nativeTurnPart) result.data.nativeTurnPart.cardIndex = index;
          return result;
        });
        if (part.kind === 'card' && part.card) {
          const mounted = node.__vcCard || node.querySelector('.vc-card')?.__vcCard;
          const card = mounted?.cid && mounted.cid === part.card.cid ? mounted : part.card;
          const result = descriptor(id, card.kind || 'artifact', card.title || '生成物', card);
          // Until this legacy producer is removed, a locally edited media
          // original must travel as data rather than refer to an older source.
          if (card !== part.card && JSON.stringify(card) !== JSON.stringify(part.card)) delete result.data.nativeTurnPart;
          // Swift creates media controls from original slots and reads the
          // shared native selection graph. No per-image web action registry.
          return [result];
        }
        // Historical extension kinds retain their original data for native
        // inspection. Do not mount/read a hidden HTML view to recover it.
        return [descriptor(id, 'artifact', part.title || part.label || '生成物', part)];
      }
      function projectPart(part, id, node, tid) {
        const kind = part.kind;
        if (kind === 'text' || kind === 'meta') return [];
        if (nativeMode) return nativePartHandles(part, id, node, tid);
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
            result.actionId = registerAction(result.id, node, () => {});
            actions.get(result.actionId).inspect = () => ({ kind: 'anki', title: result.title,
              content: { gid: part.gid, cardIndex: index,
                card: flashGroup(node, part.gid)?.__fc.cards[index] || card } });
            result.data = { ...safeFields(card, ['front', 'back', 'question', 'answer', 'type', 'cloze', 'text', 'explanation']), draft: !!part.draft, gid: text(part.gid, 160) };
            result.data.front = result.data.front ?? result.data.question ?? result.data.cloze ?? result.data.text ?? '';
            result.data.back = result.data.back ?? result.data.answer ?? '';
            return result;
          });
        }
        if (kind === 'card' && part.card) {
          const mounted = node.__vcCard || node.querySelector('.vc-card')?.__vcCard;
          const card = mounted?.cid && mounted.cid === part.card.cid ? mounted : part.card, data = card.data || {};
          const result = artifact(id, node, card.title || '生成物');
          actions.get(result.actionId).inspect = () => ({ kind: card.kind || 'artifact', title: result.title, content: card });
          if (['images', 'videos'].includes(card.kind) && rc().voiceCard?.mediaPresentation) {
            result.kind = card.kind;
            const root = node.matches('.vc-card') ? node : node.querySelector('.vc-card');
            result.data.items = rc().voiceCard.mediaPresentation(card).map(item => {
              const mediaID = registerAction(id + '-image-' + item.index, node, () => {});
              actions.get(mediaID).resource = () => {
                const current = rc().voiceCard.mediaPresentation(card).find(value => value.index === item.index);
                if (!current) throw new Error('图片已移除');
                return current.route;
              };
              return { index: item.index, title: text(item.title, 240), source: text(item.source, 120),
                sourceURL: text(item.sourceURL, 4096), mediaID, video: item.video, selected: item.selected, isMap: item.isMap, map: item.map,
                selectID: root ? registerAction(id + '-image-select-' + item.index, root, () => rc().voiceCard.mediaAction(root, card, item.index, 'toggle')) : '',
                removeID: root ? registerAction(id + '-image-remove-' + item.index, root, () => rc().voiceCard.mediaAction(root, card, item.index, 'remove')) : '' };
            });
          } else if (['fact', 'general', 'weather', 'news'].includes(card.kind)) {
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
      // 找这组学习卡当前挂着的容器。
      //
      // ⚠ 先按节点爬（老路，命中率最高），爬不到再**按 gid 取**。
      //   只爬节点的后果实测过：卡组明明在 _groups[gid] 里，只因为此刻不挂在
      //   这个动作锚点下面，整张卡就卡在"学习卡正在同步…"（2026-09-22 实报）。
      //   gid 是这组卡的身份，比"它此刻挂在哪儿"稳得多 —— 这也是把判断从 DOM
      //   上摘下来的第一步。
      function flashGroup(node, gid) {
        // The requested identity wins over whichever group happens to be the
        // first descendant of a multi-artifact turn.
        if (gid) {
          try {
            const registered = rc().flashcard?.containerOf?.(gid);
            if (registered?.__fc?.gid === gid) return registered;
          } catch (_) {}
        }
        const found = node?.querySelectorAll
          ? [node, ...node.querySelectorAll('*')].find(el => el.__fc && Array.isArray(el.__fc.cards) && (!gid || el.__fc.gid === gid))
          : null;
        if (found) return found;
        return null;
      }
      function inlineImageSources(values) {
        const sources = new Set();
        for (const value of values) {
          if (typeof value !== 'string' || !/<img\b/i.test(value)) continue;
          // Template content stays inert: this parses original markup without
          // mounting it, executing scripts or fetching its image URLs.
          const template = document.createElement('template'); template.innerHTML = value;
          for (const image of template.content.querySelectorAll('img')) sources.add(image.getAttribute('src') || '');
        }
        return Array.from(sources);
      }
      // 卡身里选中文字 → 进对话。侧栏卡与原生页卡共用。
      function selectAction(partId, node) {
        return registerAction(partId + '-select', node, command => {
          if (typeof command.text !== 'string' || command.text.length > 16000) throw new Error('选区内容无效或过长');
          const selection = command.text.trim();
          if (!selection) {
            if (window.__bwNativeSelection?.owner === partId) window.__bwNativeSelection.active = false;
            return;
          }
          window.__bwNativeSelection = { text: selection, active: true, owner: partId, scope };
          window.__setFocusSel(selection, 'text');
          if (window.__focusSel?.text !== selection) throw new Error('选区暂未进入对话，请重试');
        });
      }
      // 卡内的内联图片 → 不透明 id（Swift 拿不到任意 URL，只能按 id 取同一条本地资源路由）。
      function inlineImageActions(part, node, target) {
        const inlineImages = {};
        // Native cards parse the original content and authorize image routes
        // directly. Do not build inert DOM just to repeat that projection.
        if (nativeMode) return inlineImages;
        if (!rc().voiceCard?.mediaRoute) return inlineImages;
        const nativeCard = part.data.nativeCard?.card;
        const values = nativeCard ? ['front','back','cloze','_displayFrontHtml','_displayBackHtml'].map(key => nativeCard[key]) : part.kind === 'anki' ?
          (part.data.state === 'draft' ? (part.data.fields || []).map(f => f.value) : (part.data.faces || []).map(f => f.content)) :
          [part.data.text, part.data.answer, part.data.detail, part.text];
        const expected = JSON.stringify(target.inspect?.().content);
        for (const source of inlineImageSources(values).slice(0, 64)) {
          if (!source || source.length > 8192 || !rc().voiceCard.mediaRoute(source)) continue;
          const mediaID = registerAction(part.id + '-inline-' + hash(source), node, () => {});
          actions.get(mediaID).resource = () => {
            if (JSON.stringify(target.inspect?.().content) !== expected) throw new Error('图片已更新');
            return rc().voiceCard.mediaRoute(source);
          };
          inlineImages[source] = mediaID;
        }
        return inlineImages;
      }
      // 学习卡组找不到挂载的容器时：把卡位里实际写着什么带出来（草稿保存失败时渲染层把原因
      // 写进了卡位），记一次日志，并在稍后重拍几次快照 —— 容器可能只是晚一点才登记。
      // ⚠ 2026-09-23 用户截图：侧栏里只剩「学习卡暂时读不到（no-card-group）」，看不出为什么。
      const cardGroupRetries = new Map();
      function missingCardGroup(part, node) {
        const gid = String(part.data?.gid || part.id);
        const hint = String(node?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        if (hint) part.data.liveReason = 'no-card-group：' + hint;
        const tries = cardGroupRetries.get(gid) || 0;
        if (tries >= 3) return;
        cardGroupRetries.set(gid, tries + 1);
        if (tries === 0) {
          try { window.__bwClientLog?.('log', '[card-group] missing gid=' + gid.slice(0, 40) + ' hint=' + hint); } catch (_) {}
        }
        setTimeout(scheduleMessages, 1200 * (tries + 1));
      }
      function liveArtifacts(messages) {
        for (const message of messages) {
        for (const part of message.parts) {
          if (nativeMode && part.data?.nativeActionKey) {
            // Only producer metadata crosses this boundary. Swift owns the
            // action identities and dispatch; no hidden node/closure is kept
            // alive just to inspect, select or place an original artifact.
            if (part.kind === 'anki') {
              const group = flashGroup(null, part.data.gid);
              const index = part.data.nativeDetail?.content?.cardIndex;
              const input = group && rc().flashcard?.presentationInput?.(group, index);
              if (input) {
                part.data.nativeCard = input;
                part.data.activeInGroup = group.__fc.idx === index;
              } else part.data.liveReason = 'card-state-pending';
            }
            continue;
          }
          if (part.kind === 'tool' || part.id.endsWith('-original')) continue;
          const target = actions.get(part.actionId), node = target?.node;
          if (!node?.isConnected) {
            // ⚠ 说出来。学习卡卡在"正在同步…"时，原来这里是直接 continue ——
            //   原生那边只看到 live!==true，分不清是节点没了、卡组没挂上，
            //   还是状态取不到。三种情况要做的事完全不同（2026-09-22 实报）。
            if (part.kind === 'anki') part.data.liveReason = 'node-gone';
            continue;
          }
          const group = !nativeMode || part.kind === 'anki' || part.kind === 'artifact'
            ? flashGroup(node, part.data?.gid) : null;
          const cardIndex = Number(part.id.match(/-c-(\d+)$/)?.[1] || 0);
          const nativePin = nativeMode && ['anki','fact','general','weather','news','images','videos'].includes(part.kind);
          const pinOwner = !nativePin && [node, ...node.querySelectorAll('*')].find(el => el.__bwPinHoldBindings?.length);
          const pin = pinOwner && rc().voiceCard?.contextControl?.(pinOwner);
          if (pin) {
            part.data.pinned = pin.selected;
            part.data.pinId = registerAction(part.id + '-pin', pinOwner, () => rc().voiceCard.toggleContext(pinOwner, cardIndex));
          }
          if (typeof window.__setFocusSel === 'function') part.data.selectId = selectAction(part.id, node);
          if (group?.__fc.cards[cardIndex]?._removed) { part.removed = true; continue; }
          const nativeCard = nativeMode && group && rc().flashcard?.presentationInput?.(group, cardIndex);
          const interaction = !nativeCard && group && rc().flashcard?.interactionState(group, cardIndex);
          if (!nativeCard && !interaction && part.kind === 'anki') {
            part.data.liveReason = !group ? 'no-card-group'
              : !rc().flashcard ? 'no-flashcard-module' : 'no-state:' + cardIndex;
            if (!group) missingCardGroup(part, node);
          }
          if (nativeCard && (part.kind === 'anki' || part.kind === 'artifact')) {
            part.kind = 'anki';
            part.data.nativeCard = nativeCard;
            part.data.activeInGroup = group.__fc.idx === cardIndex;
            // Swift issues and resolves action IDs against its current card
            // data. There are no hidden webpage rating/edit closures to invoke.
            part.data.nativeActionOwner = part.actionId;
          } else if (interaction && (part.kind === 'anki' || part.kind === 'artifact')) {
            part.kind = 'anki';
            part.data.live = true;
            part.data.state = String(interaction.state || '');
            part.data.faces = interaction.presentation.faces;
            part.data.notice = interaction.presentation.notice;
            part.data.editable = interaction.editable;
            part.data.activeInGroup = group.__fc.idx === cardIndex;
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
          // Inline images retain the same local asset/proxy route as image
          // cards. Swift receives opaque IDs; it cannot request arbitrary URLs.
          part.data.inlineImages = inlineImageActions(part, node, target);
          const originalCard = target.inspect?.().content;
          const semanticCard = originalCard?.kind && originalCard?.cid && rc().voiceCard?.placementSnapshot;
          if ((group || body || semanticCard) && rc().stickynote) {
            part.data.dragId = registerAction(part.id + '-place', node, async command => {
              if (![command.x, command.y].every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1)) throw new Error('落点无效');
              const x = command.x * innerWidth, y = command.y * innerHeight;
              // 原生正文接管时落点已由 PDFKit 解成页内坐标（command.value）。
              // ⚠ 不能再让网页去解：接管后视口里没有那一页，anchorFromPoint 恒为 null，
              //   表现就是"侧栏生成物拖到页面上放不了"（2026-09-22 实报）。
              let anchor = null;
              const native = command.value;
              if (native) {
                if (!Number.isSafeInteger(native.page) || native.page < 1 ||
                    ![native.x, native.y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) {
                  throw new Error('落点无效');
                }
                anchor = { kind: 'pdf', page: native.page, x: native.x, y: native.y };
              }
              let accepted = false;
              if (group && rc().flashcard?.snapshot && rc().stickynote.placeCardAt) {
                const cards = rc().flashcard.snapshot(group);
                accepted = await rc().stickynote.placeCardAt(x, y, cards, group.__fc.gid, anchor);
              } else if (semanticCard && rc().stickynote.placeHtmlAt) {
                // Native cards have no web card body. Use the complete current
                // original, never a truncated display preview or empty innerHTML.
                const snapshot = rc().voiceCard.placementSnapshot(target.inspect().content);
                accepted = await rc().stickynote.placeHtmlAt(x, y, snapshot, anchor);
              } else if (body && rc().stickynote.placeHtmlAt) {
                accepted = await rc().stickynote.placeHtmlAt(x, y, {
                  content: body.innerHTML, contextText: body.textContent || '', isHtml: true,
                  cid: cardElement?.dataset.vcCid || cardElement?.__vcCard?.cid || '', label: part.title
                }, anchor);
              }
              if (!accepted) throw new Error('卡片位置未保存，请检查落点后重试');
            });
          }
        }
        message.parts = message.parts.filter(part => !part.removed);
        }
      }
      function toolbarActions() {
        const navigation = rc().readerNavigation?.state?.();
        const paging = navigation?.ready ? [{
          id: 'native-page', title: navigation.display + ' / ' + navigation.totalLabel, key: 'page', disabled: false
        }] : [];
        // PDF controls and their actions are owned by Swift. EPUB keeps its
        // visible WebKit document controls; no PDF header scan or click handles.
        if (rc().readerNavigation?.nativeViewport) return paging;
        const root = document.getElementById('header') || document.getElementById('ep-top');
        if (!root) return paging;
        return paging.concat(Array.from(root.querySelectorAll('button')).filter(button => !button.hidden && button.getAttribute('aria-hidden') !== 'true').map((button, index) => ({
          id: registerAction('toolbar-' + (button.id || index), button, () => button.click()),
          key: button.id || '', title: text(button.getAttribute('title') || button.getAttribute('aria-label') || button.textContent, 140).trim() || '阅读操作',
          disabled: !!button.disabled
        })));
      }
      function selectedContext() {
        return { text: text(window.__focusSel?.text, 16000), kind: text(window.__focusSel?.kind, 40) };
      }
      function selectedAttachments() {
        const registry = window.BWReaderRuntime?.contextSelections;
        if (!thread) return [];
        const figures = window.__bwNativeFigureProjection ? (window.__figAttached || []).map(item => ({
          id: scope + ':figure:' + item.token, title: text(item.caption, 160) || '插图 · p' + item.page,
          text: text(item.desc, 180), kind: 'figure',
          removeId: registerAction('figure-remove:' + item.token, thread, () => window.__bwNativeConsumeFigures?.([item.token]))
        })) : [];
        return figures.concat((registry?.snapshot?.({ maxText: 180 }).items || []).map(item => ({
          id: scope + ':context:' + hash(item.id), title: text(item.label, 160) || '已选内容',
          text: text(item.text, 180), kind: text(item.kind, 80),
          removeId: registerAction('context-remove:' + item.id, thread, () => registry.deselect(item.id))
        })));
      }
      function pagePlacements(items) {
        const owner = rc().stickynote;
        if (!nativeMode || legacyVisible || !owner?.nativePlacementState) return [];
        return items.flatMap(item => {
          if (excludedPlacementNodes.has(item.root)) return [];
          // Media/script placements need their own native renderer. Ink uses PencilKit.
          // Keep their originals intact until that renderer is migrated.
          const rich = item.card ? JSON.stringify(item.card.cards) : item.html?.content || '';
          const inline = inlineImageSources(item.card ? item.card.cards.flatMap(card => Object.values(card)) : [item.html?.content]);
          const imagesReady = !/<img\b/i.test(rich) || inline.length > 0 && inline.length <= 64 && inline.every(source => {
            return source.length > 0 && source.length <= 8192 && rc().voiceCard?.mediaRoute?.(source);
          });
          const supported = !!(item.card || item.html) &&
            imagesReady && !/<(?:iframe|video|audio|svg|canvas|script|button|input|select|textarea)\b/i.test(rich);
          item.root.toggleAttribute('data-bw-native-placement', !!supported);
          for (const marker of item.markers || []) marker.node.toggleAttribute('data-bw-native-placement', !!supported);
          // ⚠ `supported` 只该管**卡身能不能原生画**，不该把词错标记一起拖下水。
          //   不支持时原来直接 return []，于是：网页标记留着（看着像旧框）、
          //   原生不画 —— 而原生正文接管后网页那层根本不接触摸，结果就是
          //   **旧框没转成新框，而且点不开**（2026-09-22 用户实报）。
          //   现在：卡身不支持就不交卡身（网页那份继续显示），但**标记照交**，
          //   至少点得开。
          if (!supported && (!item.bound || !item.markers?.length)) return [];
          if (supported && !item.visible && !item.markers?.length) return [];
          const id = 'placement-' + hash(item.generation + ':' + item.id);
          const token = id + '-' + hash(item.version);
          const invoke = async (key, extra = {}) => { const ok = await owner.nativePlacementAction({
            id: item.id, generation: item.generation, version: item.version, key, ...extra
          }); if (!ok) throw new Error('卡片操作未确认'); return ok; };
          const controls = {};
          controls.ink = registerAction(id + '-ink', item.root, command => owner.nativeInkAction({
            id: item.id, generation: item.generation, geometry: command.value.geometry,
            key: command.value.kind, opId: command.value.opId, eventOpId: command.value.eventOpId,
            segments: command.value.segments, aspectRatio: command.value.aspectRatio
          }));
          for (const key of ['anchor', 'collapse', 'expand', 'remove', 'toggleBound', 'trash', 'favorite']) {
            controls[key] = registerAction(token + '-' + key, item.root, () => invoke(key, { confirmed: key === 'remove' }));
          }
          // 换便签色。色板的唯一来源是 rc-stickynote 的 COLORS，这里只转交。
          controls.color = registerAction(token + '-color', item.root, command => {
            if (typeof command.value !== 'string') throw new Error('颜色无效');
            return invoke('color', { value: command.value });
          });
          // 形态循环按钮（圆 / 长条 / 方块）。裁剪规则在 rc-stickynote 那侧统一做。
          controls.form = registerAction(token + '-form', item.root, command => {
            if (!['dot', 'min', 'full'].includes(command.value)) throw new Error('形态无效');
            return invoke('form', { value: command.value });
          });
          controls.move = registerAction(token + '-move', item.root, command => {
            // 原生正文接管时：页码 + 页内坐标 + 原生认好的词（见 rc-stickynote 'move' 的 native 分支）。
            const native = command.value;
            if (native && typeof native === 'object' && Number.isInteger(native.page)) {
              return invoke('move', { native: { page: native.page, x: native.x, y: native.y, bind: native.bind || null } });
            }
            if (![command.x, command.y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) throw new Error('落点无效');
            return invoke('move', { x: command.x * innerWidth, y: command.y * innerHeight });
          });
          controls.resize = registerAction(token + '-resize', item.root, command => {
            if (![command.value?.width, command.value?.height].every(v => Number.isFinite(v) && v > 0 && v <= 1)) throw new Error('尺寸无效');
            return invoke('resize', { width: command.value.width * innerWidth, height: command.value.height * innerHeight });
          });
          const markers = (item.markers || []).map((marker, index) => ({
            id: id + '-marker-' + (marker.index ?? index), kind: marker.kind, number: marker.number,
            rect: { x: marker.rect.x / innerWidth, y: marker.rect.y / innerHeight,
              width: marker.rect.width / innerWidth, height: marker.rect.height / innerHeight } }));
          // 卡身原生画不出来（含 iframe/video/未就绪的图…）就只交标记：网页那份卡身
          // 继续显示，但标记要点得开。
          //
          // ⚠⚠ 这一支原来排在 `const controls = {}` **前面**，却写着 `controls: controls`
          //   —— 同一块作用域里的 const 还在暂时性死区，一读就 ReferenceError，
          //   于是 pagePlacements 整个抛出：**这本书一张页卡都不交**。
          //   表现就是"绑定到元素后卡片就打不开了"（2026-09-22 用户实报）。
          //   现在挪到控件注册之后，toggleBound 是真的。
          if (!supported) {
            return [{ id, noteId: item.id, title: item.html?.label || '学习卡',
              bound: item.bound, collapsed: item.collapsed, visible: false, open: item.open,
              controls: { toggleBound: controls.toggleBound }, parts: [],
              surface: { color: item.color, opacity: item.opacity, blur: item.blur },
              ink: { strokes: [], aspectRatio: 0, geometry: null }, size: null, markers,
              rect: { x: item.rect.x / innerWidth, y: item.rect.y / innerHeight,
                width: item.rect.width / innerWidth, height: item.rect.height / innerHeight } }];
          }
          const parts = item.card
            ? projectPart({ kind: 'cards', cards: item.card.cards, gid: item.card.gid }, id, item.root, '')
            : [{ id: id + '-html', kind: 'general', title: item.html.label || '卡片', text: '', status: 'saved',
                data: { text: item.html.content, format: item.html.isHtml ? 'html' : 'text' } }];
          if (item.html) {
            parts[0].actionId = registerAction(id + '-html', item.root, () => {});
            actions.get(parts[0].actionId).inspect = () => ({ kind: 'general', title: parts[0].title, content: item.html });
          }
          // Reuse the same learning-card and selection services as sidebar cards.
          liveArtifacts([{ parts }]);
          parts.forEach(part => { part.data.dragId = controls.move; });
          // noteId：便签自己的 id。原生正文用它去 PDFKit 解锚（卡位、锁定框、点框开卡）；
          // 上面那个 id 是按代际哈希出来的界面身份，**跟便签 id 对不上**。
          return [{ id, noteId: item.id, title: item.html?.label || (item.card?.cards.length > 1 ? '学习卡组' : '学习卡'),
            bound: item.bound, collapsed: item.collapsed, visible: item.visible, open: item.open, controls, parts,
            // 卡面本身的用色与磨砂强度（原版 applyColor 的同一组值）。
            surface: { color: item.color, opacity: item.opacity, blur: item.blur },
            form: item.form, pinned: item.pinned, palette: item.palette, tone: item.tone || '',
            ink: { strokes: item.strokes, aspectRatio: item.iar, geometry: item.inkGeometry },
            size: item.presentationSize ? { width: item.presentationSize.w / innerWidth, height: item.presentationSize.h / innerHeight } : null,
            markers,
            rect: { x: item.rect.x / innerWidth, y: item.rect.y / innerHeight,
              width: item.rect.width / innerWidth, height: item.rect.height / innerHeight } }];
        });
      }
      // 原生正文接管 PDF 时的页卡：**只按便签数据交**，网页一张卡都不挂。
      //
      // ⚠ 以前交的是网页挂出来的那几张（nativePlacementState 读 DOM），而网页只挂它自己
      //   渲染到的那几页 —— 跟原生正文显示的页永远不同步。2026-09-23 那一串
      //   "刚建的能开、翻页回来就打不开 / 框与词分离 / 拖到别的词上"全出自这里。
      //   用户："不能就把网页的渲染直接彻底删掉么""所有旧的渲染在有新的功能代替后
      //   都应该把旧的给去掉"。
      //
      // 动作属主是便签本身（它还在就算"还连着"）；位置、开合全由原生按 PDFKit 管，
      // 这里只交内容与控件。控件键名与网页那版一致，原生卡片视图不用分两套。
      function notePlacements() {
        const sticky = rc().stickynote;
        if (!nativeMode || legacyVisible || !sticky?.nativeNoteCards) return [];
        // ⚠ 导航状态里当前页叫 position（readerNavigation.state），不叫 page。
        const nav = rc().readerNavigation?.state?.() || {};
        const page = Number(nav.position);
        // 当前页前后几页够了：这份快照频繁序列化，整本书的卡片 HTML 不该每次都跟着走。
        const pages = Number.isInteger(page) && page > 0
          ? Array.from({ length: 9 }, (_, i) => page - 4 + i).filter(n => n > 0) : null;
        const generation = sticky.nativeGeneration?.();
        return sticky.nativeNoteCards(pages, {excludeHTML:window.__BW_NATIVE_HTML_NOTES__ === true}).flatMap(item => {
          if (window.__BW_NATIVE_HTML_NOTES__ === true && item.html && !item.card) return [];
          const owner = { get isConnected() { return sticky.nativeGeneration?.() === generation && !!sticky.nativeHasNote?.(item.id); } };
          const id = 'note-' + hash(item.id);
          const token = id + '-' + hash(item.version);
          const label = item.html?.label || (item.card?.cards?.length > 1 ? '学习卡组' : '学习卡');
          let parts;
          if (item.card) {
            parts = projectPart({ kind: 'cards', cards: item.card.cards || [], gid: item.card.gid }, id, owner, '');
          } else {
            const part = { id: id + '-html', kind: 'general', title: label, text: '', status: 'saved',
              data: { text: item.html.content || '', format: item.html.isHtml ? 'html' : 'text' } };
            part.actionId = registerAction(part.id, owner, () => {});
            const target = actions.get(part.actionId);
            target.inspect = () => ({ kind: 'general', title: label, content: item.html });
            if (typeof window.__setFocusSel === 'function') part.data.selectId = selectAction(part.id, owner);
            part.data.inlineImages = inlineImageActions(part, owner, target);
            // 长按＝带入/移出对话（原版 pinBind，登记表同一份记录形状）。
            part.data.pinned = !!sticky.nativeCardContextSelected?.(item.id);
            part.data.pinId = registerAction(part.id + '-pin', owner, () => {
              if (sticky.nativeToggleCardContext(item.id) == null) throw new Error('这张卡不能带入对话');
              schedule();
            });
            parts = [part];
          }
          const update = async changes => {
            if (!(await sticky.nativeUpdateNote(item.id, changes))) throw new Error('卡片没能保存');
            return true;
          };
          const remove = async () => {
            if (!(await sticky.nativeDeleteNote(item.id))) throw new Error('卡片没能删除');
            return true;
          };
          const controls = {};
          // 移动：页码 + 页内坐标由原生按 PDFKit 算好。钉词的卡连同原生认好的词一起改绑；
          // 自由卡**不会**因为拖了一下就变成钉词卡（原版同规则）。
          controls.move = registerAction(token + '-move', owner, command => {
            const v = command.value;
            if (!v || !Number.isInteger(v.page)) throw new Error('落点不在页面内');
            return update({ anchor: { page: v.page, x: v.x, y: v.y }, bind: item.bound ? (v.bind || null) : null });
          });
          // 锚定到正文：原生认好的词（卡角最近的词）交过来，首次绑。
          controls.anchor = registerAction(token + '-anchor', owner, command => {
            const v = command.value;
            if (!v || !v.bind) throw new Error('请先将卡片移到正文附近');
            return update({ bind: v.bind });
          });
          controls.form = registerAction(token + '-form', owner, command => {
            if (!['dot', 'min', 'full'].includes(command.value)) throw new Error('形态无效');
            return update({ form: command.value });
          });
          controls.collapse = registerAction(token + '-collapse', owner, () => update({ form: 'dot' }));
          controls.expand = registerAction(token + '-expand', owner, () => update({ form: 'full' }));
          // 尺寸按卡片自身单位（原生已按页宽 / base_w 换算好）。
          controls.resize = registerAction(token + '-resize', owner, command => {
            const v = command.value;
            if (!v || !(Number(v.w) > 0) || !(Number(v.h) > 0)) throw new Error('尺寸无效');
            return update({ w: Number(v.w), h: Number(v.h) });
          });
          controls.remove = registerAction(token + '-remove', owner, remove);
          controls.trash = registerAction(token + '-trash', owner, remove);
          controls.favorite = registerAction(token + '-favorite', owner, async () => {
            if (!(await sticky.nativeFavoriteNote(item.id))) throw new Error('这张卡没能加入收藏夹');
            return true;
          });
          controls.ink = registerAction(id + '-ink', owner, command => sticky.nativeInkAction({
            id: item.id, generation, geometry: command.value.geometry,
            key: command.value.kind, opId: command.value.opId, eventOpId: command.value.eventOpId,
            segments: command.value.segments, aspectRatio: command.value.aspectRatio
          }));
          parts.forEach(part => { part.data.dragId = controls.move; });
          return [{ id, noteId: item.id, source: 'note', title: label,
            bound: item.bound, collapsed: item.form !== 'full', visible: true, open: false, controls, parts,
            form: item.form, pinned: item.pinned, tone: item.tone || '',
            ink: { strokes: item.strokes, aspectRatio: item.iar, geometry: item.inkGeometry },
            size: null, markers: [], rect: { x: 0, y: 0, width: 0, height: 0 } }];
        });
      }
      function floatingPlacements(items) {
        const owner = rc().voiceCard;
        if (!nativeMode || legacyVisible || isOpen() || !owner?.nativeFloatingState) return [];
        return items.flatMap((item, index) => {
          if (excludedPlacementNodes.has(item.root)) return [];
          const group = flashGroup(item.root), structured = item.root.__vcCard;
          const supported = !!group || structured && ['fact', 'general', 'weather', 'news', 'images', 'videos'].includes(structured.kind) ||
            typeof item.raw === 'string' && !/<(?:iframe|video|audio|img|svg|canvas|script|button|input|select|textarea)\b/i.test(item.raw);
          item.root.toggleAttribute('data-bw-native-placement', supported);
          if (!supported) return [];
          const rect = item.root.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) return [];
          const id = 'floating-' + hash(item.cid || messageID(item.root, index));
          let parts;
          if (group) parts = projectPart({ kind: 'cards', cards: group.__fc.cards, gid: group.__fc.gid }, id, item.root, '');
          else if (structured) parts = projectPart({ kind: 'card', card: structured }, id, item.root, '');
          else {
            const part = artifact(id, item.root, item.title);
            part.kind = 'general'; part.data = { text: item.raw, format: item.isHtml ? 'html' : 'text' };
            actions.get(part.actionId).inspect = () => ({ kind: 'general', title: item.title, content: item.raw });
            parts = [part];
          }
          liveArtifacts([{ parts }]);
          const controls = {};
          for (const key of ['expand', 'collapse', 'remove', 'move', 'touch']) {
            controls[key] = registerAction(id + '-' + key, item.root, command => {
              if (key === 'move' && ![command.x, command.y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) throw new Error('落点无效');
              return owner.nativeFloatingAction(item.record, key, { x: command.x * innerWidth, y: command.y * innerHeight });
            });
          }
          parts.forEach(part => { part.data.dragId = controls.move; });
          return [{ id, title: item.title, floating: true, bound: false,
            collapsed: item.root.classList.contains('vc-dot') || item.root.classList.contains('vc-min'), controls, parts,
            rect: { x: rect.left / innerWidth, y: rect.top / innerHeight, width: rect.width / innerWidth, height: rect.height / innerHeight } }];
        });
      }
      // 地址里的 ?page= 随翻页被 replaceState 改掉（reader.js 四处，都只改 page），它不是身份。
      // ⚠ 2026-09-23：把它算进 scope 的后果 —— 原生正文每翻一页 scope 就变：
      //   ① 原生视口的接管随即被判失效，页码再也不同步给网页、网页的翻页请求全被拒；
      //   ② 选区与已登记的操作每翻一页被清一次（"它看不到我的选中"）；
      //   ③ 页卡按当前页取，网页以为还停在第一页，于是翻过去的卡都"没同步"。
      //   书的身份另有 navigationID（每次页面加载一个）与 pathname。
      function identitySearch() {
        try {
          const params = new URLSearchParams(location.search);
          params.delete('page');
          return params.toString();
        } catch (_) { return location.search; }
      }
      function getScopeKey() {
        let identity = '', history = '', mode = pane()?.dataset.assistantMode || 'normal';
        try { const state = account()?.snapshot(); identity = [state?.contextId || '', state?.namespace || '', state?.generation ?? '', state?.active || false].join(':'); } catch (_) {}
        try { history = String(window.__asstHistUrl?.() || ''); } catch (_) {}
        // Scope is opaque. Never send file paths, account namespaces, routes or credentials.
        return [navigationID, location.pathname, identitySearch(), identity, history, mode].join('|');
      }
      function getPlacementScopeKey() {
        let identity = '';
        try { const state = account()?.snapshot(); identity = [state?.contextId || '', state?.namespace || '', state?.generation ?? '', state?.active || false].join(':'); } catch (_) {}
        return [navigationID, location.pathname, identitySearch(), identity].join('|');
      }
      function capabilities() {
        // ⚠ 没有 'showLegacy'。旧网页界面不再是用户能主动进去的地方
        //   （2026-09-22 用户："我要的是把旧的内容用原生功能直接代替后把原版删除"）。
        //   'hideLegacy' 留着：还没原生化的那几个面（下面几处 setLegacy(true) 的
        //   fallback）掉进去以后，得有路回来。
        const out = ['refresh', 'snapshot', 'hideLegacy', 'liveAction'];
        if (rc().voiceCard?.favorite?.load) out.push('favoritesList', 'favoritesPlace', 'favoritesDelete',
          'favoritesTrash', 'favoritesRestore', 'favoritesPin');
        if (typeof window.__clearFocusSel === 'function') out.push('clearSelection');
        if (typeof drawer()?.setTab === 'function') out.push('toggleAssistant');
        if (typeof window.__asstSend === 'function') out.push('send');
        if (rc().assistant?.conversationService?.stop) out.push('stop');
        if (rc().assistant?.conversationService?.clear) out.push('clearConversation');
        if (rc().turnCard?.performOperation) out.push('operationAction');
        if (conversationMode() === 'normal' && rc().voicecall?.canStartNewTopic?.()) out.push('newConversation');
        if (typeof rc().assistant?.openModelSettings === 'function') out.push('openModels');
        if (rc().assistant?.settingsService) out.push('nativeSettings');
        if (rc().readerSearch?.search) out.push('nativeSearch', 'openSearch');
        if (rc().readerNavigation?.state && rc().readerNavigation?.perform) out.push('nativeNavigation', 'openNavigation');
        if (rc().readerPreferences?.state && rc().readerPreferences?.perform) out.push('nativeReadingSettings', 'openSettings');
        if (typeof window.openSettings === 'function' || document.getElementById('ep-set-btn')) out.push('openSettings');
        if (rc().review?.performNativeInteraction) out.push('openReview', 'reviewAction');
        else if (document.getElementById('asst-review-toggle')) out.push('openReview');
        if (typeof window.openSearch === 'function' || document.getElementById('ep-search-btn')) out.push('openSearch');
        if (rc().readerTOC?.read && rc().readerTOC?.jump) out.push('nativeTOC', 'openTOC');
        if (rc().voicecall?.requestCall) out.push('toggleVoice', 'toggleComputerVoice');
        if (actions.size) out.push('inspectArtifact');
        return out;
      }
      // ⚠ 条件里**去掉了 `isOpen()`**（2026-09-22 修「点侧栏按钮就崩」）。
      //   原来是"网页抽屉已经开着"才压住它，于是每次开侧栏的顺序必然是
      //   **先让网页抽屉按自己的样式开出来，再由下一次快照去压** ——
      //   ① 用户看见**旧版侧栏闪一下**；
      //   ② `body.ep-side-open` 给 `#ep-viewer` 上 `margin-right` 且带 .4s 过渡
      //      → **整本 EPUB 连续重排 400ms**，同时抽屉的 backdrop-filter 开始合成；
      //      快照落地后这条 CSS 再把 margin 压回 0 → **又一次整本重排**。
      //      大书上这一串足以把 WebContent 进程顶掉，表现就是"点一下就崩"
      //      （其实是渲染进程被杀、App 自动重载，而它一声不响）。
      //   改成只看"这个面是不是归原生管"（tab 是 asst）：抽屉从第一帧起就隐藏、
      //   margin 恒为 0，开关它不再改变任何布局，也从不绘制。
      // ⚠ 保留 tab 判断**不是**多余的：网页自己也会开抽屉到 grammar/kg/vocab
      //   （epub-html.js 的 onOpenPanel 等），那些面原生没接管，一并压住就是
      //   "点了什么都不出来"。只压归原生管的那一个。
      /** 助手这一面归不归原生管。归就由原生记开合，网页抽屉一步都不动。 */
      function nativeOwnsAssistant() { return nativeMode && !legacyVisible; }

      function applyVisualMode(assistantOverride) {
        const root = document.documentElement;
        const owns = nativeMode && !legacyVisible &&
          (assistantOverride === true ||
           (assistantOverride !== false && activeTab() === 'asst'));
        root.classList.toggle('bw-native-navigation', nativeMode);
        // 网页外壳的"放回来"开关。⚠ 默认（没有这个类）＝**不存在** —— 那条样式
        //   在 atDocumentStart 就落好了，不依赖这个脚本、也不依赖 setNativeMode
        //   送没送到。这里只负责"该放回来的时候放回来"。
        // ⚠ **不知道就不动**（2026-09-22 修回归）。
        //   documentStart 已经把网页外壳默认藏掉了；而本函数会在
        //   `setNativeMode` 还没送到的时候先跑一次（snapshot 里就会调），
        //   那时 nativeMode 还是初值 false —— 若照字面写，就把外壳又放了出来，
        //   用户看到的就是**两条顶栏并排**。我自己写的"默认不存在"，
        //   被自己这行撤销了。所以只有**真得到过答案**时才能碰这个类。
        if (nativeModeKnown) {
          root.classList.toggle('bw-native-legacy-chrome', !nativeMode || legacyVisible);
        }
        root.classList.toggle('bw-native-page-cards', nativeMode && !legacyVisible);
        root.classList.toggle('bw-native-conversation-active', owns);
        // ⚠ 真正的修复在这一行，上面那几个 class 只是第二道。无头＝抽屉退成纯状态：
        //   不加 body 类、不挤压正文、不重排、不滑入。CSS 盖住只解决"看不看得见"，
        //   解决不了"开一次抽屉整本书重排两轮"——那才是把渲染进程顶掉的东西。
        try { drawer()?.setHeadless?.(nativeMode && !legacyVisible, 'asst'); } catch (_) {}
        // 告诉网页层“助手开没开”。
        // ⚠ 网页那边的 `__asstOpen()` 原本看的是抽屉有没有 open 类，
        //   而原生接管后抽屉永远不再被打开 → 它恒为 false →
        //   `__setFocusSel` 恒短路 → **选中永远钉不进对话，AI 看不到用户选了什么**。
        //   没接管时置 undefined，让网页自己判 —— 不拿原生的“没开”去盖网页的“开了”。
        //   ⚠⚠ 必须写在 **window** 上：`__asstOpen()` 读的是 window.__bwNativeAssistantOpen。
        //   这里原来写成 `root.`（= document.documentElement），于是它一次都没生效 ——
        //   侧栏明明开着，选中的文字仍然进不了对话，输入框上方永远空着（2026-09-23 实报）。
        try {
          if (nativeOwnsAssistant()) window.__bwNativeAssistantOpen = nativeAssistantOpen;
          else delete window.__bwNativeAssistantOpen;
        } catch (_) {}
      }
      function setLegacy(visible) {
        legacyVisible = !!visible;
        if (visible) drawer()?.open('asst');
        applyVisualMode(); schedule();
      }
      function setNativeMode(enabled) {
        nativeMode = !!enabled;
        nativeModeKnown = true;
        rc().turnCard?.setNativePresentation?.(nativeMode);
        rc().flashcard?.setNativePresentation?.(nativeMode);
        applyVisualMode(); schedule();
        return { ok: true };
      }
      function snapshot() {
        timer = null;
        if (suspended) return;
        discover();
        const nextPlacementKey = getPlacementScopeKey();
        if (nextPlacementKey !== placementScopeKey) {
          if (placementScopeKey) previousPlacementNodes.forEach(node => excludedPlacementNodes.add(node));
          placementScopeKey = nextPlacementKey;
        }
        previousPlacementNodes = [];
        const nextKey = getScopeKey();
        if (nextKey !== scopeKey) {
          // An account/book switch can precede asynchronous history replacement.
          // Do not relabel the previous DOM as the new account's conversation.
          scopeKey = nextKey; scope = 'reader-' + hash(nextKey); actions.clear(); lastSignature = '';
          watchedCardGroups = [];
          window.__bwNativeSelection = null;
          nativePageSelectionSequence = 0;
          settingsModels = null; settingsVoice = null;
          searchController?.abort(); searchResults.clear(); searchQuery = ''; searchSequence++;
          tocController?.abort(); tocEntries.clear(); tocOwner = null; tocSequence++;
        }
        // 迁出 P4：侧栏消息由原生对话流给，这里不再投影消息；动作表每次随页卡/工具栏重建。
        actions = new Map();
        // 原生正文接管 PDF 时，页卡由原生按便签数据自己画 —— 网页不挂、这里也不交。
        // （交了就是两份：一份网页按它自己的页挂出来，一份原生按 PDFKit 画，永远对不上。）
        const nativePageCards = !!window.RC?.readerNavigation?.nativeViewport;
        if (nativePageCards) { try { rc().stickynote?.nativeReleaseWebPageCards?.(); } catch (_) {} }
        const pageStates = nativePageCards ? [] : (rc().stickynote?.nativePlacementState?.() || []);
        const floatingStates = rc().voiceCard?.nativeFloatingState?.() || [];
        const placements = [...(nativePageCards ? notePlacements() : pagePlacements(pageStates)),
          ...floatingPlacements(floatingStates)];
        previousPlacementNodes = [...pageStates, ...floatingStates].map(item => item.root);
        const readingTools = toolbarActions();
        const attachments = selectedAttachments();
        const review = conversationMode() === 'review' ? rc().review?.presentationState?.() || null : null;
        if (review) review.contextKey = review.lease || hash(review.contextKey);
        const payload = { version: 1, scope, revision: 0, title: text(document.title, 160) || '阅读助手', ready: isReady(), busy: isBusy(),
          // ⚠ `selection`（来自 __focusSel）**只在助手侧栏开着时才有值** ——
          //   __setFocusSel 第一行就是 `if (!window.__asstOpen()) return;`。
          //   所以选区操作条不能读它：侧栏关着的时候条永远不出现。
          //   readerSelection 是独立的一份，直接问阅读器当前选中了什么。
          legacyVisible, selection: selectedContext(),
          readerSelection: (typeof window.__bwReaderEpubSelection === 'function'
            ? (window.__bwReaderEpubSelection() || { text: '' }) : { text: '' }),
          attachments, readingTools, navigation: rc().readerNavigation?.state?.() || {}, review, placements, captions: captionState(),
          nativePinnedCards: (window.BWReaderRuntime?.contextSelections?.snapshot?.({maxText:0})?.items || [])
            .filter(item => item.kind === 'card' && String(item.id).startsWith('card:')).map(item => String(item.id).slice(5)),
          favoritesCount: (() => { try { return Number(rc().voiceCard?.favorite?.count?.()) || 0; } catch (_) { return 0; } })(),
          sidebarOpen: nativeOwnsAssistant() ? nativeAssistantOpen : (isOpen() && activeTab() === 'asst'), conversationMode: conversationMode(), voice: voiceState(),
          cardInputs: cardInputs(), capabilities: capabilities() };
        const signature = JSON.stringify(payload);
        if (signature !== lastSignature) {
          lastSignature = signature; payload.revision = ++revision;
          // ⚠ 量一下这份快照有多大。理由很具体：这里每 60ms 就可能把**整段对话**
          //   （所有消息 + 卡片的完整 HTML）序列化一遍 —— schedule() 挂在 scroll /
          //   pointerup / resize 和四个 MutationObserver 上，而滚动时消息根本没变。
          //   对话越长这份字符串越大，于是"用着用着就崩"和"点什么都崩"是同一件事。
          //   是不是这样，不能靠猜：把字节数报上去，崩的时候跟着现场一起送出来。
          //   （迁出 P4 起快照里已经没有消息，这个数应当稳定在几 KB。）
          payload.payloadBytes = signature.length;
          try { handler.postMessage(payload); } catch (error) {
            lastSignature = '';
            try { window.dlog?.('[对话] 快照送不出去：' + (error && error.message || error)); } catch (_) {}
          }
        }
      }
      function schedule() {
        if (!suspended && timer == null) timer = setTimeout(snapshot, 60);
      }
      function scheduleMessages() { schedule(); }
      // 迁出 P4：对话流里的学习卡（gid + 序号）要的卡面、状态与操作仍由 rc-flashcard 给 ——
      // 原生把用到的卡组告诉这里（watchCards），快照按卡组带上 presentationInput，原生并进消息。
      function cardInputs() {
        const result = {};
        for (const gid of watchedCardGroups) {
          const group = flashGroup(null, gid);
          if (!group?.__fc || !Array.isArray(group.__fc.cards)) continue;
          result[gid] = { idx: group.__fc.idx, cards: group.__fc.cards.map((_, index) => {
            try { return rc().flashcard?.presentationInput?.(group, index) || null; } catch (_) { return null; }
          }) };
        }
        return result;
      }
      function watchCards(gids) {
        const next = Array.isArray(gids) ? [...new Set(gids.filter(gid => typeof gid === 'string' && gid && gid.length <= 160))].slice(0, 64) : [];
        if (next.length === watchedCardGroups.length && next.every((gid, i) => gid === watchedCardGroups[i])) return { ok: true };
        watchedCardGroups = next; schedule();
        return { ok: true };
      }
      // 选区变化不改 DOM，所以不会触发那些 observer —— 不显式听一下的话，
      // 选区操作条要等到别的什么事发生才出现。
      document.addEventListener('selectionchange', schedule, { passive: true });
      function wrapNotifications(owner, names) {
        if (!owner) return;
        let installed = hooked.get(owner);
        if (!installed) { installed = new Set(); hooked.set(owner, installed); }
        names.forEach(name => {
          if (installed.has(name) || typeof owner[name] !== 'function') return;
          const original = owner[name];
          owner[name] = function (...args) {
            try { const result = original.apply(this, args); if (result?.then) result.then(scheduleMessages, scheduleMessages); return result; }
            finally { scheduleMessages(); }
          };
          installed.add(name);
        });
      }
      /// 底部字幕条（#vc-cap）的内容。原生接管后网页层透明，这条字幕看不见 ——
      /// 2026-09-23 用户："侧边栏关闭时 ai 回复的流式字幕没有正确显示"。
      function captionState() {
        const cap = document.getElementById('vc-cap');
        let enabled = true;
        try { enabled = localStorage.getItem('rc-voice-sub') !== '0'; } catch (_) {}
        if (!cap || !cap.classList.contains('on')) return { enabled, on: false, lines: [] };
        const lines = Array.from(cap.children).slice(-4).map(node => {
          const cls = node.classList;
          const kind = cls.contains('vc-cap-st') ? (cls.contains('vc-st-err') ? 'error' : (cls.contains('vc-st-ok') ? 'ok' : 'status'))
            : cls.contains('vc-cap-wait') ? 'wait' : 'line';
          return { kind, user: cls.contains('vc-cap-u'), previous: cls.contains('vc-cap-prev'),
            text: text(node.textContent, 600) };
        }).filter(line => line.kind === 'wait' || line.text.trim());
        return { enabled, on: true, lines };
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
            threadObserver = new MutationObserver(scheduleMessages);
            threadObserver.observe(thread, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'hidden', 'data-turn-id', 'data-turn', 'disabled'] });
          }
        }
        const bar = rc().readerNavigation?.nativeViewport ? null : document.getElementById('header') || document.getElementById('ep-top');
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
        const registry = window.BWReaderRuntime?.contextSelections;
        if (registry !== selectionRegistry) {
          selectionSubscription?.(); selectionRegistry = registry;
          selectionSubscription = registry?.subscribe?.(scheduleMessages) || null;
        }
        const currentControls = document.getElementById('asst-input');
        if (currentControls !== controls) {
          controlsObserver?.disconnect(); controls = currentControls;
          if (controls) { controlsObserver = new MutationObserver(schedule); controlsObserver.observe(controls, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'disabled', 'title', 'placeholder'] }); }
        }
        const currentCaption = document.getElementById('vc-cap');
        if (currentCaption !== captionElement) {
          captionObserver?.disconnect(); captionElement = currentCaption;
          if (captionElement) {
            captionObserver = new MutationObserver(schedule);
            captionObserver.observe(captionElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'] });
          }
        }
        observeDrawer();
        applyVisualMode();
        wrapNotifications(rc().turnCard, ['addPart', 'draftText', 'freezeDraft', 'reconcile', 'cliPart', 'busy', 'idle', 'status', 'progress', 'drop', 'rename', 'reset']);
        wrapNotifications(rc().stickynote, ['placeCardAt', 'placeHtmlAt', 'nativePlacementAction', 'mountPending', 'repositionAll', 'loadAll',
          'nativeUpdateNote', 'nativeDeleteNote', 'nativeFavoriteNote', 'nativeToggleCardContext', 'nativeInkAction']);
        wrapNotifications(rc().voiceCard, ['nativeFloatingAction']);
        if (!accountSubscription && account()?.subscribe) accountSubscription = account().subscribe(schedule);
      }
      async function perform(command) {
        if (suspended) return { ok: false, error: '页面已离开，请在当前页面操作' };
        if (!command || typeof command !== 'object' || Array.isArray(command)) return { ok: false, error: '无效操作' };
        if (getScopeKey() !== scopeKey) snapshot();
        if (command.scope && command.scope !== scope) return { ok: false, error: '会话已切换，请重新操作' };
        const parameterKeys = ['action', 'scope', 'text', 'actionId', 'x', 'y'];
        if (command.action === 'settingsRead') parameterKeys.push('section');
        if (['nativePageSelection', 'nativeSelectionHighlight', 'nativeSelectionLookup',
             'nativeVocabMark',
             'nativeFigureAttach', 'nativeGrammar',
             'nativeHighlightEdit', 'nativePhraseFav',
             'nativeCreateNote', 'nativeOcrSelection',
             'nativeEpubHighlight', 'favoritesPlace', 'favoritesDelete', 'favoritesRestore',
             'favoritesPin'].includes(command.action)) parameterKeys.push('value');
        if (command.action === 'readingSettingsWrite') parameterKeys.push('key', 'value');
        if (command.action === 'settingsWrite') parameterKeys.push('section', 'value', 'key', 'device', 'op', 'name');
        if (command.action === 'reviewAction' || command.action === 'navigationAction' || command.action === 'liveAction' || command.action === 'clearConversation') parameterKeys.push('value');
        if (Object.keys(command).some(key => !parameterKeys.includes(key))) return { ok: false, error: '不支持的操作参数' };
        const action = command.action;
        try {
          if (action === 'send') {
            if (!isReady()) return { ok: false, error: '阅读器尚未准备好' };
            if (isBusy()) return { ok: false, error: '上一条消息仍在处理中' };
            if (typeof command.text !== 'string' || !command.text.trim() || command.text.length > 32000) return { ok: false, error: '消息为空或过长' };
            // Commit selection intent and apply native expiry before accepting
            // the composer text, including the active-voice shortcut.
            const sendingScope = scope;
            await window.BWReaderRuntime?.contextSelections?.settle?.();
            if (sendingScope !== scope || getScopeKey() !== scopeKey || isBusy()) return {ok:false, error:'会话已切换或仍在处理中'};
            const voice = voiceState();
            if (voice.busy) return { ok: false, error: '语音正在连接，请稍候再发送' };
            if (voice.active && conversationMode() === 'normal') {
              // Never fall through into a separate text conversation when the
              // visible current call cannot accept its typed input.
              if (typeof window.__vcSendText !== 'function' || window.__vcSendText(command.text.trim()) !== true) return { ok: false, error: '当前语音未接收文字，请稍后重试' };
            } else {
              if (typeof window.__asstSendAccepted !== 'function') return {ok:false,error:'发送入口尚未准备好'};
              const receipt = await window.__asstSendAccepted(command.text.trim());
              if (receipt?.ok !== true) return {ok:false,error:receipt?.error || '消息未被接收'};
            }
          } else if (action === 'toggleAssistant') {
            if (!drawer()?.setTab) return { ok: false, error: '侧栏尚未准备好' };
            // 原生接管时：**只切 tab，不开抽屉**。切 tab 是为了让助手那一面的
            // 内容挂上去（原生从同一棵 DOM 读），它不改布局、不滑入、不挤压正文。
            if (nativeOwnsAssistant()) {
              nativeAssistantOpen = !nativeAssistantOpen;
              if (nativeAssistantOpen) { try { drawer().setTab('asst'); } catch (_) {} }
              // 打开侧栏 = 要看最新记录。网页抽屉的 onDrawerTabChanged 在原生接管后永远
              // 不会触发（抽屉不再真的打开），所以由这里补那一次刷新。
              if (nativeAssistantOpen) {
                applyVisualMode();
                try { rc().assistant?.onDrawerTabChanged?.('asst', true); } catch (_) {}
              }
              // 立刻同步，不等下一次快照 —— 用户开完侧栏马上选词是常态。
              applyVisualMode();
              schedule();
              return { ok: true };
            }
            if (isOpen() && activeTab() === 'asst') { drawer().close(); legacyVisible = false; }
            // ⚠ **先压住再开**，顺序不能反：反过来就是"网页抽屉先开出来、下一次
            //   快照才去压"，那一瞬间正是旧侧栏闪一下 + 整本重排的来源。
            //   这里传 true 是因为此刻 DOM 里的 activeTab 还没切到 asst
            //   （setTab 在 open() 内部才跑）。
            else { applyVisualMode(true); drawer().open('asst'); }
          } else if (action === 'anchorPreview') {
            // 拖卡时问网页层：这个点会锁到哪里。
            // ⚠ 只取**数据**，画由原生做 —— 原生正文接管后网页那层不可见，
            //   网页自己画的锁定反馈用户根本看不到。判据仍然走网页那一份
            //   （与 anchorFx 共用），否则会出现“预览说钉这儿、松手却钉别处”。
            const owner = rc().stickynote;
            if (!owner?.nativeAnchorPreview) return { ok: false, error: '锁定预览尚未就绪' };
            if (![command.x, command.y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) {
              return { ok: false, error: '落点无效' };
            }
            const preview = owner.nativeAnchorPreview(command.x * innerWidth, command.y * innerHeight);
            return { ok: true, value: preview ? {
              kind: preview.kind,
              y: typeof preview.y === 'number' ? preview.y / innerHeight : null,
              rects: (preview.rects || []).map(rect => ({
                x: rect.x / innerWidth, y: rect.y / innerHeight,
                width: rect.width / innerWidth, height: rect.height / innerHeight }))
            } : null };
          } else if (action === 'clearSelection') {
            if (typeof window.__clearFocusSel !== 'function') return { ok: false, error: '选区尚未准备好' };
            window.__clearFocusSel();
            window.__bwNativeSelection = null;
          } else if (action === 'liveAction') {
            const target = actions.get(command.actionId);
            if (!command.scope || !target || target.scope !== scope || !target.node?.isConnected) return { ok: false, error: '内容已更新，请重试' };
            await target.run(command);
          } else if (action === 'stop') {
            if (rc().assistant?.conversationService?.stop?.() !== true) return { ok: false, error: '当前没有可停止的文字回复' };
          } else if (action === 'operationAction') {
            if (!rc().turnCard?.performOperation) return {ok:false,error:'操作记录尚未就绪'};
            await rc().turnCard.performOperation(command.value);
          } else if (action === 'clearConversation') {
            if (!rc().assistant?.conversationService?.clear) return { ok: false, error: '对话尚未准备好' };
            await rc().assistant.conversationService.clear(command.value);
          } else if (action === 'newConversation') {
            if (conversationMode() !== 'normal' || !rc().voicecall?.canStartNewTopic?.()) return { ok: false, error: '当前通话不支持新话题' };
            rc().voicecall.startNewTopic();
          } else if (action === 'toggleVoice' || action === 'toggleComputerVoice') {
            if (!rc().voicecall?.requestCall) return {ok:false,error:'语音入口尚未准备好'};
            rc().voicecall.requestCall(action === 'toggleVoice' ? 'realtime' : 'computer');
          } else if (action === 'hideLegacy') {
            setLegacy(false);
          } else if (action === 'refresh') {
            rc().assistant?.reloadHistory?.();
          } else if (action === 'snapshot') {
            // 只要一份新快照（末尾统一 schedule）；不重载对话历史。
          } else if (action === 'nativePageSelection') {
            const value = command.value;
            if (!value || !Number.isSafeInteger(value.sequence) || value.sequence <= nativePageSelectionSequence ||
                !Array.isArray(value.pages) || value.pages.length > 32 || typeof window.__setFocusSel !== 'function') return { ok: false, error: '选区已更新，请重新选择' };
            if (value.pages.some(page => !Number.isSafeInteger(page.page) || page.page < 1 || typeof page.text !== 'string' ||
                !Array.isArray(page.indexes) || page.indexes.length > 32000 || page.indexes.some(i => !Number.isSafeInteger(i) || i < 0) ||
                (page.sentence != null && (typeof page.sentence !== 'string' || page.sentence.length > 600)) ||
                typeof page.geometryDigest !== 'string' || !/^[a-f0-9]{64}$/i.test(page.contentSHA256))) return { ok: false, error: '选区数据无效' };
            const selection = value.pages.map(page => page.text).join('\n').trim();
            if (selection.length > 16000) return { ok: false, error: '选区过长，请缩小范围' };
            nativePageSelectionSequence = value.sequence;
            // AI 读的阅读快照（通话里的语音 AI、后台 AI 都读它）—— 选中与清空都要报，
            // 与侧栏开没开无关。⚠ 以前只写了下面几个全局量，快照里从来没有原生选区。
            try {
              window.__bwReportNativeSelection?.(selection,
                value.pages.length === 1 ? (value.pages[0].sentence || '') : '');
            } catch (_) {}
            if (!selection) {
              if (window.__bwNativeSelection?.owner === 'native-pdf') window.__bwNativeSelection.active = false;
            } else {
              window.__bwNativeSelection = { text: selection, active: true, owner: 'native-pdf', scope, pages: value.pages };
              window.__lastSelMeta = { page: value.pages[0].page, t: Date.now() };
              window.__lastSelSentence = value.pages.length === 1 ? (value.pages[0].sentence || '') : '';
              // 钉进侧栏对话（输入框上方那条）：原版规定侧栏没开时不钉，那不是失败。
              window.__setFocusSel(selection, 'text');
              const pinned = typeof window.__asstOpen === 'function' ? window.__asstOpen() : true;
              if (pinned && window.__focusSel?.text !== selection) return { ok: false, error: '选区暂未进入对话，请重试' };
            }
          } else if (action === 'nativeSelectionHighlight') {
            // 原生阅读区的划线。
            //
            // ⚠ 这里**不再走 __bwReaderHighlightExactText**（它要先
            //   `_pdfExactTextPage` 把那一页在网页里渲出来取 __charBoxes，
            //   `dataset.loaded === '1'` 是硬条件）—— 那正是"两套渲染器同时渲同一本书"
            //   的来源，用户 2026-09-21 报的崩溃就指着它。
            //   原生那侧已经有自己的字符层和选区核心，点坐标的矩形它自己就算得出来，
            //   所以这里直接拼出**和网页保存时一模一样的记录**，交给本地 runtime 落库。
            //   记录形状以 reader.src/17-highlight.js::saveHighlight 的 payload 为准。
            const value = command.value;
            const palette = { yellow: '#fff59d', green: '#a7f3d0', blue: '#a3d4ff', pink: '#fda4af' };
            const okRect = r => Array.isArray(r) && r.length === 4 && r.every(n => Number.isFinite(n));
            if (!value || typeof value.text !== 'string' || !value.text.trim() ||
                value.text.length > 4000 || !Number.isSafeInteger(value.page) || value.page < 1 ||
                !palette[value.color] || !Array.isArray(value.rects) || !value.rects.length ||
                value.rects.length > 512 || !value.rects.every(okRect) ||
                !(Number(value.pageWidth) > 0) || !(Number(value.pageHeight) > 0)) {
              return { ok: false, error: '划线参数无效' };
            }
            const runtime = window.__BW_READER_RUNTIME__;
            const fileRel = window.__PDF_CFG && window.__PDF_CFG.file_rel;
            if (!runtime || typeof runtime.savePDFHighlight !== 'function' ||
                typeof fileRel !== 'string' || !fileRel) return { ok: false, error: '本地存储尚未就绪' };
            const bytes = new Uint8Array(12);
            (window.crypto || {}).getRandomValues?.(bytes);
            const captured = scope;
            let saved;
            try {
              saved = await runtime.savePDFHighlight({
                file: fileRel, page: value.page, rects: value.rects,
                color: palette[value.color], text: value.text,
                kind: 'note', sentence: typeof value.sentence === 'string' ? value.sentence.slice(0, 600) : '',
                body: '', note: '',
                page_w: Number(value.pageWidth), page_h: Number(value.pageHeight),
                id: 'c_' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
              });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换，请在原书核对结果' };
            if (!saved || saved.ok !== true) return { ok: false, error: '划线未落库' };
            return { ok: true, value: { id: (saved.highlight && saved.highlight.id) || saved.id || '', page: value.page } };
          } else if (action === 'favoritesList') {
            // 原生收藏夹面板：数据与写入都走 rc-voicecall 的 _dock（与网页收藏夹同一份）。
            const favorite = rc().voiceCard?.favorite;
            if (!favorite?.load) return { ok: false, error: '收藏夹尚未就绪' };
            const list = await favorite.load();
            return { ok: true, value: list.slice(0, 200).map(item => {
              const cards = item.payload && Array.isArray(item.payload.cards) ? item.payload.cards : null;
              return { id: text(item.id, 160), label: text(item.label, 200) || (cards ? '学习卡片' : '收藏卡片'),
                kind: cards ? 'cards' : 'html', isHtml: !!item.isHtml,
                content: cards ? '' : text(item.raw || item.text || '', 20000),
                cards: cards ? cards.slice(0, 20).map(card => safeFields(card, ['front', 'back', 'question', 'answer', 'cloze', 'text'])) : [],
                page: text(item.meta?.page, 20), file: text(item.meta?.file, 200),
                ts: Number(item.ts) || 0, text: text(item.text || '', 600),
                pinned: !!favorite.pinned?.(item.id) };
            }) };
          } else if (action === 'favoritesTrash') {
            const favorite = rc().voiceCard?.favorite;
            if (!favorite?.trash) return { ok: false, error: '回收站尚未就绪' };
            const list = await favorite.trash();
            return { ok: true, value: list.slice(0, 200).map(item => ({ id: text(item.id, 160),
              label: text(item.label, 200) || '收藏卡片', ts: Number(item.ts) || 0, text: text(item.text || '', 600),
              page: text(item.meta?.page, 20), file: text(item.meta?.file, 200) })) };
          } else if (action === 'favoritesRestore' || action === 'favoritesPin') {
            const favorite = rc().voiceCard?.favorite, value = command.value;
            if (!favorite || !value || typeof value.id !== 'string') return { ok: false, error: '参数无效' };
            if (action === 'favoritesRestore') { await favorite.restore(value.id); return { ok: true }; }
            const on = favorite.togglePin?.(value.id);
            if (on == null) return { ok: false, error: '这张卡已不在收藏夹里' };
            schedule();
            return { ok: true, value: { pinned: on } };
          } else if (action === 'favoritesPlace') {
            const favorite = rc().voiceCard?.favorite, value = command.value;
            if (!favorite?.place || !value || typeof value.id !== 'string' || !Number.isSafeInteger(value.page) ||
                ![value.x, value.y].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) return { ok: false, error: '落点无效' };
            const placed = await favorite.place(value.id, { kind: 'pdf', page: value.page, x: value.x, y: value.y });
            if (!placed) return { ok: false, error: '没能放到书页上' };
          } else if (action === 'favoritesDelete') {
            const favorite = rc().voiceCard?.favorite, value = command.value;
            if (!favorite?.remove || !value || !Array.isArray(value.ids) || !value.ids.length) return { ok: false, error: '参数无效' };
            if (!await favorite.remove(value.ids.map(String))) return { ok: false, error: '没能删除' };
          } else if (action === 'nativeEpubHighlight') {
            // EPUB 选区条的「划线」。落库、锚点解析、就地上色、记住上次用的颜色
            // 都在底座 saveHl 那条路上，这里只转交。
            const value = command.value || {};
            if (typeof window.__bwReaderEpubHighlight !== 'function') return { ok: false, error: '划线尚未就绪' };
            const captured = scope;
            let saved;
            try {
              saved = await window.__bwReaderEpubHighlight({
                color: typeof value.color === 'string' ? value.color : ''
              });
            } catch (error) {
              const code = String(error && error.message || error);
              const said = { BW_READER_EPUB_NO_SELECTION: '没有选中内容',
                             BW_READER_EPUB_HL_FAILED: '划线没有保存成功' }[code];
              return { ok: false, error: said || code.slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: saved };
          } else if (action === 'nativeEpubHighlightColors') {
            if (typeof window.__bwReaderEpubHighlightColors !== 'function') return { ok: false, error: '色板尚未就绪' };
            return { ok: true, value: { colors: window.__bwReaderEpubHighlightColors() } };
          } else if (action === 'nativeOcrSelection') {
            // 文字层坏掉时对这块重新识别。⚠ App 里这条端点由本地 runtime 接管，
            // 跑的是 App 自己的 OCR、写回 App 自己的字符层，**不出网**。
            const value = command.value;
            if (!value || !Number.isSafeInteger(value.page) || value.page < 1 ||
                !Array.isArray(value.bbox) || value.bbox.length !== 4 ||
                !value.bbox.every(n => Number.isFinite(n) && n >= 0)) {
              return { ok: false, error: 'OCR 选区无效' };
            }
            if (typeof window.__bwReaderOcrSelection !== 'function') return { ok: false, error: '文字识别尚未就绪' };
            const captured = scope;
            let recognized;
            try {
              recognized = await window.__bwReaderOcrSelection({ page: value.page, bbox: value.bbox });
            } catch (error) {
              const code = String(error && error.message || error);
              const said = { BW_READER_OCR_BBOX: '这块区域太小，识别不了',
                             BW_READER_OCR_FAILED: '没有识别出文字' }[code];
              return { ok: false, error: said || code.slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: recognized };
          } else if (action === 'nativeCreateNote') {
            // 顶栏 🗒 在接管后是**彻底静默**的：createAtCenter → anchorFromPoint →
            // document.elementFromPoint，一页都不在 DOM 里，七个候选点全落空，
            // 便签没建、连"放不了"的 toast 也看不见。位置此时只有 PDFKit 知道。
            const value = command.value;
            if (!value || !Number.isSafeInteger(value.page) || value.page < 1 ||
                !(value.x >= 0 && value.x <= 1) || !(value.y >= 0 && value.y <= 1)) {
              return { ok: false, error: '便签位置无效' };
            }
            if (typeof window.__bwReaderCreateNote !== 'function') return { ok: false, error: '便签尚未就绪' };
            const captured = scope;
            let made;
            try {
              made = window.__bwReaderCreateNote({ page: value.page, x: value.x, y: value.y });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: made };
          } else if (action === 'nativePhraseFav') {
            // 原生词组面板的「收藏为词组」。本地先翻、真分词重算、长下划线即时画、
            // outbox 兜底，四件事都挂在底座 _phraseFav 那一条路上，这里只转交。
            const value = command.value;
            if (!value || typeof value.text !== 'string' || !value.text.trim() ||
                value.text.length > 200) return { ok: false, error: '词组无效' };
            if (typeof window.__bwReaderPhraseFav !== 'function') return { ok: false, error: '词组尚未就绪' };
            const captured = scope;
            let done;
            try {
              done = await window.__bwReaderPhraseFav({ text: value.text });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: done };
          } else if (action === 'nativeHighlightEdit') {
            // 原生划线编辑面板：改色 / 备注 / 删除。落库全走底座 _hlUpdate / _hlDelete
            // （同一条 PATCH/DELETE、同一套「点当前色＝取消颜色」语义），这里只转交。
            const value = command.value;
            if (!value || typeof value.id !== 'string' || !value.id ||
                !['color', 'note', 'delete'].includes(value.op)) return { ok: false, error: '划线参数无效' };
            if (typeof window.__bwReaderHighlightEdit !== 'function') return { ok: false, error: '划线尚未就绪' };
            const captured = scope;
            let done;
            try {
              done = await window.__bwReaderHighlightEdit({
                id: value.id, op: value.op,
                value: typeof value.value === 'string' ? value.value.slice(0, 2000) : ''
              });
            } catch (error) {
              const code = String(error && error.message || error);
              const said = { BW_READER_HL_MISS: '这条划线已经不在了',
                             BW_READER_HL_DELETE_FAILED: '删除未确认，它可能还在',
                             BW_READER_HL_SAVE_FAILED: '保存未确认，请重试' }[code];
              return { ok: false, error: said || code.slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: done };
          } else if (action === 'nativeGrammar') {
            // 原生语法面板：只取**数据**。analyze() 把结果渲成 .grammar-block 塞进容器，
            // 接管后那个容器不在屏幕上，跑完也没人看得见。前置（哪些 KG 开着、有没有
            // 跟踪节点）留在 RC.grammar 那一处，别复制到原生。
            const value = command.value;
            if (!value || typeof value.sentence !== 'string' || !value.sentence.trim() ||
                value.sentence.length > 4000) return { ok: false, error: '句子无效' };
            const grammar = rc().grammar;
            if (!grammar?.analyzeData) return { ok: false, error: '语法分析尚未就绪' };
            const captured = scope;
            let result;
            try {
              result = await grammar.analyzeData({
                sentence: value.sentence,
                text: typeof value.text === 'string' && value.text.trim() ? value.text : value.sentence,
                // ⚠ 别直接读 window.FILE_REL：那是 PDF 的变量，EPUB 上它是空的，
                // 而空 file 会让 loadTracked 当成"没有这本书"——启用的 KG 一个都取不到，
                // 于是语法分析在 EPUB 上永远回"请先启用至少一个语法 KG"。
                file: (typeof window.__bwReaderFileRel === 'function'
                  ? window.__bwReaderFileRel() : (window.FILE_REL || '')),
                aiParams: typeof window._getAiOverrides === 'function' ? window._getAiOverrides : null
              });
            } catch (error) {
              const code = String(error && error.message || error);
              const said = { BW_GRAMMAR_NO_KG: '请先在阅读设置里启用至少一个语法 KG',
                             BW_GRAMMAR_NO_TRACKED: '已启用的 KG 里没有跟踪中的节点',
                             BW_GRAMMAR_SHORT: '句子太短',
                             BW_GRAMMAR_EMPTY: '没有选中内容' }[code];
              return { ok: false, error: said || code.slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: result };
          } else if (action === 'nativeFigureAttach') {
            // 原生图描述面板的「带入助手」。带入与否的权威是网页那侧的 __figAttached
            // （它就是助手上下文的来源），所以这里转交并把回执里的真实状态带回去 ——
            // 原生本地先翻按钮会和助手实际看到的东西对不上。
            const value = command.value;
            if (!value || typeof value.id !== 'string' || !value.id ||
                !Number.isFinite(Number(value.page))) return { ok: false, error: '图参数无效' };
            if (typeof window.__bwReaderFigureAttach !== 'function') return { ok: false, error: '插图尚未就绪' };
            const captured = scope;
            let done;
            try {
              done = await window.__bwReaderFigureAttach({ id: value.id, page: Number(value.page) });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: done };
          } else if (action === 'nativeVocabMark') {
            // 原生词典面板的「标记掌握」。判据（日语/英语分流）和副作用（重画下划线）
            // 都在阅读器那侧的 __bwReaderMarkVocab 里，这里只转交。
            const value = command.value;
            if (!value || typeof value.word !== 'string' || !value.word.trim() ||
                value.word.length > 200) return { ok: false, error: '词无效' };
            if (typeof window.__bwReaderMarkVocab !== 'function') return { ok: false, error: '词表尚未就绪' };
            const captured = scope;
            let saved;
            try {
              saved = await window.__bwReaderMarkVocab({
                word: value.word,
                jp: typeof value.jp === 'boolean' ? value.jp : null,
                mastered: value.mastered !== false
              });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: saved };
          } else if (action === 'nativeSelectionLookup') {
            // 原生阅读区的查词/翻译。**只取数据**，渲染在原生那侧。
            // 语言路由和端点都在阅读器自己的 __bwReaderLookupData 里，这里不复制。
            const value = command.value;
            if (!value || typeof value.text !== 'string' || !value.text.trim() ||
                value.text.length > 2000 ||
                !['dict', 'dict-full', 'translate', 'explain', 'phrase',
                  'example-zh', 'jp-ai', 'vocab-anki', 'word-cards'].includes(value.mode)) return { ok: false, error: '查询参数无效' };
            if (typeof window.__bwReaderLookupData !== 'function') return { ok: false, error: '词典尚未就绪' };
            const captured = scope;
            let data;
            try {
              data = await window.__bwReaderLookupData({
                text: value.text, mode: value.mode,
                context: typeof value.context === 'string' ? value.context : '',
                page: Number.isSafeInteger(value.page) ? value.page : 0
              });
            } catch (error) {
              return { ok: false, error: String(error && error.message || error).slice(0, 200) };
            }
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '书籍已切换' };
            return { ok: true, value: data };
          } else if (action === 'readingSettingsRead' || action === 'readingSettingsWrite') {
            const owner = rc().readerPreferences, captured = scope;
            if (!owner?.state || !owner?.perform || command.scope !== scope) return { ok: false, error: '阅读设置尚未就绪' };
            if (action === 'readingSettingsWrite') await owner.perform(command.key, command.value);
            else if (owner.read) await owner.read();
            if (captured !== scope || getScopeKey() !== scopeKey || owner !== rc().readerPreferences) return { ok: false, error: '书籍已切换，请在原书核对保存结果' };
            schedule();
            return { ok: true, value: owner.state() };
          } else if (action === 'navigationRead' || action === 'navigationAction') {
            const owner = rc().readerNavigation, captured = scope;
            if (!owner?.state || !owner?.perform || command.scope !== scope) return { ok: false, error: '导航尚未就绪' };
            if (action === 'navigationAction') await owner.perform(command.text, command.value);
            if (captured !== scope || getScopeKey() !== scopeKey || owner !== rc().readerNavigation) return { ok: false, error: '书籍已切换' };
            schedule();
            return { ok: true, value: owner.state() };
          } else if (action === 'tocRead') {
            const owner = rc().readerTOC, captured = scope;
            if (!owner?.read || !owner?.jump || command.scope !== scope) return { ok: false, error: '目录尚未就绪' };
            const sequence = ++tocSequence;
            tocController?.abort(); tocController = new AbortController(); tocEntries.clear(); tocOwner = owner;
            const value = await owner.read({ signal: tocController.signal });
            if (captured !== scope || getScopeKey() !== scopeKey || sequence !== tocSequence || owner !== rc().readerTOC) return { ok: false, error: '书籍已切换，请重新打开目录' };
            return { ok: true, value: value.map((entry, index) => {
              const id = 'toc-' + hash(scope + ':' + sequence + ':' + index);
              tocEntries.set(id, entry);
              return { id, title: text(entry.title, 2000), label: text(entry.label, 100), level: Math.max(1, Math.min(12, Number(entry.level) || 1)) };
            }) };
          } else if (action === 'tocJump') {
            const entry = tocEntries.get(command.actionId);
            if (!entry || command.scope !== scope || tocOwner !== rc().readerTOC) return { ok: false, error: '目录已更新，请重新选择' };
            await tocOwner.jump(entry);
          } else if (action === 'searchRead') {
            const owner = rc().readerSearch, captured = scope;
            if (!owner?.search || command.scope !== scope) return { ok: false, error: '搜索尚未就绪' };
            const sequence = ++searchSequence;
            searchController?.abort(); searchController = new AbortController();
            searchResults.clear(); searchQuery = String(command.text || '').trim();
            const value = await owner.search(searchQuery, { signal: searchController.signal });
            if (captured !== scope || getScopeKey() !== scopeKey || sequence !== searchSequence) return { ok: false, error: '搜索内容已切换' };
            const results = (value.results || []).map((result, index) => {
              const id = 'search-' + hash(scope + ':' + sequence + ':' + index);
              searchResults.set(id, result);
              return { id, label: text(result.label, 240), excerpt: text(result.excerpt, 6000), count: result.count || 1 };
            });
            return { ok: true, value: { total: value.total || 0, pages: value.pages || 0, incomplete: !!value.incomplete, results } };
          } else if (action === 'searchJump') {
            const result = searchResults.get(command.actionId);
            if (!result || command.scope !== scope || !rc().readerSearch?.jump) return { ok: false, error: '搜索结果已更新，请重新选择' };
            await rc().readerSearch.jump(result, searchQuery);
          } else if (action === 'settingsRead') {
            const service = rc().assistant?.settingsService, captured = scope;
            if (!service || command.scope !== scope || !['models', 'voice', 'profiles', 'computer'].includes(command.section)) return { ok: false, error: '设置尚未就绪' };
            const value = await service[command.section]();
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '页面已切换' };
            if (command.section === 'models') settingsModels = value;
            if (command.section === 'voice') settingsVoice = value;
            return { ok: true, value };
          } else if (action === 'settingsWrite') {
            const service = rc().assistant?.settingsService, captured = scope;
            if (!service || command.scope !== scope) return { ok: false, error: '设置尚未就绪' };
            let result;
            if (command.section === 'models' && command.value && settingsModels) result = await service.setAction(command.value, settingsModels);
            else if (command.section === 'profiles') result = await service.profile(command.op, command.name);
            else if (command.section === 'computer') result = await service.setComputerTarget(command.value);
            else if (command.section === 'voice' && settingsVoice) result = await service.setField(command.key, command.value, command.device === true, settingsVoice.cfg);
            else return { ok: false, error: '请先读取当前配置' };
            if (captured !== scope || getScopeKey() !== scopeKey) return { ok: false, error: '页面已切换，请在原账户核对保存结果' };
            return { ok: true, value: result };
          } else if (action === 'mediaResource') {
            const target = actions.get(command.actionId);
            if (!command.scope || !target || target.scope !== scope || !target.node?.isConnected || !target.resource) {
              return { ok: false, error: '图片已更新，请重试' };
            }
            return { ok: true, resource: target.resource() };
          } else if (action === 'inspectArtifact') {
            const target = actions.get(command.actionId);
            if (!command.scope || !target || target.scope !== scope || !target.node?.isConnected || !target.inspect) {
              return { ok: false, error: '此项内容暂不可读取，请刷新后重试' };
            }
            return { ok: true, detail: JSON.parse(JSON.stringify(target.inspect())) };
          } else if (action === 'openModels' && typeof rc().assistant?.openModelSettings === 'function') {
            setLegacy(true); rc().assistant.openModelSettings();
          } else if (action === 'openSettings' && (typeof window.openSettings === 'function' || document.getElementById('ep-set-btn'))) {
            setLegacy(true);
            if (typeof window.openSettings === 'function') window.openSettings(); else document.getElementById('ep-set-btn').click();
          } else if ((action === 'openReview' || action === 'reviewAction') && rc().review?.performNativeInteraction) {
            const owner = rc().review, state = owner.presentationState();
            const value = action === 'openReview' ? { key: 'mode', enabled: !state.active } : command.value;
            if (!value || (action === 'reviewAction' && value.contextKey !== (state.lease || hash(state.contextKey)))) return { ok: false, error: '复习内容已更新，请重试' };
            await owner.performNativeInteraction({ ...value, contextKey: state.contextKey });
          } else if (action === 'openReview' && document.getElementById('asst-review-toggle')) {
            setLegacy(true); document.getElementById('asst-review-toggle').click();
          } else if (action === 'openSearch' && (typeof window.openSearch === 'function' || document.getElementById('ep-search-btn'))) {
            setLegacy(true);
            if (typeof window.openSearch === 'function') window.openSearch(); else document.getElementById('ep-search-btn').click();
          } else return { ok: false, error: '当前页面不支持此操作' };
          schedule();
          return { ok: true };
        } catch (error) { return { ok: false, error: error?.message || '操作未完成，请重试' }; }
      }
      document.getElementById('bw-native-conversation-style')?.remove();
      const style = document.createElement('style');
      style.id = 'bw-native-conversation-style';
      style.textContent = `
        /* 网页外壳的隐藏规则不在这里 —— 见 ReaderWebView 里 atDocumentStart 那条
           (bw-native-shell)。写在这里就要等本脚本跑完再等 setNativeMode 送到,
           而原生顶栏是 SwiftUI 画的、不等任何人。bw-native-navigation 这个类保留,
           因为 epub-html.js 和原生选区条还拿它判断"现在是原生导航"。 */
        /* ⚠⚠ 这里必须是 visibility:hidden。三个都试过，只有它同时成立：
           · opacity:0 —— 照旧布局、绘制、合成整棵子树（富文本/公式/图片），
             等于同一批卡片画两遍，正是"后台跑着很耗性能"的来源。
           · display:none —— getBoundingClientRect 全是 0。
           · content-visibility:hidden —— 它会**连同尺寸一起 contain**：
             盒子宽高由内容撑开的那些（浮动卡片就是）直接塌成 0×0。
             nativeFloatingState 按 rect.width<=0 过滤，于是整张卡被判成
             不存在 —— 表现就是"松手后卡片直接消失了"（2026-09-22 实报，我造的）。
           visibility:hidden 保留完整布局（几何仍然准），只跳过绘制。 */
        .bw-native-page-cards [data-bw-native-placement] {visibility:hidden!important;pointer-events:none!important}
        /* 对话正文同理：原生侧栏接管后，网页那条消息流谁也看不见，却还在为
           每条回答排版、绘制（富文本 + 公式 + 图片），历史越长越贵。
           ⚠ 这里**不能**删 DOM：原生侧栏的内容目前正是从这些节点读出来的
           （真要删得先把对话做成数据模型）。content-visibility 只停渲染、
           不动 DOM，是眼下唯一两头都成立的做法。 */
        .bw-native-conversation-active #asst-thread {visibility:hidden!important}
        .bw-native-conversation-active #ep-side,.bw-native-conversation-active #grammar-panel,
        .bw-native-conversation-active #side-handle,.bw-native-conversation-active #ep-side-handle {visibility:hidden!important;pointer-events:none!important}
        .bw-native-conversation-active body.grammar-open #main,.bw-native-conversation-active body.grammar-open #header {padding-right:0!important}
        .bw-native-conversation-active body.ep-side-open #ep-content {padding-right:0!important}
        .bw-native-conversation-active body.ep-side-open #ep-viewer,.bw-native-conversation-active body.ep-side-open #html-content {margin-right:0!important}
      `;
      const mountObserver = new MutationObserver(records => {
        if (!thread || !thread.isConnected || records.some(record => Array.from(record.addedNodes).some(node => node.nodeType === 1 && (['asst-thread', 'asst-input', 'asst-computer', 'asst-call'].includes(node.id) || node.querySelector?.('#asst-thread,#asst-input,#asst-computer,#asst-call'))))) schedule();
      });
      function observeDocument() {
        if (!document.documentElement) return;
        if (!style.isConnected) document.documentElement.appendChild(style);
        mountObserver.observe(document.documentElement, { childList: true, subtree: true });
      }
      observeDocument();
      window.addEventListener('DOMContentLoaded', observeDocument, {once:true});
      ['DOMContentLoaded', 'popstate', 'hashchange', 'bw:native-local-runtime-ready', 'rc:native-document-position', 'bw-native-computer-voice-state', 'bw-native-figure-projection'].forEach(name => window.addEventListener(name, schedule));
      ['rc:native-turn-update','rc:assistant-mode-changed','rc:assistant-message-changed','rc:review-presentation-changed','rc:placement-changed','rc:favorites-changed','bw:native-favorites-changed','rc:flashcard-state-changed'].forEach(name => window.addEventListener(name,scheduleMessages));
      window.addEventListener('scroll', schedule, { capture: true, passive: true });
      window.addEventListener('resize', schedule, { passive: true });
      window.addEventListener('pointerup', schedule, { capture: true, passive: true });
      window.addEventListener('pageshow', () => { suspended = false; mountObserver.observe(document.documentElement, { childList: true, subtree: true }); thread = null; controls = null; drawerElement = null; contextElement = null; toolbarElement = null; schedule(); });
      window.addEventListener('pagehide', () => { suspended = true; threadObserver?.disconnect(); controlsObserver?.disconnect(); drawerObserver?.disconnect(); contextObserver?.disconnect(); toolbarObserver?.disconnect(); mountObserver.disconnect(); if (timer != null) clearTimeout(timer); timer = null; });
      window.__bwNativeConversation = Object.freeze({ perform, setNativeMode, watchCards, currentScope: () => scope, snapshot: () => { lastSignature = ''; snapshot(); } });
      schedule();
    })();
    """#
}
