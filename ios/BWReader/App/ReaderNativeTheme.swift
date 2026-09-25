import SwiftUI
import UIKit

/// Shared palette for the native Reader shell. Follow the system appearance.
enum ReaderNativeTheme {
    static let accent = adaptive(
        light: UIColor(red: 0.13, green: 0.40, blue: 0.38, alpha: 1),
        dark: UIColor(red: 0.48, green: 0.78, blue: 0.73, alpha: 1)
    )

    static var canvas: Color {
        adaptive(
            light: UIColor(red: 0.965, green: 0.968, blue: 0.972, alpha: 1),
            dark: UIColor(red: 0.075, green: 0.085, blue: 0.095, alpha: 1)
        )
    }

    static var card: Color {
        adaptive(
            light: .white,
            dark: UIColor(red: 0.12, green: 0.135, blue: 0.15, alpha: 1)
        )
    }

    static var ink: Color { Color(uiColor: .label) }
    static var muted: Color { Color(uiColor: .secondaryLabel) }
    static var subtle: Color { Color(uiColor: .tertiaryLabel) }
    static var separator: Color { Color(uiColor: .separator) }
    static var accentWash: Color { accent.opacity(0.10) }

    private static func adaptive(light: UIColor, dark: UIColor) -> Color {
        Color(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? dark : light
        })
    }
}

extension View {
    /// Liquid Glass（iOS 26+）。用在**控件层**：浮在正文上的页卡与提示、顶栏上的
    /// 工具按钮、边界上的把手。不要拿它铺整块面板（侧栏卡片、复习卡这些已经
    /// 坐在不透明底上），那只是白白多一层模糊，既不好看也不省电。
    ///
    /// ⚠ 部署目标是 iOS 17，所以必须带 `#available`；低版本**原样退回**调用方
    /// 给的底色，不做"像玻璃的假玻璃"（半透明 + blur 在旧系统上既不省电也不好看）。
    /// 页卡卡面。2026-09-23 用户："既然使用 liquid glass 了就把玻璃效果做的好看点"
    /// "不一定要严格遵照过去的样式"。
    ///
    /// iOS 26：卡片**本身就是玻璃**——
    ///   ① 一层淡淡染了卡片色调的 Liquid Glass（透得出下面的书页）；
    ///   ② 玻璃上一层上浅下深的色调渐层，给正文垫底（白纸页上浅色字也清楚）；
    ///   ③ 高光描边：左上亮、顺着色调、到右下几乎隐去 —— 光从左上打来。
    /// ⚠ 旧版把玻璃染成近乎不透明的深灰（色调 15% 混 rgba(28,30,34,.9)），玻璃等于没有：
    ///   看起来就是一块平涂。
    /// ⚠ 内容先按形状裁，再垫玻璃：裁在玻璃**外面**会把玻璃自己的边缘高光裁掉。
    /// 更早的系统：原版平涂卡面 + 细描边（不做"像玻璃的假玻璃"）。
    @ViewBuilder
    func readerCardGlass<S: InsettableShape>(tone: Color, fallbackFill: Color, fallbackBorder: Color,
                                            enabled: Bool, in shape: S) -> some View {
        if !enabled {
            self
        } else if #available(iOS 26.0, *) {
            // 2026-09-26 用户（迁移前笔记）：「优先复现原网页卡片的视觉设计…玻璃适度，
            // 不要牺牲原有的字体和整体观感」。卡面回到原版 .vc-card 的深色底
            // rgba(30,30,34)（这里 0.8，留一点透光），玻璃只薄薄一层；高光描边收淡。
            self
                .background(LinearGradient(colors: [tone.opacity(0.10), Color.clear],
                                           startPoint: .top, endPoint: .bottom), in: shape)
                .background(Color(red: 30 / 255, green: 30 / 255, blue: 34 / 255).opacity(0.80), in: shape)
                .clipShape(shape)
                .glassEffect(.regular.tint(tone.opacity(0.10)), in: shape)
                .overlay(shape.strokeBorder(
                    LinearGradient(colors: [Color.white.opacity(0.30), Color.white.opacity(0.14), Color.white.opacity(0.06)],
                                   startPoint: .topLeading, endPoint: .bottomTrailing),
                    lineWidth: 0.5))
        } else {
            self.background(fallbackFill, in: shape)
                .clipShape(shape)
                .overlay(shape.stroke(fallbackBorder, lineWidth: 0.5))
        }
    }

    /// 卡上的小圆按钮（删除 / 更多）：iOS 26 是可按压的玻璃圆（系统按压反馈），
    /// 更早的系统退回调用方给的底色。
    @ViewBuilder
    func readerGlassCircle(tint: Color?, fallback: Color) -> some View {
        if #available(iOS 26.0, *) {
            let glass: Glass = tint.map { Glass.regular.tint($0) } ?? Glass.regular
            self.glassEffect(glass.interactive(), in: Circle())
        } else {
            self.background(fallback, in: Circle())
        }
    }

    /// 圆点态那枚标记：iOS 26 是一块染了色调的小玻璃 + 高光描边；更早的系统照原版
    /// `.vc-card-dot`（色调 14% 混 rgba(22,26,38,.38)）。
    @ViewBuilder
    func readerCardDot(tone: Color, border: Color) -> some View {
        let shape = RoundedRectangle(cornerRadius: 13)
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular.tint(tone.opacity(0.32)), in: shape)
                .overlay(shape.strokeBorder(
                    LinearGradient(colors: [Color.white.opacity(0.55), tone.opacity(0.35)],
                                   startPoint: .topLeading, endPoint: .bottomTrailing),
                    lineWidth: 0.8))
        } else {
            self.background(tone.opacity(0.14), in: shape)
                .background(Color(red: 22 / 255, green: 26 / 255, blue: 38 / 255).opacity(0.38), in: shape)
                .overlay(shape.stroke(border, lineWidth: 0.5))
        }
    }

    @ViewBuilder
    func readerGlass<S: Shape, F: ShapeStyle>(in shape: S, fallback: F) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(in: shape)
        } else {
            self.background(fallback, in: shape)
        }
    }
}
