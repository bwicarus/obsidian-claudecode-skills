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
    @ViewBuilder
    func readerGlass<S: Shape, F: ShapeStyle>(in shape: S, fallback: F) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(in: shape)
        } else {
            self.background(fallback, in: shape)
        }
    }
}
