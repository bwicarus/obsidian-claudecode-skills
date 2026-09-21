import SwiftUI

struct ReaderNativeCardStroke {
    let points: [CGPoint]
    let widths: [CGFloat]
    let width: CGFloat
    let color: Color
    let region: Bool
    let ordinal: Int
    let kind: String
    let createdAt: Date?

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
        var value = (raw["c"] as? String ?? "#ff3b30").replacingOccurrences(of: "#", with: "")
        if value.count == 3 || value.count == 4 { value = value.map { "\($0)\($0)" }.joined() }
        let number = UInt32(value, radix: 16) ?? 0xff3b30
        let rgb = value.count == 8 ? number >> 8 : number
        color = Color(red: Double((rgb >> 16) & 255) / 255, green: Double((rgb >> 8) & 255) / 255,
                      blue: Double(rgb & 255) / 255, opacity: value.count == 8 ? Double(number & 255) / 255 : 1)
        region = raw["t"] as? String == "region"
        kind = raw["t"] as? String ?? "pen"
        ordinal = (raw["ordinal"] as? NSNumber)?.intValue ?? 0
        let timestamp = (raw["createdAtEpochMs"] as? NSNumber)?.doubleValue ?? 0
        createdAt = timestamp.isFinite && timestamp > 0 ? Date(timeIntervalSince1970: timestamp / 1000) : nil
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
                    ReaderNativeInkDrawing.draw(stroke, in: box, context: &context)
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

/// Shared native drawing for card and page overlays, preserving original
/// normalized points and pressure samples instead of rasterizing old canvases.
enum ReaderNativeInkDrawing {
    static func draw(_ stroke: ReaderNativeCardStroke, in box: CGRect, context: inout GraphicsContext) {
        let points = stroke.points.map { CGPoint(x: box.minX + $0.x * box.width, y: box.minY + $0.y * box.height) }
        guard let first = points.first else { return }
        func outline(_ path: Path, _ width: CGFloat = 0) {
            context.stroke(path, with: .color(stroke.color), style: StrokeStyle(lineWidth: width > 0 ? width : stroke.width, lineCap: .round, lineJoin: .round))
        }
        if stroke.region {
            var path = Path(); path.move(to: first)
            for point in points.dropFirst() { path.addLine(to: point) }; path.closeSubpath()
            context.fill(path, with: .color(stroke.color.opacity(0.18)), style: FillStyle(eoFill: true)); outline(path)
            if stroke.ordinal > 0 {
                let time = stroke.createdAt.map { $0.formatted(date: .omitted, time: .shortened) } ?? ""
                let anchor = CGPoint(x: max(box.minX + 4, points.map(\.x).min() ?? first.x),
                                     y: max(box.minY + 4, (points.map(\.y).min() ?? first.y) - 16))
                context.draw(Text("#\(stroke.ordinal) \(time)").font(.caption2.bold()).foregroundStyle(stroke.color), at: anchor, anchor: .topLeading)
            }
        } else if stroke.kind == "line" || stroke.kind == "arrow", points.count >= 2 {
            let end = points[1]
            var path = Path(); path.move(to: first); path.addLine(to: end)
            if stroke.kind == "arrow" {
                let angle = atan2(end.y - first.y, end.x - first.x), head = max(9, stroke.width * 3.5)
                for offset in [CGFloat(-0.42), CGFloat(0.42)] {
                    path.move(to: end)
                    path.addLine(to: CGPoint(x: end.x - head * cos(angle + offset), y: end.y - head * sin(angle + offset)))
                }
            }
            outline(path)
        } else if stroke.kind == "rect", points.count >= 2 {
            let end = points[1]
            outline(Path(CGRect(x: min(first.x,end.x), y: min(first.y,end.y), width: abs(first.x-end.x), height: abs(first.y-end.y))))
        } else if stroke.kind == "pen", points.count == 1 {
            context.fill(Path(ellipseIn: CGRect(x: first.x - stroke.width / 2, y: first.y - stroke.width / 2, width: stroke.width, height: stroke.width)), with: .color(stroke.color))
        } else if stroke.kind == "pen", points.count >= 2 {
            func midpoint(_ a: CGPoint, _ b: CGPoint) -> CGPoint { CGPoint(x: (a.x+b.x)/2, y: (a.y+b.y)/2) }
            var start = first
            if points.count > 2 {
                for index in 1..<(points.count - 1) {
                    let end = midpoint(points[index], points[index + 1])
                    var path = Path(); path.move(to: start); path.addQuadCurve(to: end, control: points[index])
                    outline(path, stroke.widths.count == points.count ? stroke.widths[index] : stroke.width)
                    start = end
                }
            }
            var tail = Path(); tail.move(to: start); tail.addLine(to: points[points.count-1])
            outline(tail, stroke.widths.count == points.count ? stroke.widths[points.count-1] : stroke.width)
        }
    }
}
