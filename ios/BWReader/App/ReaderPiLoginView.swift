import AuthenticationServices
import SwiftUI
import WebKit

struct ReaderPiLoginView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var model: ReaderAppleSignInModel
    @State private var createAccount = false
    @State private var username = ""
    @State private var password = ""
    @State private var invite = ""

    init(dataStore: WKWebsiteDataStore) {
        _model = StateObject(wrappedValue: ReaderAppleSignInModel(dataStore: dataStore))
    }

    var body: some View {
        NavigationStack {
            Form {
                if model.signedIn {
                    Section {
                        Label("已登录 · " + model.username, systemImage: "checkmark.circle.fill")
                            .foregroundStyle(ReaderNativeTheme.accent)
                        Text("现有书库、收藏和学习记录仍属于同一个账户。")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                } else if model.needsLink {
                    Section {
                        Text("首次使用 Apple 登录，请关联原账户。以后即可直接通过 Apple 登录。")
                            .font(.subheadline)
                        Picker("账户", selection: $createAccount) {
                            Text("关联原账户").tag(false)
                            Text("受邀创建账户").tag(true)
                        }.pickerStyle(.segmented)
                        TextField("Reader 用户名", text: $username)
                            .textContentType(.username).textInputAutocapitalization(.never).autocorrectionDisabled()
                        if createAccount {
                            TextField("邀请码", text: $invite).textInputAutocapitalization(.never).autocorrectionDisabled()
                        } else {
                            SecureField("原账户密码", text: $password).textContentType(.password)
                        }
                        Button(createAccount ? "创建并使用 Apple 登录" : "关联并登录") {
                            Task {
                                await model.link(username: username, password: password, invite: createAccount ? invite : "")
                                password = ""
                            }
                        }.disabled(model.busy || username.isEmpty || (createAccount ? invite.isEmpty : password.isEmpty))
                        Button("重新使用 Apple 登录") {
                            password = ""
                            Task { await model.prepare() }
                        }.disabled(model.busy)
                    }
                } else {
                    Section {
                        Text(model.linking ? "将 Apple 账户关联到当前 Reader 账户" : "登录 Reader").font(.headline)
                        SignInWithAppleButton(.continue) { request in model.configure(request) }
                        onCompletion: { result in Task { await model.complete(result) } }
                            .signInWithAppleButtonStyle(colorScheme == .dark ? .white : .black)
                            .frame(height: 48).disabled(!model.ready || model.busy)
                        Text("Apple 身份仅用于登录。原有账户与数据不会按邮箱自动合并。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if model.busy { ProgressView("正在处理…") }
                if let error = model.error {
                    Section {
                        Text(error).foregroundStyle(.red).textSelection(.enabled)
                        if !model.needsLink { Button("重试") { Task { await model.prepare() } }.disabled(model.busy) }
                    }
                }
            }
            .scrollContentBackground(.hidden).background(ReaderNativeTheme.canvas)
            .navigationTitle("账户").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() }.disabled(model.busy) } }
            .task { await model.prepare() }
        }.tint(ReaderNativeTheme.accent)
    }
}
