import Foundation

/// Temporary data-only projection for the existing assistant transport. There
/// is no hidden image crop, DOM thumbnail, network fetch or attachment writer.
enum ReaderNativeFigureBridge {
    static let source = #"""
      if (typeof FILE_REL === 'undefined' || FILE_REL !== payload.file) return false;
      const old = window.__bwNativeFigureProjection;
      if (old?.epoch === payload.epoch && old.revision > payload.revision) return false;
      if (old?.epoch !== payload.epoch) window.__bwNativeFigureConsumed = new Set();
      const present = new Set(payload.items.map(item => item.token));
      window.__bwNativeFigureConsumed = new Set([...window.__bwNativeFigureConsumed].filter(token => present.has(token)));
      window.__bwNativeFigureProjection = payload;
      window.__figAttached = payload.items.filter(item => !window.__bwNativeFigureConsumed.has(item.token));
      window.__figInk = (page, box) => {
        const item = (window.__figAttached || []).find(a => a.page === page && a.box?.every((v, i) => v === box[i]));
        return item?.ink || [];
      };
      window.__renderFigChips = () => {};
      window.__bwNativeConsumeFigures = tokens => {
        const state = window.__bwNativeFigureProjection;
        if (!state) return;
        tokens.forEach(token => window.__bwNativeFigureConsumed.add(token));
        window.__figAttached = (window.__figAttached || []).filter(item => !tokens.includes(item.token));
        window.webkit.messageHandlers.bwNativeConversation.postMessage({
          version: 1, type: 'figure-consumed', file: state.file, epoch: state.epoch, tokens
        });
        window.dispatchEvent(new Event('bw-native-figure-projection'));
      };
      window.__clearFigFocus = () => window.__bwNativeConsumeFigures((window.__figAttached || []).map(a => a.token));
      window.dispatchEvent(new Event('bw-native-figure-projection'));
      return true;
    """#
}
