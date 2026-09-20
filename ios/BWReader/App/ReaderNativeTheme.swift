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
