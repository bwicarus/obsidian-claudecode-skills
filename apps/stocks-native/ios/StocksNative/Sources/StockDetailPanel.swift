import SwiftUI

@MainActor
struct StockDetailPanel: View {
    @ObservedObject var model: AppModel
    let availableSize: CGSize
    let onClose: () -> Void

    @State private var panelFrame: CGRect = .zero
    @State private var restoreFrame: CGRect?
    @State private var hasUserAdjusted = false
    @State private var restoreWasUserAdjusted = false
    @State private var resizeOrigin: CGRect?
    @State private var isExpanded = false

    private var bounds: CGRect { StockDetailPanelGeometry.bounds(in: availableSize) }
    private var visibleFrame: CGRect {
        if isExpanded { return bounds }
        return !hasUserAdjusted || panelFrame.isEmpty
            ? StockDetailPanelGeometry.initialFrame(in: availableSize)
            : StockDetailPanelGeometry.clamp(panelFrame, in: availableSize)
    }

    var body: some View {
        let frame = visibleFrame
        VStack(spacing: 0) {
            titleBar
            Divider()
            StockDetailView(model: model)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: frame.width, height: frame.height)
        .background(.white)
        .clipShape(UnevenRoundedRectangle(topLeadingRadius: 18, style: .continuous))
        .background {
            // Cast the shadow from a simple shape, not the native chart/list subtree.
            UnevenRoundedRectangle(topLeadingRadius: 18, style: .continuous)
                .fill(.white)
                .shadow(color: .black.opacity(0.13), radius: 18, x: 0, y: 6)
                .allowsHitTesting(false)
        }
        .overlay {
            UnevenRoundedRectangle(topLeadingRadius: 18, style: .continuous)
                .strokeBorder(.black.opacity(0.07), lineWidth: 1)
                .allowsHitTesting(false)
        }
        .offset(x: frame.minX, y: frame.minY)
        // This outer frame has no background, gesture, or contentShape. Only the
        // visible panel participates in hit testing; the result list stays usable.
        .frame(width: max(0, availableSize.width), height: max(0, availableSize.height), alignment: .topLeading)
        .onChange(of: availableSize) { _, _ in
            // Clamp only the displayed frame. A temporarily narrow viewport must
            // not replace the user's preferred geometry when its space returns.
            resizeOrigin = nil
        }
    }

    private var titleBar: some View {
        HStack(spacing: 0) {
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
                .gesture(resizeGesture)
                .opacity(isExpanded ? 0.35 : 1)
                .accessibilityLabel("调整详情面板大小")
                .accessibilityHint("拖动左上角调整宽高，右下角保持固定")
                .accessibilityAdjustableAction { direction in
                    guard !isExpanded else { return }
                    let amount: CGFloat = direction == .increment ? -50 : 50
                    panelFrame = StockDetailPanelGeometry.resizingTopLeft(
                        visibleFrame, translation: CGSize(width: amount, height: amount), in: availableSize
                    )
                    hasUserAdjusted = true
                }
            HStack(spacing: 8) {
                Text("个股详情")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(AppStyle.ink)
                Spacer(minLength: 0)
            }
            .lineLimit(1)
            .frame(maxWidth: .infinity, minHeight: 44)
            .accessibilityElement(children: .combine)
            Button(action: toggleExpanded) {
                Image(systemName: isExpanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isExpanded ? "恢复详情面板大小" : "扩大详情面板")
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("关闭个股详情，返回股票列表")
        }
        .font(.system(size: 14, weight: .medium))
        .foregroundStyle(.secondary)
        .frame(height: 44)
        .background(AppStyle.canvas)
    }

    private var resizeGesture: some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .global)
            .onChanged { value in
                guard !isExpanded else { return }
                if resizeOrigin == nil { resizeOrigin = visibleFrame }
                guard let origin = resizeOrigin else { return }
                panelFrame = StockDetailPanelGeometry.resizingTopLeft(
                    origin, translation: value.translation, in: availableSize
                )
                hasUserAdjusted = true
            }
            .onEnded { _ in resizeOrigin = nil }
    }

    private func toggleExpanded() {
        resizeOrigin = nil
        withAnimation(.easeInOut(duration: 0.18)) {
            if isExpanded {
                panelFrame = restoreFrame ?? .zero
                hasUserAdjusted = restoreWasUserAdjusted
                isExpanded = false
            } else {
                restoreFrame = hasUserAdjusted ? panelFrame : nil
                restoreWasUserAdjusted = hasUserAdjusted
                isExpanded = true
            }
        }
    }
}

enum StockDetailPanelGeometry {
    static func bounds(in size: CGSize) -> CGRect {
        let width = size.width.isFinite ? max(1, size.width) : 1
        let height = size.height.isFinite ? max(1, size.height) : 1
        return CGRect(x: 0, y: 0, width: width, height: height)
    }

    static func initialFrame(in size: CGSize) -> CGRect {
        let area = bounds(in: size)
        let width = size.width >= 820
            ? min(area.width, max(480, size.width - 328))
            : area.width
        return CGRect(x: area.maxX - width, y: area.minY, width: width, height: area.height)
    }

    static func clamp(_ frame: CGRect, in size: CGSize) -> CGRect {
        let area = bounds(in: size)
        guard !frame.isEmpty, frame.minX.isFinite, frame.minY.isFinite,
              frame.width.isFinite, frame.height.isFinite else { return initialFrame(in: size) }
        let width = min(area.width, max(min(480, area.width), frame.width))
        let height = min(area.height, max(min(280, area.height), frame.height))
        // Only size is user adjustable. The viewport owns the bottom/right anchor.
        return CGRect(x: area.maxX - width, y: area.maxY - height, width: width, height: height)
    }

    static func resizingTopLeft(_ frame: CGRect, translation: CGSize, in size: CGSize) -> CGRect {
        let area = bounds(in: size)
        let original = clamp(frame, in: size)
        let minimumWidth = min(480, area.width)
        let minimumHeight = min(280, area.height)
        let x = min(max(original.minX + translation.width, area.minX), original.maxX - minimumWidth)
        let y = min(max(original.minY + translation.height, area.minY), original.maxY - minimumHeight)
        return CGRect(x: x, y: y, width: original.maxX - x, height: original.maxY - y)
    }
}
