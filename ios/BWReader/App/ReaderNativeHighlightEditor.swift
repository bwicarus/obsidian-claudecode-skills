import SwiftUI

/// 划线编辑面板（点已有划线弹出）。
///
/// 接管后 `.hl-layer` 不存在，网页那条「点划线弹浮层」的路整条断掉 —— 划得上去、
/// 改不了也删不掉。这里是原生唯一能改到划线的地方。
///
/// ⚠ **落库全走底座** `_hlUpdate` / `_hlDelete`（同一条 PATCH/DELETE、同一套
/// 「点当前色＝取消颜色」语义）。原生不另写保存：颜色键是用户自己的墨水，
/// 存在他的笔记里，另写一套等于两份规则各自漂移。
@MainActor
final class ReaderNativeHighlightEditorModel: ObservableObject, Identifiable {
    let id: String
    let page: Int
    let text: String
    private let request: ([String: Any]) async -> [String: Any]

    /// 四支笔的键名。⚠ 与阅读器色板一一对应，不可改名改值。
    static let palette: [(key: String, title: String, color: Color)] = [
        ("yellow", "黄", Color(red: 1.0, green: 0.96, blue: 0.62)),
        ("green", "绿", Color(red: 0.72, green: 0.94, blue: 0.72)),
        ("blue", "蓝", Color(red: 0.70, green: 0.87, blue: 1.0)),
        ("pink", "粉", Color(red: 1.0, green: 0.78, blue: 0.87)),
    ]

    @Published var note: String
    @Published private(set) var colorKey: String
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var deleted = false

    init(highlight: ReaderNativePDFDocument.Highlight,
         request: @escaping ([String: Any]) async -> [String: Any]) {
        self.id = highlight.id
        self.page = highlight.page
        self.text = highlight.text
        self.note = highlight.note
        self.colorKey = highlight.colorKey
        self.request = request
    }

    /// 改完要让正文重取一次（颜色/虚框/条目消失都是这一步才看得见）。
    var onChanged: (() -> Void)?

    /// 点色板。点的是**当前色**时是「取消颜色」：有备注留虚框，没备注整条删掉 ——
    /// 这条语义在网页那侧，这里只是把空字符串递过去让它决定。
    func pick(_ key: String) async {
        await send(op: "color", value: key == colorKey ? "" : key)
    }

    func saveNote() async {
        await send(op: "note", value: note)
    }

    func delete() async {
        await send(op: "delete", value: "")
    }

    private func send(op: String, value: String) async {
        guard !busy, !deleted else { return }
        busy = true
        defer { busy = false }
        let receipt = await request([
            "action": "nativeHighlightEdit",
            "value": ["id": id, "op": op, "value": value],
        ])
        guard receipt["ok"] as? Bool == true, let body = receipt["value"] as? [String: Any] else {
            // 「删除未确认」和「删除失败」都不能当成删掉了：底座那边就是为此才把
            // 三条路的返回值区分开的（假删的观感是刷新后它又回来了）。
            error = receipt["error"] as? String ?? "操作未完成，请重试。"
            return
        }
        if body["deleted"] as? Bool == true {
            deleted = true
        } else {
            colorKey = body["color"] as? String ?? colorKey
            note = body["note"] as? String ?? note
        }
        onChanged?()
    }
}

struct ReaderNativeHighlightEditor: View {
    @ObservedObject var model: ReaderNativeHighlightEditorModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let error = model.error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    if !model.text.isEmpty {
                        Text(model.text).font(.callout)
                            .foregroundStyle(ReaderNativeTheme.muted)
                            .textSelection(.enabled)
                    }
                    HStack(spacing: 12) {
                        ForEach(ReaderNativeHighlightEditorModel.palette, id: \.key) { pen in
                            Button {
                                Task { await model.pick(pen.key) }
                            } label: {
                                Circle()
                                    .fill(pen.color)
                                    .frame(width: 30, height: 30)
                                    .overlay(
                                        Circle().strokeBorder(
                                            model.colorKey == pen.key
                                                ? ReaderNativeTheme.ink : Color.clear,
                                            lineWidth: 2)
                                    )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(pen.title + (model.colorKey == pen.key ? "（当前，点此取消颜色）" : ""))
                        }
                    }
                    .disabled(model.busy || model.deleted)
                    Text(model.colorKey.isEmpty
                         ? "当前没有颜色：只保留备注，正文上画虚框。"
                         : "再点一次当前颜色＝取消颜色（有备注则留虚框，没备注整条删掉）。")
                        .font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                    Divider()
                    Text("备注").font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                    TextEditor(text: $model.note)
                        .frame(minHeight: 90)
                        .padding(6)
                        .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 8))
                        .disabled(model.busy || model.deleted)
                    HStack {
                        Button("保存备注") { Task { await model.saveNote() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.busy || model.deleted)
                        Spacer()
                        Button(role: .destructive) {
                            Task { await model.delete() }
                        } label: {
                            Label("删除划线", systemImage: "trash")
                        }
                        .disabled(model.busy || model.deleted)
                    }
                    if model.deleted {
                        Label("已删除", systemImage: "checkmark.circle")
                            .foregroundStyle(ReaderNativeTheme.muted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
            .scrollContentBackground(.hidden)
            .background(ReaderNativeTheme.canvas)
            .navigationTitle("划线")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .onChange(of: model.deleted) { _, deleted in
                if deleted { dismiss() }
            }
        }
        .tint(ReaderNativeTheme.accent)
        .presentationDetents([.medium, .large])
    }
}
