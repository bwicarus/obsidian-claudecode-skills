import SwiftUI

/// Range controls belong to the card viewport, outside the scrolling chart details.
struct ChartCardViewport<Content: View, Navigator: View>: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    private let content: (CGFloat) -> Content
    private let navigator: Navigator

    init(@ViewBuilder content: @escaping (CGFloat) -> Content, @ViewBuilder navigator: () -> Navigator) {
        self.content = content
        self.navigator = navigator()
    }

    var body: some View {
        Group {
            if availableHeight > 0 {
                VStack(spacing: 0) {
                    GeometryReader { geometry in
                        ScrollView(.vertical) {
                            content(max(0, geometry.size.height - 32))
                                .frame(maxWidth: .infinity, alignment: .topLeading)
                                .padding(.horizontal, 22).padding(.vertical, 16)
                        }
                        .scrollBounceBehavior(.basedOnSize)
                    }
                    Divider()
                    navigator
                        .padding(.horizontal, 22).padding(.top, 8).padding(.bottom, 22)
                        .fixedSize(horizontal: false, vertical: true)
                        .layoutPriority(1)
                }
                .frame(height: availableHeight, alignment: .top)
            } else {
                VStack(alignment: .leading, spacing: 16) {
                    content(0)
                    navigator
                }
                .padding(22)
            }
        }
        .background(.white, in: RoundedRectangle(cornerRadius: 22))
    }
}

enum ChartRangeBounds {
    static func clamped(_ range: Range<Int>, count: Int, minimumCount: Int = 1) -> Range<Int> {
        guard count > 0 else { return 0..<0 }
        let length = min(count, max(min(count, max(1, minimumCount)), range.count))
        let lower = min(max(0, range.lowerBound), count - length)
        return lower..<(lower + length)
    }
}

/// Index boundaries are half-open, matching the range used to slice chart data.
struct ChartRangeNavigator: View {
    let values: [Double?]
    @Binding var selection: Range<Int>
    let minimumCount: Int
    let onEditingChanged: (Bool) -> Void

    @State private var dragOrigin: Range<Int>?
    @State private var activeTarget: DragTarget?
    @State private var dragAxis = DragAxis.undecided
    @Namespace private var navigatorCoordinateSpace

    private let touchSize: CGFloat = 44
    private let trackHeight: CGFloat = 36

    init(values: [Double?], selection: Binding<Range<Int>>, minimumCount: Int,
         onEditingChanged: @escaping (Bool) -> Void = { _ in }) {
        self.values = values
        self._selection = selection
        self.minimumCount = minimumCount
        self.onEditingChanged = onEditingChanged
    }

    var body: some View {
        GeometryReader { geometry in
            let width = max(1, geometry.size.width - touchSize)
            let minimum = effectiveMinimum(width: width)
            let range = ChartRangeBounds.clamped(selection, count: values.count, minimumCount: minimum)
            let lowerX = position(range.lowerBound, width: width)
            let upperX = position(range.upperBound, width: width)
            let selectedWidth = upperX - lowerX

            ZStack(alignment: .topLeading) {
                overview
                    .frame(width: width, height: trackHeight)
                    .background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 6))
                    .overlay(alignment: .leading) {
                        Rectangle().fill(AppStyle.canvas.opacity(0.75)).frame(width: lowerX)
                    }
                    .overlay(alignment: .trailing) {
                        Rectangle().fill(AppStyle.canvas.opacity(0.75)).frame(width: width - upperX)
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .offset(x: touchSize / 2, y: (60 - trackHeight) / 2)
                    .accessibilityHidden(true)

                RoundedRectangle(cornerRadius: 6)
                    .fill(AppStyle.accent.opacity(0.04))
                    .overlay {
                        RoundedRectangle(cornerRadius: 6).stroke(AppStyle.accent.opacity(0.8), lineWidth: 1.5)
                    }
                    .frame(width: selectedWidth, height: trackHeight)
                    .offset(x: touchSize / 2 + lowerX, y: (60 - trackHeight) / 2)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)

                // The handles occupy 22 points on either side of each boundary;
                // the remaining middle target never overlaps either handle.
                Rectangle().fill(Color.clear)
                    .frame(width: max(0, selectedWidth - touchSize), height: touchSize)
                    .contentShape(Rectangle())
                    .position(x: touchSize / 2 + (lowerX + upperX) / 2, y: 30)
                    .simultaneousGesture(dragGesture(for: .window, width: width, minimum: minimum))
                    .accessibilityElement()
                    .accessibilityLabel("平移时间范围")
                    .accessibilityValue(rangeDescription(range))
                    .accessibilityHint("向上轻扫查看更晚数据，向下轻扫查看更早数据")
                    .accessibilityAdjustableAction {
                        adjust(.window, direction: $0, width: width, minimum: minimum)
                    }
                    .accessibilityHidden(values.isEmpty)

                handle(.lower, range: range, width: width, minimum: minimum)
                    .position(x: touchSize / 2 + lowerX, y: 30)
                handle(.upper, range: range, width: width, minimum: minimum)
                    .position(x: touchSize / 2 + upperX, y: 30)
            }
            .coordinateSpace(name: navigatorCoordinateSpace)
            .onAppear { normalize(width: width) }
            .onChange(of: values.count) { _, _ in normalize(width: width) }
            .onChange(of: geometry.size.width) { _, _ in normalize(width: width) }
            .onChange(of: minimumCount) { _, _ in normalize(width: width) }
            .onChange(of: selection) { _, _ in normalize(width: width) }
        }
        .frame(minWidth: 132, maxWidth: .infinity)
        .frame(height: 60)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("图表时间范围")
        .onDisappear { finishDrag() }
    }

    private var overview: some View {
        Canvas { context, size in
            let finite = values.compactMap { value -> Double? in
                guard let value, value.isFinite else { return nil }
                return value
            }
            guard let minimum = finite.min(), let maximum = finite.max(), !values.isEmpty else { return }
            let span = maximum - minimum
            let baseline = size.height - 3
            var segments: [[CGPoint]] = []
            var segment: [CGPoint] = []
            for (index, value) in values.enumerated() {
                guard let value, value.isFinite else {
                    if !segment.isEmpty { segments.append(segment); segment = [] }
                    continue
                }
                let fraction = span > 0 ? (value - minimum) / span : 0.5
                let point = CGPoint(x: (CGFloat(index) + 0.5) / CGFloat(values.count) * size.width,
                                    y: baseline - CGFloat(fraction) * (size.height - 6))
                segment.append(point)
            }
            if !segment.isEmpty { segments.append(segment) }
            for points in segments {
                guard let first = points.first, let last = points.last else { continue }
                if points.count == 1 {
                    context.fill(Path(ellipseIn: CGRect(x: first.x - 1.5, y: first.y - 1.5,
                                                        width: 3, height: 3)),
                                 with: .color(AppStyle.accent.opacity(0.65)))
                    continue
                }
                var line = Path()
                line.move(to: first)
                for point in points.dropFirst() { line.addLine(to: point) }
                var fill = line
                fill.addLine(to: CGPoint(x: last.x, y: baseline))
                fill.addLine(to: CGPoint(x: first.x, y: baseline))
                fill.closeSubpath()
                context.fill(fill, with: .linearGradient(
                    Gradient(colors: [AppStyle.accent.opacity(0.22), AppStyle.accent.opacity(0.03)]),
                    startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
                context.stroke(line, with: .color(AppStyle.accent.opacity(0.55)), lineWidth: 1)
            }
        }
    }

    private func handle(_ target: DragTarget, range: Range<Int>, width: CGFloat, minimum: Int) -> some View {
        ZStack {
            Rectangle().fill(Color.clear)
            RoundedRectangle(cornerRadius: 4)
                .fill(AppStyle.accent)
                .frame(width: 10, height: 28)
                .overlay {
                    Capsule().fill(.white.opacity(0.9)).frame(width: 2, height: 13)
                }
        }
        .frame(width: touchSize, height: touchSize)
        .contentShape(Rectangle())
        .simultaneousGesture(dragGesture(for: target, width: width, minimum: minimum))
        .accessibilityElement()
        .accessibilityLabel(target == .lower ? "范围起点" : "范围终点")
        .accessibilityValue(values.isEmpty ? "暂无数据" : "第 \(target == .lower ? range.lowerBound + 1 : range.upperBound) 个，共 \(values.count) 个数据点")
        .accessibilityHint("向上或向下轻扫调整时间范围")
        .accessibilityAdjustableAction { adjust(target, direction: $0, width: width, minimum: minimum) }
        .accessibilityHidden(values.isEmpty)
    }

    private func effectiveMinimum(width: CGFloat) -> Int {
        guard !values.isEmpty else { return 0 }
        // 44 points for the middle target, plus each handle's inner 22 points.
        let pixels = min(touchSize * 2, width)
        let countForTouchTargets = Int(ceil(pixels / width * CGFloat(values.count)))
        return min(values.count, max(1, minimumCount, countForTouchTargets))
    }

    private func position(_ boundary: Int, width: CGFloat) -> CGFloat {
        guard !values.isEmpty else { return 0 }
        return CGFloat(boundary) / CGFloat(values.count) * width
    }

    private func normalize(width: CGFloat) {
        let normalized = ChartRangeBounds.clamped(selection, count: values.count,
                                                 minimumCount: effectiveMinimum(width: width))
        if normalized != selection { selection = normalized }
    }

    private func dragGesture(for target: DragTarget, width: CGFloat, minimum: Int) -> some Gesture {
        DragGesture(minimumDistance: 10, coordinateSpace: .named(navigatorCoordinateSpace))
            .onChanged { value in
                guard !values.isEmpty else { return }
                if activeTarget == nil {
                    activeTarget = target
                    dragOrigin = ChartRangeBounds.clamped(selection, count: values.count, minimumCount: minimum)
                    dragAxis = abs(value.translation.width) > abs(value.translation.height) ? .horizontal : .vertical
                    if dragAxis == .horizontal { onEditingChanged(true) }
                }
                guard activeTarget == target, dragAxis == .horizontal, let dragOrigin else { return }
                let scaled = value.translation.width / width * CGFloat(values.count)
                let delta = Int(max(-CGFloat(values.count), min(CGFloat(values.count), scaled)).rounded())
                apply(target, delta: delta, origin: dragOrigin, minimum: minimum)
            }
            .onEnded { _ in
                if activeTarget == target { finishDrag() }
            }
    }

    private func apply(_ target: DragTarget, delta: Int, origin: Range<Int>, minimum: Int) {
        let range = ChartRangeBounds.clamped(origin, count: values.count, minimumCount: minimum)
        let next: Range<Int>
        switch target {
        case .lower:
            let lower = min(max(0, range.lowerBound + delta), range.upperBound - minimum)
            next = lower..<range.upperBound
        case .upper:
            let upper = max(min(values.count, range.upperBound + delta), range.lowerBound + minimum)
            next = range.lowerBound..<upper
        case .window:
            let lower = min(max(0, range.lowerBound + delta), values.count - range.count)
            next = lower..<(lower + range.count)
        }
        if selection != next { selection = next }
    }

    private func adjust(_ target: DragTarget, direction: AccessibilityAdjustmentDirection, width: CGFloat, minimum: Int) {
        guard !values.isEmpty else { return }
        let range = ChartRangeBounds.clamped(selection, count: values.count, minimumCount: minimum)
        let step = max(1, range.count / 10)
        let delta: Int
        switch direction {
        case .increment: delta = step
        case .decrement: delta = -step
        @unknown default: return
        }
        onEditingChanged(true)
        apply(target, delta: delta, origin: range, minimum: minimum)
        onEditingChanged(false)
    }

    private func rangeDescription(_ range: Range<Int>) -> String {
        values.isEmpty ? "暂无数据" : "第 \(range.lowerBound + 1) 至 \(range.upperBound) 个，共 \(range.count) 个数据点"
    }

    private func finishDrag() {
        let wasEditing = dragAxis == .horizontal
        dragOrigin = nil
        activeTarget = nil
        dragAxis = .undecided
        if wasEditing { onEditingChanged(false) }
    }

    private enum DragTarget { case lower, upper, window }
    private enum DragAxis { case undecided, horizontal, vertical }
}
