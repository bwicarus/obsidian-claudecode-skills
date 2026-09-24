import SwiftUI

enum ReaderNativePDFToolbar {
    // Keep the old stable keys/titles so the user's pinned tools survive.
    static let entries: [(String, String, String)] = [
        ("spread", "spread-toggle", "双页并排(连续滚动)。再点错开 facing 页(12↔23);第三下回单列连续"),
        ("favorite", "", "收藏当前页到收藏夹（书架「⭐ 收藏夹」tab 可查看）"),
        ("fit", "", "按页宽自适应（取消手动缩放）"),
        ("ruby", "ruby-toggle", "振假名 / 英文音标叠加（汉字上方注假名读音、英文词上方注音标）"),
        ("translation", "pagetr-toggle", "整页翻译：当前页所有句子就地显示中文（再按关闭）"),
        ("crop", "crop-toggle", "去边阅读模式：隐藏每页左/右/上/下边距（在 ⚙ 设置里配各边百分比）"),
        ("search", "", "全文搜索（全书）"),
        ("note", "note-new", "便签（在当前视野中央新建;单击短条折叠/展开,长按短条移动/删除,长按内容区编辑）"),
        ("insert", "", "插入我的页（真正写进 PDF:页数变化、所有阅读器可见;已有高亮/便签/墨迹等页锚自动迁移,原书先备份;页角 📝 可编辑/删除）"),
        ("edit", "native-userpage-edit", "编辑或删除当前我的页"),
        ("library", "", "书籍：回到书架 / 打开书库"),
        ("settings", "", "AI 设置")
    ]
    static func controls() -> [ReaderNativeControl] {
        entries.compactMap { ReaderNativeControl(["id": "native-pdf-" + $0.0, "key": $0.1, "title": $0.2]) }
    }
}

/// PDF toolbar forms own their input and committed state. No hidden HTML
/// controls are created, clicked or read by these forms.
@MainActor
final class ReaderNativePDFToolPanel: ObservableObject, Identifiable {
    let id = UUID()
    let kind: String
    let page: Int
    let request: (String, [String: Any]) async throws -> [String: Any]
    @Published var title = ""
    @Published var markdown = ""
    @Published var name = ""
    @Published var folders: [[String: Any]] = []
    @Published var busy = false
    @Published var loaded = false
    @Published var error: String?
    private var recordID = ""
    private var revision = 0
    private var savedTitle = ""
    private var savedMarkdown = ""
    private var saveTask: Task<Void, Never>?

    init(kind: String, page: Int, request: @escaping (String, [String: Any]) async throws -> [String: Any]) {
        self.kind = kind; self.page = page; self.request = request
    }
    func load() async {
        guard !loaded, !busy else { return }
        busy = true; defer { busy = false }
        do {
            let value = try await request("load", [:])
            if kind == "favorite" { folders = value["folders"] as? [[String: Any]] ?? [] }
            else {
                guard let record = value["record"] as? [String: Any], let id = record["id"] as? String else {
                    throw ReaderNativeBookStore.MutationError.invalid("用户页记录缺失")
                }
                recordID = id; revision = (record["md_ver"] as? NSNumber)?.intValue ?? 0
                title = record["title"] as? String ?? ""; markdown = record["md"] as? String ?? ""
                savedTitle = title; savedMarkdown = markdown
            }
            loaded = true; error = nil
        } catch { self.error = error.localizedDescription }
    }
    func changed() {
        guard loaded, kind != "favorite" else { return }
        saveTask?.cancel()
        saveTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 600_000_000) } catch { return }
            guard let self else { return }
            _ = await self.save()
        }
    }
    @discardableResult
    func save() async -> Bool {
        guard loaded, !busy else { return false }
        busy = true; defer { busy = false }
        do {
            while savedTitle != title || savedMarkdown != markdown {
                let nextTitle = title, nextMarkdown = markdown
                let value = try await request("save", ["id": recordID, "revision": revision, "title": nextTitle, "md": nextMarkdown])
                guard let version = value["revision"] as? Int else { throw ReaderNativeBookStore.MutationError.invalid("保存回执") }
                revision = version; savedTitle = nextTitle; savedMarkdown = nextMarkdown
            }
            error = nil; return true
        } catch { self.error = error.localizedDescription; return false }
    }
    func finish(delete: Bool = false) async -> Bool {
        guard !busy else { return false }
        saveTask?.cancel()
        if !delete, !(await save()) { return false }
        busy = true; defer { busy = false }
        do {
            _ = try await request(delete ? "delete" : "finish", ["id": recordID, "revision": revision])
            return true
        } catch { self.error = error.localizedDescription; return false }
    }
    func favorite(folder: String? = nil, selected: Bool = false) async {
        guard loaded, !busy else { return }
        busy = true; defer { busy = false }
        do {
            let value = try await request("favorite", folder.map { ["folder": $0, "selected": selected] } ?? ["name": name])
            guard let row = value["folder"] as? [String: Any], let id = row["id"] as? String else {
                throw ReaderNativeBookStore.MutationError.invalid("收藏回执")
            }
            if let at = folders.firstIndex(where: { $0["id"] as? String == id }) { folders[at] = row }
            else { folders.insert(row, at: 0) }
            name = ""; error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct ReaderNativePDFToolView: View {
    @ObservedObject var model: ReaderNativePDFToolPanel
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false
    var body: some View {
        NavigationStack {
            Form {
                if let error = model.error { Text(error).foregroundStyle(.red) }
                if !model.loaded {
                    if model.busy { ProgressView("正在读取…") }
                    else { Button("重试") { Task { await model.load() } } }
                } else if model.kind == "favorite" {
                    Section("收藏到…") {
                        ForEach(Array(model.folders.enumerated()), id: \.offset) { _, row in
                            let selected = row["selected"] as? Bool == true
                            Button {
                                Task { await model.favorite(folder: row["id"] as? String, selected: !selected) }
                            } label: {
                                HStack {
                                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                                    Text(row["name"] as? String ?? "未命名")
                                    Spacer()
                                    Text("\((row["items"] as? [Any] ?? []).count) 条").foregroundStyle(.secondary)
                                }
                            }.disabled(model.busy)
                        }
                        HStack {
                            TextField("新建收藏夹", text: $model.name)
                            Button("新建") { Task { await model.favorite() } }
                                .disabled(model.busy || model.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        }
                    }
                } else {
                    Section("第 \(model.page) 页 · 自动保存") {
                        TextField("标题", text: $model.title).onChange(of: model.title) { _, _ in model.changed() }
                        TextEditor(text: $model.markdown).frame(minHeight: 320)
                            .font(.body).onChange(of: model.markdown) { _, _ in model.changed() }
                    }
                    Button("删除这一页", role: .destructive) { confirmDelete = true }.disabled(model.busy)
                }
            }
            .navigationTitle(model.kind == "favorite" ? "收藏当前页" : "我的页")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(model.kind == "favorite" ? "完成" : "完成并写入 PDF") {
                        if model.kind == "favorite" { dismiss() }
                        else { Task { if await model.finish() { dismiss() } } }
                    }.disabled(model.busy || !model.loaded)
                }
                if !model.loaded { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() }.disabled(model.busy) } }
            }
            .alert("删除这一页？PDF 页码及批注位置会随之更新。", isPresented: $confirmDelete) {
                Button("删除", role: .destructive) { Task { if await model.finish(delete: true) { dismiss() } } }
                Button("取消", role: .cancel) {}
            }
            .task { await model.load() }
        }
        .interactiveDismissDisabled(model.kind != "favorite" || model.busy)
    }
}
