import AppIntents
import Foundation

private enum ReaderIntentDispatcher {
    static func enqueue(_ action: ReaderNativeFeatureAction) throws {
        try ReaderNativeFeatureStore().enqueue(action)
    }
}

struct OpenReaderIntent: AppIntent {
    static var title: LocalizedStringResource = "打开 BW 阅读器"
    static var description = IntentDescription(
        "打开 BW 阅读器，并返回最近一次阅读的位置。"
    )
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try ReaderIntentDispatcher.enqueue(.openReader)
        return .result(dialog: "正在打开 BW 阅读器")
    }
}

struct ScanCurrentPageIntent: AppIntent {
    static var title: LocalizedStringResource = "识别当前页面"
    static var description = IntentDescription(
        "打开 BW 阅读器并识别当前可见页面中的文字。"
    )
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try ReaderIntentDispatcher.enqueue(.scanCurrentPage)
        return .result(dialog: "正在打开当前页面识别")
    }
}

struct AnnotateCurrentPageIntent: AppIntent {
    static var title: LocalizedStringResource = "批注当前页面"
    static var description = IntentDescription(
        "打开 BW 阅读器的原生 Apple Pencil 页面批注。"
    )
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try ReaderIntentDispatcher.enqueue(.annotateCurrentPage)
        return .result(dialog: "正在打开页面批注")
    }
}

struct OpenNativeToolsIntent: AppIntent {
    static var title: LocalizedStringResource = "打开阅读工具"
    static var description = IntentDescription(
        "打开 BW 阅读器的原生识别、翻译与 Apple Pencil 工具。"
    )
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try ReaderIntentDispatcher.enqueue(.openNativeTools)
        return .result(dialog: "正在打开阅读工具")
    }
}

struct AddQuickNoteIntent: AppIntent {
    static var title: LocalizedStringResource = "添加 BW 速记"
    static var description = IntentDescription(
        "把一条文字保存到 BW 阅读器的本地速记中。"
    )
    static var openAppWhenRun = false

    @Parameter(
        title: "速记内容",
        requestValueDialog: "要记录什么？"
    )
    var text: String

    static var parameterSummary: some ParameterSummary {
        Summary("记录 \(\.$text)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let note = ReaderQuickNote(text: text)
        guard !note.text.isEmpty else {
            return .result(dialog: "没有可保存的文字")
        }
        try ReaderNativeFeatureStore().appendQuickNote(note)
        return .result(dialog: "已保存到 BW 阅读器速记")
    }
}

struct BWReaderAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenReaderIntent(),
            phrases: [
                "用 \(.applicationName) 打开阅读器",
                "在 \(.applicationName) 继续阅读",
            ],
            shortTitle: "打开阅读器",
            systemImageName: "book"
        )
        AppShortcut(
            intent: ScanCurrentPageIntent(),
            phrases: [
                "用 \(.applicationName) 识别当前页面",
            ],
            shortTitle: "识别页面",
            systemImageName: "text.viewfinder"
        )
        AppShortcut(
            intent: AnnotateCurrentPageIntent(),
            phrases: [
                "用 \(.applicationName) 批注当前页面",
            ],
            shortTitle: "页面批注",
            systemImageName: "pencil.tip.crop.circle"
        )
        AppShortcut(
            intent: OpenNativeToolsIntent(),
            phrases: [
                "用 \(.applicationName) 打开阅读工具",
            ],
            shortTitle: "阅读工具",
            systemImageName: "wrench.and.screwdriver"
        )
        AppShortcut(
            intent: AddQuickNoteIntent(),
            phrases: [
                "用 \(.applicationName) 添加速记",
            ],
            shortTitle: "添加速记",
            systemImageName: "square.and.pencil"
        )
    }
}
