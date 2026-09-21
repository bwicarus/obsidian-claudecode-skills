import SwiftUI

struct ReaderNativeCardStroke {
    let points: [CGPoint]
    let widths: [CGFloat]
    let width: CGFloat
    let color: Color
    let region: Bool
    let ordinal: Int

    init?(_ raw: [String: Any]) {
        let values = (raw["pts"] ?? raw["p"]) as? [[NSNumber]] ?? []
        let points = values.compactMap { p -> CGPoint? in
            guard p.count >= 2, p[0].doubleValue.isFinite, p[1].doubleValue.isFinite else { return nil }
            return CGPoint(x: p[0].doubleValue, y: p[1].doubleValue)
        }
        guard !points.isEmpty else { return nil }
        self.points = points
        width = max(0.3, min(48, (raw["w"] as? NSNumber).map { CGFloat(truncating: $0) } ?? 4))
        widths = (raw["ww"] as? [NSNumber] ?? []).map { max(0.3, min(48, CGFloat(truncating: $0))) }
        let value = (raw["c"] as? String ?? "#ff3b30").replacingOccurrences(of: "#", with: "")
        let rgb = UInt32(value, radix: 16) ?? 0xff3b30
        color = Color(red: Double((rgb >> 16) & 255) / 255, green: Double((rgb >> 8) & 255) / 255, blue: Double(rgb & 255) / 255)
        region = raw["t"] as? String == "region"
        ordinal = (raw["ordinal"] as? NSNumber)?.intValue ?? 0
    }
}

@MainActor
struct ReaderNativeCardInkLayer: View {
    let item: ReaderNativePagePlacement
    @ObservedObject var reader: ReaderWebViewModel
    let actionID: String

    var body: some View {
        GeometryReader { geometry in
            let frame = geometry.frame(in: .global)
            let ratio = item.inkAspectRatio > 0 ? item.inkAspectRatio : max(0.01, geometry.size.width / max(1, geometry.size.height))
            let box = inkBox(geometry.size, ratio: ratio)
            Canvas { context, _ in
                for stroke in item.ink {
                    let points = stroke.points.map { CGPoint(x: box.minX + $0.x * box.width, y: box.minY + $0.y * box.height) }
                    guard let first = points.first else { continue }
                    if stroke.region {
                        var path = Path(); path.move(to: first)
                        for p in points.dropFirst() { path.addLine(to: p) }
                        path.closeSubpath()
                        context.fill(path, with: .color(stroke.color.opacity(0.12)))
                        context.stroke(path, with: .color(stroke.color), style: StrokeStyle(lineWidth: stroke.width, lineCap: .round, lineJoin: .round))
                        if stroke.ordinal > 0 { context.draw(Text(String(stroke.ordinal)).font(.caption.bold()).foregroundStyle(stroke.color), at: first) }
                    } else if points.count == 1 {
                        context.fill(Path(ellipseIn: CGRect(x: first.x - stroke.width / 2, y: first.y - stroke.width / 2, width: stroke.width, height: stroke.width)), with: .color(stroke.color))
                    } else {
                        for index in 1..<points.count {
                            var path = Path(); path.move(to: points[index - 1]); path.addLine(to: points[index])
                            let width = stroke.widths.count == points.count ? stroke.widths[index] : stroke.width
                            context.stroke(path, with: .color(stroke.color), style: StrokeStyle(lineWidth: width, lineCap: .round, lineJoin: .round))
                        }
                    }
                }
            }
            .onChange(of: frame, initial: true) { _, _ in register(frame, box: box, ratio: ratio) }
            .onChange(of: item.inkAspectRatio) { _, _ in register(frame, box: box, ratio: ratio) }
            .onChange(of: item.inkGeometry) { _, _ in register(frame, box: box, ratio: ratio) }
            .onDisappear { reader.registerNativeCardInk(id: actionID, windowRect: nil, aspectRatio: ratio, geometry: item.inkGeometry) }
        }.allowsHitTesting(false).clipped()
    }

    private func register(_ frame: CGRect, box: CGRect, ratio: CGFloat) {
        reader.registerNativeCardInk(id: actionID, windowRect: box.offsetBy(dx: frame.minX, dy: frame.minY),
            occlusion: frame, aspectRatio: ratio, geometry: item.inkGeometry)
    }

    private func inkBox(_ size: CGSize, ratio: CGFloat) -> CGRect {
        if size.width > size.height * ratio {
            let width = size.height * ratio
            return CGRect(x: (size.width - width) / 2, y: 0, width: width, height: size.height)
        }
        let height = size.width / ratio
        return CGRect(x: 0, y: (size.height - height) / 2, width: size.width, height: height)
    }
}
