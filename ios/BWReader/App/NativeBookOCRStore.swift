import CryptoKit
import Foundation

struct NativeBookOCRPDFMutationLease: Codable, Equatable, Sendable {
    let bookID: String
    let token: String
    let oldContentSHA256: String
}

struct NativeBookOCRPDFMutationStageReceipt: Sendable {
    let hasSource: Bool
    let hadTarget: Bool
}

actor NativeBookOCRSidecarStore {
    static let shared = NativeBookOCRSidecarStore()

    private static let maximumAttachments = 5_001
    private static let maximumAttachmentBytes = 32 * 1_024 * 1_024
    private static let maximumBundleBytes = 512 * 1_024 * 1_024

    private let fileManager: FileManager
    private let rootURL: URL
    private var pdfMutationLeases: [String: NativeBookOCRPDFMutationLease] = [:]
    private var pdfMutationLeasesByDigest:
        [String: [String: NativeBookOCRPDFMutationLease]] = [:]
    private var pdfMutationTargetLeaseByDigest:
        [String: NativeBookOCRPDFMutationLease] = [:]

    init(
        fileManager: FileManager = .default,
        rootURL: URL? = nil
    ) {
        self.fileManager = fileManager
        let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        self.rootURL = rootURL ?? applicationSupport.appendingPathComponent(
            "BWReader/NativeBookOCR",
            isDirectory: true
        )
    }

    /// The store actor is the serialization point for every OCR sidecar write.
    /// Calling this after the manager has drained its tasks also waits for all
    /// earlier store messages before fencing later writes for this book.
    func beginPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validatePDFMutationLease(lease)
        if let active = pdfMutationLeases[lease.bookID] {
            guard active == lease else {
                throw NativeBookOCRError.storage(
                    "本书已有另一项 PDF 改页 OCR 租约"
                )
            }
            return
        }
        try registerPDFMutationDigestLease(
            lease.oldContentSHA256,
            lease: lease
        )
        pdfMutationLeases[lease.bookID] = lease
    }

    func stagePDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        transform: @Sendable (URL) throws -> Void
    ) throws -> NativeBookOCRPDFMutationStageReceipt {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validateContentSHA256(stagedContentSHA256)
        try validatePDFMutationTicket(ticket)
        let stagedDigest = stagedContentSHA256.lowercased()
        if let active = pdfMutationTargetLeaseByDigest[stagedDigest],
           active != lease {
            throw NativeBookOCRError.storage(
                "目标 PDF 内容已有另一项 OCR 改页租约"
            )
        }
        try registerPDFMutationDigestLease(stagedDigest, lease: lease)
        pdfMutationTargetLeaseByDigest[stagedDigest] = lease

        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        let source = contentRoot.appendingPathComponent(
            lease.oldContentSHA256.lowercased(),
            isDirectory: true
        )
        let staging = contentRoot.appendingPathComponent(
            ".bw-pdf-mutation-\(ticket).staging",
            isDirectory: true
        )
        let target = contentRoot.appendingPathComponent(
            stagedDigest,
            isDirectory: true
        )
        let backup = contentRoot.appendingPathComponent(
            ".bw-pdf-mutation-\(ticket).backup",
            isDirectory: true
        )
        let hasSource = lease.oldContentSHA256.lowercased() != stagedDigest
            && fileManager.fileExists(atPath: source.path)
        let hadTarget = fileManager.fileExists(atPath: target.path)
        guard hasSource else {
            return NativeBookOCRPDFMutationStageReceipt(
                hasSource: false,
                hadTarget: hadTarget
            )
        }
        do {
            try fileManager.createDirectory(
                at: contentRoot,
                withIntermediateDirectories: true
            )
            guard !fileManager.fileExists(atPath: staging.path),
                  !fileManager.fileExists(atPath: backup.path) else {
                throw NativeBookOCRError.storage(
                    "本机 OCR staging/backup 已存在"
                )
            }
            try fileManager.copyItem(at: source, to: staging)
            try transform(staging)
            return NativeBookOCRPDFMutationStageReceipt(
                hasSource: true,
                hadTarget: hadTarget
            )
        } catch {
            try? fileManager.removeItem(at: staging)
            throw error
        }
    }

    func installPDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        hasSource: Bool,
        hadTarget: Bool
    ) throws {
        try assertPDFMutationFileAccess(
            lease: lease,
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        guard hasSource else { return }
        let urls = pdfMutationURLs(
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        guard fileManager.fileExists(atPath: urls.staging.path) else {
            throw NativeBookOCRError.storage("本机 OCR staging 不存在")
        }
        if hadTarget {
            guard fileManager.fileExists(atPath: urls.target.path),
                  !fileManager.fileExists(atPath: urls.backup.path) else {
                throw NativeBookOCRError.storage(
                    "本机 OCR 目标/备份状态不一致"
                )
            }
            try fileManager.moveItem(at: urls.target, to: urls.backup)
        } else if fileManager.fileExists(atPath: urls.target.path) {
            throw NativeBookOCRError.storage(
                "本机 OCR 目标在事务期间意外出现"
            )
        }
        try fileManager.moveItem(at: urls.staging, to: urls.target)
    }

    func rollbackPDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        hadTarget: Bool,
        mayHaveInstalled: Bool
    ) throws {
        try assertPDFMutationFileAccess(
            lease: lease,
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        let urls = pdfMutationURLs(
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        if fileManager.fileExists(atPath: urls.backup.path) {
            if fileManager.fileExists(atPath: urls.target.path) {
                try fileManager.removeItem(at: urls.target)
            }
            try fileManager.moveItem(at: urls.backup, to: urls.target)
        } else if !hadTarget, mayHaveInstalled,
                  fileManager.fileExists(atPath: urls.target.path) {
            try fileManager.removeItem(at: urls.target)
        }
        try? fileManager.removeItem(at: urls.staging)
    }

    func cleanupPDFMutationArtifacts(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String?
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validatePDFMutationTicket(ticket)
        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        for suffix in [".staging", ".backup"] {
            let url = contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket)\(suffix)",
                isDirectory: true
            )
            if fileManager.fileExists(atPath: url.path) {
                try fileManager.removeItem(at: url)
            }
        }
        if let stagedContentSHA256 {
            try validateContentSHA256(stagedContentSHA256)
        }
    }

    func finishPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validatePDFMutationLease(lease)
        guard let active = pdfMutationLeases[lease.bookID] else {
            let conflicting = pdfMutationLeasesByDigest.values.contains {
                $0[lease.bookID] != nil
            } || pdfMutationTargetLeaseByDigest.values.contains {
                $0.bookID == lease.bookID
            }
            guard !conflicting else {
                throw NativeBookOCRError.storage(
                    "PDF 改页 OCR 租约索引状态不一致"
                )
            }
            return
        }
        guard active == lease else {
            throw NativeBookOCRError.storage(
                "拒绝结束另一项 PDF 改页 OCR 租约"
            )
        }
        pdfMutationLeases.removeValue(forKey: lease.bookID)
        for digest in Array(pdfMutationLeasesByDigest.keys) {
            guard var leases = pdfMutationLeasesByDigest[digest],
                  leases[lease.bookID] == lease else { continue }
            leases.removeValue(forKey: lease.bookID)
            if leases.isEmpty {
                pdfMutationLeasesByDigest.removeValue(forKey: digest)
            } else {
                pdfMutationLeasesByDigest[digest] = leases
            }
        }
        pdfMutationTargetLeaseByDigest =
            pdfMutationTargetLeaseByDigest.filter { $0.value != lease }
    }

    func layerState(
        contentSHA256: String
    ) throws -> NativeBookOCRLayerState {
        try validateContentSHA256(contentSHA256)
        let digest = contentSHA256.lowercased()
        var available = [NativeBookOCRLayerMetadata(
            schema: NativeBookOCRLayerMetadata.schema,
            contentSHA256: digest,
            layer: .embedded,
            engine: "pdf-embedded",
            executor: nil,
            processingProfile: nil,
            revision: "pdfjs-embedded/1",
            pageCount: 0,
            updatedAt: .distantPast
        )]
        let legacyPages = try pages(contentSHA256: digest, layer: .legacy)
        if !legacyPages.isEmpty {
            available.append(NativeBookOCRLayerMetadata(
                schema: NativeBookOCRLayerMetadata.schema,
                contentSHA256: digest,
                layer: .legacy,
                engine: "mixed",
                executor: nil,
                processingProfile: nil,
                revision: "legacy-pages/1",
                pageCount: legacyPages.count,
                updatedAt: legacyPages.map(\.createdAt).max() ?? .distantPast
            ))
        }
        for layer in [
            NativeBookOCRLayerID.appleVision,
            NativeBookOCRLayerID.pi,
            NativeBookOCRLayerID.pc,
        ] {
            guard let metadata = try storedLayerMetadata(
                contentSHA256: digest,
                layer: layer
            ) else { continue }
            let stored = try pages(contentSHA256: digest, layer: layer)
            guard !stored.isEmpty, metadata.pageCount == stored.count else {
                // 这一层坏了（页数与元数据对不上，或删到一半），**跳过它**，
                // 不要连累别的层。
                //
                // 旧行为是 throw；而 page() 每次读页第一行就调 layerState()，
                // 于是一层不一致 = 整本书每一页文字层全抛。删除功能上线后这条
                // 路径会被真实走到（删层过程中断、Pi 那份被删而本机还留着）。
                continue
            }
            available.append(metadata)
        }
        var selected: NativeBookOCRLayerID
        if let persisted = try loadLayerSelection(contentSHA256: digest) {
            selected = persisted.selected
        } else {
            selected = legacyPages.isEmpty ? .embedded : .legacy
        }
        if !available.contains(where: { $0.layer == selected }) {
            // 选中的层已经不在了（被删、或上面那一步跳过了）。**静默回落**，
            // 不要抛 —— 抛出去的后果是整本书读不出文字层，而回落最多是
            // 少了一层可选项。PDF 原文字层永远在 available 里，所以这个回落
            // 一定有落点。
            selected = available.contains(where: { $0.layer == .legacy })
                ? .legacy
                : .embedded
        }
        return NativeBookOCRLayerState(
            contentSHA256: digest,
            selected: selected,
            available: available
        )
    }

    /// 删除本机已导入的某个文字层。
    ///
    /// 用户 2026-08-19：在服务器上删掉一份结果之后，「当前使用」里那一项还在 ——
    /// 因为那个选择器列的是**本机已导入的层**，跟服务器上的结果是两回事。
    /// Pi 删了、iPad 上的副本还在，而且可能仍在被使用。
    ///
    /// 顺序是要害：**先把选择挪走，再删目录**。反过来的话中间那一瞬
    /// layerState() 会看到"选中的层不存在"，而 page() 每次读页都调它。
    /// （那个不一致现在会静默回落而不是抛，但仍然不该主动制造。）
    func deleteLayer(
        bookID: String,
        contentSHA256: String,
        layer: NativeBookOCRLayerID
    ) throws -> NativeBookOCRLayerState {
        guard [.appleVision, .pi, .pc].contains(layer) else {
            // 内嵌层与兼容旧结果不是"导入进来的一份"，没有可删的目录。
            throw NativeBookOCRError.storage("这一层不能删除")
        }
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: contentSHA256
        )
        let current = try layerState(contentSHA256: contentSHA256)
        if current.selected == layer {
            let fallback: NativeBookOCRLayerID =
                current.available.contains(where: { $0.layer == .legacy })
                    ? .legacy
                    : .embedded
            let selection = NativeBookOCRLayerSelection(
                schema: NativeBookOCRLayerSelection.schema,
                contentSHA256: contentSHA256.lowercased(),
                selected: fallback,
                updatedAt: Date(),
                // 回落是被动发生的（选中的那层被删了），不是用户拍板。
                chosenByUser: false
            )
            let url = layerSelectionURL(contentSHA256)
            do {
                try fileManager.createDirectory(
                    at: url.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try encoder().encode(selection).write(to: url, options: .atomic)
            } catch {
                throw NativeBookOCRError.storage(error.localizedDescription)
            }
        }
        let directory = layerDirectory(
            contentSHA256: contentSHA256,
            layer: layer
        )
        if fileManager.fileExists(atPath: directory.path) {
            do {
                try fileManager.removeItem(at: directory)
            } catch {
                throw NativeBookOCRError.storage(error.localizedDescription)
            }
        }
        return try layerState(contentSHA256: contentSHA256)
    }

    /// 刚导入的那一层，在"还没人选过"时自动采纳。
    ///
    /// 用户 2026-08-19 的实况：书里当前用的是 PDF 自带的文字层，它的框比字高
    /// 一大截、相邻两行直接重叠 12.7pt，于是"选下面会一起选上面"，词分组也没有。
    /// 我们跑好的 OCR 层就躺在旁边、几何是准的 —— 但从没被选上，预处理白做。
    ///
    /// 「导入不覆盖当前选择」本身是对的（用户挑过的东西不该被后台改掉），错在
    /// 把"从来没挑过"也算成了一次选择。所以只在这两种情况下自动采纳：
    /// 当前是内嵌层或兼容旧结果，**且**这个选择不是用户自己点出来的。
    func adoptImportedLayerIfUnchosen(
        bookID: String,
        contentSHA256: String,
        layer: NativeBookOCRLayerID
    ) throws -> (state: NativeBookOCRLayerState, adopted: Bool) {
        let current = try layerState(contentSHA256: contentSHA256)
        guard current.available.contains(where: { $0.layer == layer }) else {
            return (current, false)
        }
        if current.selected == layer { return (current, false) }
        guard [.embedded, .legacy].contains(current.selected) else {
            return (current, false)
        }
        let stored = try loadLayerSelection(contentSHA256: contentSHA256)
        if stored?.chosenByUser == true { return (current, false) }
        let state = try selectLayer(
            bookID: bookID,
            contentSHA256: contentSHA256,
            layer: layer
        )
        // selectLayer 会记 chosenByUser: true —— 这一次不是用户点的，改回去，
        // 否则以后就再也不会自动采纳更好的层了。
        let selection = NativeBookOCRLayerSelection(
            schema: NativeBookOCRLayerSelection.schema,
            contentSHA256: contentSHA256.lowercased(),
            selected: layer,
            updatedAt: Date(),
            chosenByUser: false
        )
        let url = layerSelectionURL(contentSHA256)
        do {
            try encoder().encode(selection).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
        return (state, true)
    }

    func selectLayer(
        bookID: String,
        contentSHA256: String,
        layer: NativeBookOCRLayerID
    ) throws -> NativeBookOCRLayerState {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: contentSHA256
        )
        let current = try layerState(contentSHA256: contentSHA256)
        guard current.available.contains(where: { $0.layer == layer }) else {
            throw NativeBookOCRError.storage("选择的文字层尚无可用结果")
        }
        let selection = NativeBookOCRLayerSelection(
            schema: NativeBookOCRLayerSelection.schema,
            contentSHA256: contentSHA256.lowercased(),
            selected: layer,
            updatedAt: Date(),
            chosenByUser: true
        )
        let url = layerSelectionURL(contentSHA256)
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(selection).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
        return NativeBookOCRLayerState(
            contentSHA256: current.contentSHA256,
            selected: layer,
            available: current.available
        )
    }

    func page(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        try validateContentSHA256(contentSHA256)
        guard page >= 1 else { return nil }
        let selectedLayer = try layerState(
            contentSHA256: contentSHA256
        ).selected
        let manual = try manualPage(contentSHA256: contentSHA256, page: page)
        let corrections = try selectionCorrections(
            contentSHA256: contentSHA256,
            page: page
        )
        let base: NativeBookOCRPageCharacters?
        if selectedLayer == .embedded, corrections.isEmpty {
            base = nil
        } else {
            base = try basePage(
                contentSHA256: contentSHA256,
                page: page,
                layer: selectedLayer
            ) ?? (corrections.isEmpty ? nil : try basePage(
                contentSHA256: contentSHA256,
                page: page,
                layer: .embedded
            ))
        }
        guard let selected = manual ?? base else {
            if !corrections.isEmpty {
                throw NativeBookOCRError.storage("选区校正缺少基础文字页")
            }
            return nil
        }
        let effective = Self.applyingSelectionCorrections(corrections, to: selected)
        if manual == nil, corrections.isEmpty,
           [.legacy, .appleVision, .pi, .pc].contains(selectedLayer) {
            // Existing Reader runtimes already treat local-override as the
            // explicit instruction to prefer native text over PDF.js. This is
            // an ephemeral authority marker; the stored base remains reusable.
            return effective.replacingTextAuthority(.localOverride)
        }
        return effective
    }

    func basePage(
        contentSHA256: String,
        page: Int,
        layer: NativeBookOCRLayerID? = nil
    ) throws -> NativeBookOCRPageCharacters? {
        try validateContentSHA256(contentSHA256)
        let resolvedLayer: NativeBookOCRLayerID
        if let layer {
            resolvedLayer = layer
        } else {
            resolvedLayer = try layerState(
                contentSHA256: contentSHA256
            ).selected
        }
        let url = pageURL(
            contentSHA256: contentSHA256,
            page: page,
            beneath: layerDirectory(
                contentSHA256: contentSHA256,
                layer: resolvedLayer
            )
        )
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try decoder().decode(
                NativeBookOCRPageCharacters.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRPageCharacters.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page else {
                throw NativeBookOCRError.storage("页 sidecar 身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func writeManualPageOverride(
        _ value: NativeBookOCRPageCharacters,
        bookID: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: value.contentSHA256
        )
        try validateContentSHA256(value.contentSHA256)
        guard value.schema == NativeBookOCRPageCharacters.schema,
              value.page >= 1,
              value.pageWidth > 0,
              value.pageHeight > 0,
              value.textAuthority == .localOverride else {
            throw NativeBookOCRError.storage("单页手动覆盖结构无效")
        }
        let url = manualPageURL(
            contentSHA256: value.contentSHA256,
            page: value.page
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(value).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func clearManualPageOverride(
        bookID: String,
        contentSHA256: String,
        page: Int
    ) throws -> Bool {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: contentSHA256
        )
        try validateContentSHA256(contentSHA256)
        guard page >= 1 else { return false }
        let url = manualPageURL(contentSHA256: contentSHA256, page: page)
        guard fileManager.fileExists(atPath: url.path) else { return false }
        do {
            try fileManager.removeItem(at: url)
            return true
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func appendSelectionCorrection(
        _ correction: NativeBookOCRSelectionCorrection,
        bookID: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: correction.contentSHA256
        )
        try validateContentSHA256(correction.contentSHA256)
        guard correction.schema == NativeBookOCRSelectionCorrection.schema,
              correction.page >= 1,
              correction.id.range(
                of: #"^ocrfix-[0-9a-f]{32}$"#,
                options: .regularExpression
              ) != nil,
              correction.bbox.count == 4,
              correction.bbox.allSatisfy({ $0.isFinite && $0 >= 0 }),
              correction.bbox[2] > correction.bbox[0],
              correction.bbox[3] > correction.bbox[1],
              !correction.text.trimmingCharacters(
                in: .whitespacesAndNewlines
              ).isEmpty,
              !correction.chars.isEmpty else {
            throw NativeBookOCRError.storage("选区校正结构无效")
        }
        var corrections = try selectionCorrections(
            contentSHA256: correction.contentSHA256,
            page: correction.page
        )
        guard corrections.count < 256,
              !corrections.contains(where: { $0.id == correction.id }) else {
            throw NativeBookOCRError.storage("选区校正数量或身份无效")
        }
        corrections.append(correction)
        let envelope = NativeBookOCRSelectionCorrectionEnvelope(
            schema: NativeBookOCRSelectionCorrectionEnvelope.schema,
            contentSHA256: correction.contentSHA256.lowercased(),
            page: correction.page,
            corrections: corrections
        )
        let url = selectionCorrectionURL(
            contentSHA256: correction.contentSHA256,
            page: correction.page
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(envelope).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func manualPage(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        let url = manualPageURL(contentSHA256: contentSHA256, page: page)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try decoder().decode(
                NativeBookOCRPageCharacters.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRPageCharacters.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page,
                  value.textAuthority == .localOverride else {
                throw NativeBookOCRError.storage("单页手动覆盖身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func selectionCorrections(
        contentSHA256: String,
        page: Int
    ) throws -> [NativeBookOCRSelectionCorrection] {
        let url = selectionCorrectionURL(
            contentSHA256: contentSHA256,
            page: page
        )
        guard fileManager.fileExists(atPath: url.path) else { return [] }
        do {
            let value = try decoder().decode(
                NativeBookOCRSelectionCorrectionEnvelope.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRSelectionCorrectionEnvelope.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page,
                  value.corrections.count <= 256,
                  value.corrections.allSatisfy({ correction in
                    correction.schema == NativeBookOCRSelectionCorrection.schema
                        && correction.contentSHA256 == contentSHA256.lowercased()
                        && correction.page == page
                        && correction.bbox.count == 4
                        && !correction.chars.isEmpty
                  }) else {
                throw NativeBookOCRError.storage("选区校正身份不匹配")
            }
            return value.corrections
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private static func applyingSelectionCorrections(
        _ corrections: [NativeBookOCRSelectionCorrection],
        to initial: NativeBookOCRPageCharacters
    ) -> NativeBookOCRPageCharacters {
        guard !corrections.isEmpty else { return initial }
        var chars = initial.chars
        var furigana = initial.furigana
        var formulaRegions = initial.formulaRegions
        var latest = initial.createdAt
        var revisionParts = [initial.engineRevision]
        for correction in corrections.sorted(by: { $0.createdAt < $1.createdAt }) {
            let box = correction.bbox
            func contains(_ x0: Double, _ y0: Double, _ x1: Double, _ y1: Double) -> Bool {
                let x = (x0 + x1) / 2
                let y = (y0 + y1) / 2
                return x >= box[0] && x <= box[2]
                    && y >= box[1] && y <= box[3]
            }
            let firstRemoved = chars.firstIndex(where: {
                contains($0.x0, $0.y0, $0.x1, $0.y1)
            }) ?? chars.count
            chars.removeAll(where: {
                contains($0.x0, $0.y0, $0.x1, $0.y1)
            })
            chars.insert(
                contentsOf: correction.chars,
                at: min(firstRemoved, chars.count)
            )
            furigana.removeAll(where: { item in
                guard let x0 = item.x0, let y0 = item.y0,
                      let x1 = item.x1, let y1 = item.y1 else { return false }
                return contains(x0, y0, x1, y1)
            })
            formulaRegions.removeAll(where: { region in
                !(region.x1 < box[0] || region.x0 > box[2]
                    || region.y1 < box[1] || region.y0 > box[3])
            })
            latest = max(latest, correction.createdAt)
            revisionParts.append("selection:\(correction.id)")
        }
        let meaningful = chars.filter { $0.sp == 0 }
        return NativeBookOCRPageCharacters(
            schema: initial.schema,
            contentSHA256: initial.contentSHA256,
            page: initial.page,
            pageWidth: initial.pageWidth,
            pageHeight: initial.pageHeight,
            rotation: initial.rotation,
            geometryDigest: initial.geometryDigest,
            engineRevision: revisionParts.joined(separator: "+"),
            status: meaningful.isEmpty ? .readyEmpty : .ready,
            source: initial.source,
            chars: chars,
            furigana: furigana,
            wordSegmentation: initial.wordSegmentation,
            characterGeometry: initial.characterGeometry,
            formulaCoverage: formulaRegions.isEmpty
                ? .unavailable : initial.formulaCoverage,
            formulaRegions: formulaRegions,
            createdAt: latest,
            error: nil,
            textAuthority: .localOverride
        )
    }

    func pages(
        contentSHA256: String,
        layer: NativeBookOCRLayerID? = nil
    ) throws -> [NativeBookOCRPageCharacters] {
        try validateContentSHA256(contentSHA256)
        let resolvedLayer: NativeBookOCRLayerID
        if let layer {
            resolvedLayer = layer
        } else {
            resolvedLayer = try layerState(
                contentSHA256: contentSHA256
            ).selected
        }
        let directory = layerDirectory(
            contentSHA256: contentSHA256,
            layer: resolvedLayer
        )
            .appendingPathComponent("pages", isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return [] }
        do {
            return try fileManager.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles]
            ).filter { url in
                guard url.pathExtension == "json" else { return false }
                let values = try? url.resourceValues(
                    forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
                )
                return values?.isRegularFile == true
                    && values?.isSymbolicLink != true
            }.compactMap { url in
                guard let value = try? decoder().decode(
                    NativeBookOCRPageCharacters.self,
                    from: Data(contentsOf: url)
                ), value.schema == NativeBookOCRPageCharacters.schema,
                   value.contentSHA256 == contentSHA256.lowercased(),
                   value.page >= 1 else {
                    return nil
                }
                return value
            }.sorted { $0.page < $1.page }
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func effectivePages(
        contentSHA256: String
    ) throws -> [NativeBookOCRPageCharacters] {
        try pages(contentSHA256: contentSHA256, layer: nil).map { base in
            try page(contentSHA256: contentSHA256, page: base.page) ?? base
        }
    }

    func writePage(
        _ value: NativeBookOCRPageCharacters,
        bookID: String,
        layer: NativeBookOCRLayerID = .appleVision
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: value.contentSHA256
        )
        try validateContentSHA256(value.contentSHA256)
        guard value.schema == NativeBookOCRPageCharacters.schema,
              value.page >= 1,
              value.pageWidth > 0,
              value.pageHeight > 0,
              layer == .appleVision || layer == .embedded else {
            throw NativeBookOCRError.storage("页 sidecar 结构无效")
        }
        let contentDirectory = layerDirectory(
            contentSHA256: value.contentSHA256,
            layer: layer
        )
        let url = pageURL(
            contentSHA256: value.contentSHA256,
            page: value.page,
            beneath: contentDirectory
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(value).write(to: url, options: .atomic)
            if layer == .appleVision {
                let storedCount = try pages(
                    contentSHA256: value.contentSHA256,
                    layer: layer
                ).count
                try writeLayerMetadata(
                    NativeBookOCRLayerMetadata(
                        schema: NativeBookOCRLayerMetadata.schema,
                        contentSHA256: value.contentSHA256.lowercased(),
                        layer: layer,
                        engine: "vision",
                        executor: "device",
                        processingProfile: NativeBookOCRConfiguration.engineRevision,
                        revision: NativeBookOCRConfiguration.engineRevision,
                        pageCount: storedCount,
                        updatedAt: Date()
                    ),
                    beneath: contentDirectory
                )
            }
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func writeStatus(
        _ status: NativeBookOCRBookStatus,
        mutationLease: NativeBookOCRPDFMutationLease? = nil
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: status.bookID,
            contentSHA256: status.contentSHA256.isEmpty
                ? nil : status.contentSHA256,
            mutationLease: mutationLease
        )
        guard status.schema == NativeBookOCRBookStatus.schema,
              !status.bookID.isEmpty,
              status.contentSHA256.isEmpty
                || Self.isSHA256(status.contentSHA256) else {
            throw NativeBookOCRError.storage("任务状态无效")
        }
        let url = statusURL(bookID: status.bookID)
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(status).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func loadStatuses() throws -> [NativeBookOCRBookStatus] {
        let directory = rootURL.appendingPathComponent("jobs", isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return [] }
        do {
            return try fileManager.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles]
            ).compactMap { url in
                let values = try? url.resourceValues(
                    forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
                )
                guard url.pathExtension == "json",
                      values?.isRegularFile == true,
                      values?.isSymbolicLink != true,
                      let status = try? decoder().decode(
                        NativeBookOCRBookStatus.self,
                        from: Data(contentsOf: url)
                      ), status.schema == NativeBookOCRBookStatus.schema,
                      !status.bookID.isEmpty,
                      status.contentSHA256.isEmpty
                        || Self.isSHA256(status.contentSHA256) else {
                    return nil
                }
                return status.state == .running
                    ? NativeBookOCRBookStatus(
                        schema: status.schema,
                        bookID: status.bookID,
                        contentSHA256: status.contentSHA256,
                        state: .paused,
                        source: status.source,
                        totalPages: status.totalPages,
                        currentPage: nil,
                        textProgress: status.textProgress,
                        wordProgress: status.wordProgress,
                        formulaProgress: status.formulaProgress,
                        formulaPendingRegions: status.formulaPendingRegions,
                        formulaFailedRegions: status.formulaFailedRegions,
                        message: "App 上次退出时已保存进度，可继续处理",
                        updatedAt: Date()
                      )
                    : status
            }
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    /// A content-addressed receipt survives view/model recreation and lets the
    /// network coordinator skip downloading the same immutable Pi/PC revision.
    /// Version 2 also records every page's character count. A legacy receipt or
    /// a payload mismatch is a repairable cache miss: the verified immutable
    /// attachment is downloaded again instead of trusting a stale empty layer.
    func hasImportedRevision(
        contentSHA256: String,
        revision: String
    ) throws -> Bool {
        try validateContentSHA256(contentSHA256)
        try validateRevision(revision)
        let url = importReceiptURL(
            revision: revision,
            beneath: contentDirectory(contentSHA256)
        )
        guard fileManager.fileExists(atPath: url.path) else { return false }
        do {
            let values = try url.resourceValues(forKeys: [
                .isRegularFileKey, .isSymbolicLinkKey,
            ])
            guard values.isRegularFile == true,
                  values.isSymbolicLink != true else {
                throw NativeBookOCRError.storage("导入回执文件无效")
            }
            let receipt = try decoder().decode(
                ImportedRevisionReceipt.self,
                from: Data(contentsOf: url)
            )
            guard [
                ImportedRevisionReceipt.legacySchema,
                ImportedRevisionReceipt.schema,
            ].contains(receipt.schema),
                  receipt.contentSHA256 == contentSHA256.lowercased(),
                  receipt.revision == revision else {
                throw NativeBookOCRError.storage("导入回执身份不匹配")
            }
            if let layer = receipt.layer {
                guard let metadata = try storedLayerMetadata(
                    contentSHA256: contentSHA256,
                    layer: layer
                ), metadata.revision == revision else {
                    return false
                }
                if receipt.schema == ImportedRevisionReceipt.legacySchema {
                    // PC layers shipped before count-bearing receipts are
                    // re-imported once. Existing Pi layers keep their legacy
                    // cache behavior and are not all redownloaded on upgrade.
                    return layer != .pc
                }
                guard let expectedCounts = receipt.pageCharacterCounts,
                      !expectedCounts.isEmpty,
                      expectedCounts.keys.allSatisfy({ $0 >= 1 }),
                      expectedCounts.values.allSatisfy({ $0 >= 0 }) else {
                    return false
                }
                let orderedPages = expectedCounts.keys.sorted()
                var probePages = Set([
                    orderedPages.first!,
                    orderedPages[orderedPages.count / 2],
                    orderedPages.last!,
                ])
                if let densestPage = expectedCounts.max(by: {
                    $0.value < $1.value
                })?.key {
                    probePages.insert(densestPage)
                }
                for pageNumber in probePages {
                    guard let storedPage = try basePage(
                        contentSHA256: contentSHA256,
                        page: pageNumber,
                        layer: layer
                    ), storedPage.chars.count == expectedCounts[pageNumber] else {
                        return false
                    }
                }
            }
            return true
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    /// Imports a complete immutable Pi attachment revision. All bytes and all
    /// payload identities are validated before a staged content directory is
    /// exchanged, so a failed download cannot partially replace local OCR.
    /// The network layer supplies bytes by opaque attachmentId; downloadUrl is
    /// validated metadata and is never opened by this store.
    func importDerivedAttachments(
        bookID: String,
        expectedContentSHA256: String,
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) throws -> NativeBookOCRImportResult {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: expectedContentSHA256
        )
        try validateContentSHA256(expectedContentSHA256)
        guard expectedContentSHA256.caseInsensitiveCompare(
            manifest.contentSha256
        ) == .orderedSame else {
            throw NativeBookOCRError.invalidAttachment("本机书籍摘要与附件不匹配")
        }
        try validate(manifest: manifest, files: files)
        let contentSHA256 = manifest.contentSha256.lowercased()
        let executor = (manifest.executor ?? "pi").lowercased()
        guard ["pi", "pc"].contains(executor) else {
            throw NativeBookOCRError.invalidAttachment("预处理执行器无效")
        }
        guard executor != "pc"
            || ["quality-first-v1", "quality-first-v2", "quality-first-v3", "quality-first-v4"].contains(
                manifest.processingProfile
            ) else {
            throw NativeBookOCRError.invalidAttachment("PC 预处理配置无效")
        }
        let processingProfile = manifest.processingProfile ?? "pi-default-v1"
        let targetLayer: NativeBookOCRLayerID = executor == "pc" ? .pc : .pi
        let targetSource: NativeBookOCRSource = executor == "pc" ? .pc : .pi
        let target = contentDirectory(contentSHA256)
        var importedPages: [Int] = []
        var importedFormulaPages: [Int] = []
        var incomingPages: [Int: NativeBookOCRPageCharacters] = [:]
        var formulaEnvelope: PiFormulaEnvelope?

        for entry in manifest.files {
            guard let data = files[entry.attachmentId] else {
                throw NativeBookOCRError.invalidAttachment(
                    "缺少附件 \(entry.attachmentId)"
                )
            }
            switch entry.kind {
            case "ocr-page-chars":
                let value = try decoder().decode(PiPageCharacters.self, from: data)
                let expectedAttachmentID = String(
                    format: "ocr-page-%06d",
                    value.pageNumber
                )
                guard entry.attachmentId == expectedAttachmentID,
                      entry.page.map({ $0 == value.pageNumber }) ?? true else {
                    throw NativeBookOCRError.invalidAttachment(
                        "文字页附件页码不匹配"
                    )
                }
                let converted = try convertPiPage(
                    value,
                    expectedContentSHA256: contentSHA256,
                    expectedBookID: manifest.bookId,
                    executor: executor,
                    processingProfile: processingProfile
                )
                guard incomingPages.updateValue(
                    converted,
                    forKey: converted.page
                ) == nil else {
                    throw NativeBookOCRError.invalidAttachment("页附件重复")
                }
                importedPages.append(converted.page)
            case "ocr-formula-regions":
                guard formulaEnvelope == nil else {
                    throw NativeBookOCRError.invalidAttachment("公式附件重复")
                }
                formulaEnvelope = try decoder().decode(
                    PiFormulaEnvelope.self,
                    from: data
                )
            default:
                throw NativeBookOCRError.invalidAttachment(
                    "不支持的附件类型 \(entry.kind)"
                )
            }
        }

        var layerPages = incomingPages
        if let formulaEnvelope {
            guard formulaEnvelope.schema == PiFormulaEnvelope.schema,
                  formulaEnvelope.bookId == manifest.bookId,
                  formulaEnvelope.contentSha256 == contentSHA256 else {
                throw NativeBookOCRError.invalidAttachment("公式附件身份不匹配")
            }
            let grouped = Dictionary(grouping: formulaEnvelope.formulas, by: \.page)
            for (pageNumber, formulas) in grouped {
                guard let page = layerPages[pageNumber] else {
                    throw NativeBookOCRError.invalidAttachment(
                        "公式页 \(pageNumber) 没有对应文字页几何"
                    )
                }
                let regions = try formulas.enumerated().map { offset, formula in
                    try convertPiFormula(
                        formula,
                        offset: offset,
                        page: page
                    )
                }
                let attachment = NativeBookOCRFormulaAttachment(
                    schema: NativeBookOCRFormulaAttachment.schema,
                    contentSHA256: contentSHA256,
                    page: pageNumber,
                    geometryDigest: page.geometryDigest,
                    engineRevision: "pi-formula/1",
                    formulaCoverage: .complete,
                    formulaRegions: regions,
                    createdAt: Date()
                )
                layerPages[pageNumber] = page.replacingFormulaAttachment(
                    attachment,
                    source: targetSource
                )
                importedFormulaPages.append(pageNumber)
            }
            // A valid formula export is the complete result for this immutable
            // revision. Imported text pages absent from its region list were
            // examined and contain no detected formulas.
            for pageNumber in incomingPages.keys where grouped[pageNumber] == nil {
                guard let page = layerPages[pageNumber] else { continue }
                let attachment = NativeBookOCRFormulaAttachment(
                    schema: NativeBookOCRFormulaAttachment.schema,
                    contentSHA256: contentSHA256,
                    page: pageNumber,
                    geometryDigest: page.geometryDigest,
                    engineRevision: "pi-formula/1",
                    formulaCoverage: .complete,
                    formulaRegions: [],
                    createdAt: Date()
                )
                layerPages[pageNumber] = page.replacingFormulaAttachment(
                    attachment,
                    source: targetSource
                )
                importedFormulaPages.append(pageNumber)
            }
        }

        let stagingRoot = rootURL.appendingPathComponent(
            ".import-\(UUID().uuidString)",
            isDirectory: true
        )
        do {
            try fileManager.createDirectory(
                at: rootURL,
                withIntermediateDirectories: true
            )
            if fileManager.fileExists(atPath: target.path) {
                try fileManager.copyItem(at: target, to: stagingRoot)
            } else {
                try fileManager.createDirectory(
                    at: stagingRoot,
                    withIntermediateDirectories: true
                )
            }
            let stagingLayer = layerDirectory(
                contentSHA256: contentSHA256,
                layer: targetLayer,
                beneath: stagingRoot
            )
            let stagingPages = stagingLayer.appendingPathComponent(
                "pages",
                isDirectory: true
            )
            if fileManager.fileExists(atPath: stagingPages.path) {
                try fileManager.removeItem(at: stagingPages)
            }
            try fileManager.createDirectory(
                at: stagingPages,
                withIntermediateDirectories: true
            )
            for value in layerPages.values {
                let url = pageURL(
                    contentSHA256: contentSHA256,
                    page: value.page,
                    beneath: stagingLayer
                )
                try fileManager.createDirectory(
                    at: url.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try encoder().encode(value).write(to: url, options: .atomic)
            }
            let importedEngine = manifest.engine
                ?? Set(layerPages.values.map(\.engineRevision)).sorted().joined(
                    separator: "+"
                )
            try writeLayerMetadata(
                NativeBookOCRLayerMetadata(
                    schema: NativeBookOCRLayerMetadata.schema,
                    contentSHA256: contentSHA256,
                    layer: targetLayer,
                    engine: importedEngine,
                    executor: executor,
                    processingProfile: processingProfile,
                    revision: manifest.revision,
                    pageCount: layerPages.count,
                    updatedAt: Date()
                ),
                beneath: stagingLayer
            )
            let receipt = ImportedRevisionReceipt(
                schema: ImportedRevisionReceipt.schema,
                contentSHA256: contentSHA256,
                revision: manifest.revision,
                layer: targetLayer,
                pageCharacterCounts: Dictionary(
                    uniqueKeysWithValues: layerPages.values.map {
                        ($0.page, $0.chars.count)
                    }
                ),
                importedAt: Date()
            )
            let receiptURL = importReceiptURL(
                revision: manifest.revision,
                beneath: stagingRoot
            )
            try fileManager.createDirectory(
                at: receiptURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(receipt).write(to: receiptURL, options: .atomic)
            if fileManager.fileExists(atPath: target.path) {
                _ = try fileManager.replaceItemAt(
                    target,
                    withItemAt: stagingRoot,
                    backupItemName: nil,
                    options: []
                )
            } else {
                try fileManager.createDirectory(
                    at: target.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try fileManager.moveItem(at: stagingRoot, to: target)
            }
        } catch {
            try? fileManager.removeItem(at: stagingRoot)
            if let nativeError = error as? NativeBookOCRError {
                throw nativeError
            }
            throw NativeBookOCRError.invalidAttachment(error.localizedDescription)
        }
        return NativeBookOCRImportResult(
            contentSHA256: contentSHA256,
            layer: targetLayer,
            importedPages: importedPages.sorted(),
            importedFormulaPages: Array(Set(importedFormulaPages)).sorted()
        )
    }

    private struct ImportedRevisionReceipt: Codable {
        static let legacySchema = "reader-native-book-ocr-import-receipt/1"
        static let schema = "reader-native-book-ocr-import-receipt/2"

        let schema: String
        let contentSHA256: String
        let revision: String
        let layer: NativeBookOCRLayerID?
        let pageCharacterCounts: [Int: Int]?
        let importedAt: Date
    }

    private struct PiPageCharacters: Decodable {
        static let schema = "reader-page-chars/1"

        let schema: String
        let bookId: String
        let contentSha256: String
        let engine: String
        let pageNumber: Int
        let pageWidth: Double
        let pageHeight: Double
        let imageWidth: Int
        let imageHeight: Int
        let chars: [NativeBookOCRCharacter]
        let furigana: [NativeBookOCRFurigana]
        let tokenized: Bool?
        let textCharCount: Int?
        let generatedAtEpochMs: Int64

        enum CodingKeys: String, CodingKey {
            case schema, bookId, contentSha256, engine, pageNumber
            case pageWidth = "page_w"
            case pageHeight = "page_h"
            case imageWidth, imageHeight, chars, furigana, tokenized, textCharCount
            case generatedAtEpochMs
        }
    }

    private struct PiFormulaEnvelope: Decodable {
        static let schema = "reader-formula-regions/1"

        let schema: String
        let bookId: String
        let contentSha256: String
        let formulas: [Formula]

        struct Formula: Decodable {
            let page: Int
            let bbox: [Double]
            let latex: String?
            let multiline: Bool?
        }
    }

    private func convertPiPage(
        _ value: PiPageCharacters,
        expectedContentSHA256: String,
        expectedBookID: String,
        executor: String,
        processingProfile: String
    ) throws -> NativeBookOCRPageCharacters {
        guard value.schema == PiPageCharacters.schema,
              value.bookId == expectedBookID,
              value.contentSha256 == expectedContentSHA256,
              value.pageNumber >= 1,
              value.pageWidth.isFinite, value.pageHeight.isFinite,
              value.pageWidth > 0, value.pageHeight > 0,
              value.imageWidth > 0, value.imageHeight > 0,
              ["vision", "manga"].contains(value.engine) else {
            throw NativeBookOCRError.invalidAttachment("文字页附件身份或几何无效")
        }
        // ⚠ textCharCount 与 chars.count **不是**同一个量，不能相等比。
        //
        //   worker 那边它是正文字符数：`len("".join(text.split()))` —— 拿 Vision
        //   返回的整页文本去掉**全部** Unicode 空白后的字数。而 chars 是 symbol
        //   列表，另外含 sp 条目（只标记 Vision 报告的 detectedBreak）。两个口径
        //   出自不同的数据源，永远不可能逐字对齐：全角空格这类既被 split() 去掉、
        //   又不产生 detectedBreak 的字符就会让它们差几个。
        //
        //   2026-08-19 实测这条一直在拒：三份结果（更早代码产的 57/60、Pi 产的
        //   60/60、当天新跑的 53/53）几乎每页都不等 —— 也就是说**导入从来没成功过**，
        //   报的是「文字页字符层无效」。按 sp 重数之后仍有 1/53 差 1，
        //   足以说明"调口径"是条走不通的路。
        //
        //   换成一个**恒成立**的不变量：正文字数不可能超过条目总数。反过来才是
        //   真的数据损坏，而那正是这条校验本来想拦的东西。
        guard value.chars.count <= 250_000,
              value.textCharCount.map({ $0 >= 0 && $0 <= value.chars.count }) ?? true,
              value.chars.allSatisfy({ character in
                !character.c.isEmpty
                    && character.c.count <= 16
                    && character.x0.isFinite && character.y0.isFinite
                    && character.x1.isFinite && character.y1.isFinite
                    && character.x0 <= character.x1
                    && character.y0 <= character.y1
                    && character.x0 >= -1 && character.y0 >= -1
                    && character.x1 <= value.pageWidth + 1
                    && character.y1 <= value.pageHeight + 1
              }) else {
            throw NativeBookOCRError.invalidAttachment("文字页字符层无效")
        }
        let geometry = NativeBookOCRPageGeometry(
            page: value.pageNumber,
            cropBoxX: 0,
            cropBoxY: 0,
            pageWidth: value.pageWidth,
            pageHeight: value.pageHeight,
            rotation: 0,
            renderPixelWidth: value.imageWidth,
            renderPixelHeight: value.imageHeight
        )
        let assigned = value.chars.filter { $0.sp == 0 && $0.w >= 0 }.count
        let meaningful = value.chars.filter { $0.sp == 0 }.count
        let segmentation: NativeBookOCRWordSegmentationState
        if meaningful == 0 || assigned == 0 {
            segmentation = .unavailable
        } else if assigned < meaningful {
            segmentation = .partial
        } else {
            segmentation = .ready
        }
        let geometryVersion: Int
        if value.engine == "manga"
            && ["pi-default-v3", "quality-first-v4"].contains(processingProfile) {
            geometryVersion = 3
        } else if value.engine == "manga"
            && ["pi-default-v2", "quality-first-v3"].contains(processingProfile) {
            geometryVersion = 2
        } else {
            geometryVersion = 1
        }
        let revision = "\(executor)-\(value.engine)/\(geometryVersion)"
        return NativeBookOCRPageCharacters(
            schema: NativeBookOCRPageCharacters.schema,
            contentSHA256: expectedContentSHA256,
            page: value.pageNumber,
            pageWidth: value.pageWidth,
            pageHeight: value.pageHeight,
            rotation: 0,
            geometryDigest: NativeBookOCRProcessor.geometryDigest(
                contentSHA256: expectedContentSHA256,
                geometry: geometry,
                engineRevision: revision
            ),
            engineRevision: revision,
            status: meaningful == 0 ? .readyEmpty : .ready,
            source: executor == "pc" ? .pc : .pi,
            chars: value.chars,
            furigana: value.furigana,
            wordSegmentation: segmentation,
            // Manga geometry aligns observed ink runs inside each recognized
            // line, but it is still not symbol-exact and deliberately falls
            // back to line division when optical alignment is unavailable.
            characterGeometry: value.engine == "vision" ? .exact : .estimated,
            formulaCoverage: .unknown,
            formulaRegions: [],
            createdAt: Date(
                timeIntervalSince1970: Double(value.generatedAtEpochMs) / 1_000
            ),
            error: nil
        )
    }

    private func convertPiFormula(
        _ value: PiFormulaEnvelope.Formula,
        offset: Int,
        page: NativeBookOCRPageCharacters
    ) throws -> NativeBookOCRFormulaRegion {
        guard value.page == page.page,
              value.bbox.count == 4,
              value.bbox.allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 1 }),
              value.bbox[0] < value.bbox[2],
              value.bbox[1] < value.bbox[3],
              (value.latex?.count ?? 0) <= 20_000 else {
            throw NativeBookOCRError.invalidAttachment("公式区域无效")
        }
        let latex = value.latex?.trimmingCharacters(in: .whitespacesAndNewlines)
        return NativeBookOCRFormulaRegion(
            id: "pi-formula-p\(page.page)-\(offset + 1)",
            x0: value.bbox[0] * page.pageWidth,
            y0: value.bbox[1] * page.pageHeight,
            x1: value.bbox[2] * page.pageWidth,
            y1: value.bbox[3] * page.pageHeight,
            state: latex?.isEmpty == false ? .ready : .failed,
            latex: latex?.isEmpty == false ? latex : nil,
            multiline: value.multiline,
            error: latex?.isEmpty == false ? nil : "Pi 未能识别该公式"
        )
    }

    private func validate(
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) throws {
        try validateContentSHA256(manifest.contentSha256)
        guard manifest.contract == NativeBookOCRDerivedAttachmentManifest.contract,
              manifest.schema == 1,
              !manifest.bookId.isEmpty,
              manifest.category == "derived",
              manifest.mergePolicy == "immutable",
              manifest.revision.range(
                of: #"^ocr_[0-9a-f]{20}$"#,
                options: .regularExpression
              ) != nil,
              manifest.executor.map({ ["pi", "pc"].contains($0) }) ?? true,
              manifest.engine.map({ ["vision", "manga", "legacy"].contains($0) })
                ?? true,
              manifest.processingProfile.map({ !$0.isEmpty && $0.count <= 80 })
                ?? true,
              !manifest.files.isEmpty,
              manifest.files.count <= Self.maximumAttachments,
              files.count == manifest.files.count else {
            throw NativeBookOCRError.invalidAttachment("附件清单无效")
        }
        var seen = Set<String>()
        var aggregateBytes: Int64 = 0
        for entry in manifest.files {
            guard seen.insert(entry.attachmentId).inserted,
                  entry.category == "derived",
                  entry.mergePolicy == "immutable",
                  entry.mediaType == "application/json",
                  entry.size >= 0,
                  entry.size <= Self.maximumAttachmentBytes,
                  Self.isSHA256(entry.sha256),
                  let data = files[entry.attachmentId],
                  Int64(data.count) == entry.size,
                  Self.sha256(data) == entry.sha256.lowercased(),
                  ["ocr-page-chars", "ocr-formula-regions"].contains(entry.kind),
                  entry.page.map { (1...100_000).contains($0) } ?? true,
                  Self.isValidAttachmentID(entry.attachmentId),
                  entry.downloadUrl == Self.expectedDownloadURL(
                    manifest: manifest,
                    attachmentID: entry.attachmentId
                  ) else {
                throw NativeBookOCRError.invalidAttachment(
                    "附件 \(entry.attachmentId) 校验失败"
                )
            }
            let (nextAggregate, overflow) = aggregateBytes.addingReportingOverflow(
                entry.size
            )
            guard !overflow, nextAggregate <= Self.maximumBundleBytes else {
                throw NativeBookOCRError.invalidAttachment("附件总大小超出限制")
            }
            aggregateBytes = nextAggregate
        }
    }

    private func storedLayerMetadata(
        contentSHA256: String,
        layer: NativeBookOCRLayerID
    ) throws -> NativeBookOCRLayerMetadata? {
        guard [.appleVision, .pi, .pc].contains(layer) else { return nil }
        let url = layerDirectory(
            contentSHA256: contentSHA256,
            layer: layer
        ).appendingPathComponent("metadata.json", isDirectory: false)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let values = try url.resourceValues(forKeys: [
                .isRegularFileKey, .isSymbolicLinkKey,
            ])
            guard values.isRegularFile == true,
                  values.isSymbolicLink != true else {
                throw NativeBookOCRError.storage("文字层元数据文件无效")
            }
            let value = try decoder().decode(
                NativeBookOCRLayerMetadata.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRLayerMetadata.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.layer == layer,
                  value.pageCount > 0,
                  !value.engine.isEmpty,
                  !value.revision.isEmpty else {
                throw NativeBookOCRError.storage("文字层元数据身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func writeLayerMetadata(
        _ value: NativeBookOCRLayerMetadata,
        beneath layerDirectory: URL
    ) throws {
        guard value.schema == NativeBookOCRLayerMetadata.schema,
              value.pageCount > 0,
              [.appleVision, .pi, .pc].contains(value.layer) else {
            throw NativeBookOCRError.storage("文字层元数据无效")
        }
        let url = layerDirectory.appendingPathComponent(
            "metadata.json",
            isDirectory: false
        )
        try fileManager.createDirectory(
            at: layerDirectory,
            withIntermediateDirectories: true
        )
        try encoder().encode(value).write(to: url, options: .atomic)
    }

    private func loadLayerSelection(
        contentSHA256: String
    ) throws -> NativeBookOCRLayerSelection? {
        let url = layerSelectionURL(contentSHA256)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try decoder().decode(
                NativeBookOCRLayerSelection.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRLayerSelection.schema,
                  value.contentSHA256 == contentSHA256.lowercased() else {
                throw NativeBookOCRError.storage("文字层选择身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func contentDirectory(_ contentSHA256: String) -> URL {
        rootURL.appendingPathComponent("content", isDirectory: true)
            .appendingPathComponent(contentSHA256.lowercased(), isDirectory: true)
    }

    private func layerDirectory(
        contentSHA256: String,
        layer: NativeBookOCRLayerID,
        beneath directory: URL? = nil
    ) -> URL {
        let base = directory ?? contentDirectory(contentSHA256)
        if layer == .legacy { return base }
        return base.appendingPathComponent("layers", isDirectory: true)
            .appendingPathComponent(layer.rawValue, isDirectory: true)
    }

    private func pageURL(
        contentSHA256: String,
        page: Int,
        beneath directory: URL
    ) -> URL {
        directory.appendingPathComponent("pages", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func manualPageURL(
        contentSHA256: String,
        page: Int
    ) -> URL {
        contentDirectory(contentSHA256)
            .appendingPathComponent("overrides/manual", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func selectionCorrectionURL(
        contentSHA256: String,
        page: Int
    ) -> URL {
        contentDirectory(contentSHA256)
            .appendingPathComponent("overrides/selection", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func statusURL(bookID: String) -> URL {
        let digest = Self.sha256(Data(bookID.utf8))
        return rootURL.appendingPathComponent("jobs", isDirectory: true)
            .appendingPathComponent("\(digest).json", isDirectory: false)
    }

    private func layerSelectionURL(_ contentSHA256: String) -> URL {
        contentDirectory(contentSHA256).appendingPathComponent(
            "active-layer.json",
            isDirectory: false
        )
    }

    private func importReceiptURL(
        revision: String,
        beneath directory: URL
    ) -> URL {
        directory.appendingPathComponent("imports", isDirectory: true)
            .appendingPathComponent("\(revision).json", isDirectory: false)
    }

    private func validateContentSHA256(_ value: String) throws {
        guard Self.isSHA256(value) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
    }

    private func validateRevision(_ value: String) throws {
        guard value.range(
            of: #"^ocr_[0-9a-f]{20}$"#,
            options: .regularExpression
        ) != nil else {
            throw NativeBookOCRError.invalidAttachment("附件修订号无效")
        }
    }

    private func validatePDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        guard lease.bookID.range(
            of: #"^localbook-[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil,
        lease.token.range(
            of: #"^[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil,
        Self.isSHA256(lease.oldContentSHA256) else {
            throw NativeBookOCRError.storage("PDF 改页 OCR 租约身份无效")
        }
    }

    private func registerPDFMutationDigestLease(
        _ contentSHA256: String,
        lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validateContentSHA256(contentSHA256)
        let digest = contentSHA256.lowercased()
        var leases = pdfMutationLeasesByDigest[digest] ?? [:]
        if let active = leases[lease.bookID], active != lease {
            throw NativeBookOCRError.storage(
                "本书的 PDF 摘要已有另一项 OCR 改页租约"
            )
        }
        leases[lease.bookID] = lease
        pdfMutationLeasesByDigest[digest] = leases
    }

    private func assertPDFMutationWriteAllowed(
        bookID: String,
        contentSHA256: String? = nil,
        mutationLease: NativeBookOCRPDFMutationLease? = nil
    ) throws {
        if let mutationLease {
            try validatePDFMutationLease(mutationLease)
            guard mutationLease.bookID == bookID,
                  pdfMutationLeases[bookID] == mutationLease else {
                throw NativeBookOCRError.storage(
                    "PDF 改页 OCR 租约已失效"
                )
            }
            if let contentSHA256 {
                try validateContentSHA256(contentSHA256)
                guard pdfMutationLeasesByDigest[
                    contentSHA256.lowercased()
                ]?[bookID] == mutationLease else {
                    throw NativeBookOCRError.storage(
                        "PDF 改页 OCR 摘要租约已失效"
                    )
                }
            }
            return
        }
        guard pdfMutationLeases[bookID] == nil else {
            throw NativeBookOCRError.storage(
                "PDF 改页期间拒绝并发 OCR 写入"
            )
        }
        if let contentSHA256 {
            try validateContentSHA256(contentSHA256)
            let digest = contentSHA256.lowercased()
            guard pdfMutationLeasesByDigest[digest]?[bookID] == nil else {
                throw NativeBookOCRError.storage(
                    "本书的 PDF 内容正在改页，拒绝并发 OCR 写入"
                )
            }
            guard pdfMutationTargetLeaseByDigest[digest] == nil else {
                throw NativeBookOCRError.storage(
                    "目标 PDF OCR sidecar 正在原子替换，拒绝并发写入"
                )
            }
        }
    }

    private func validatePDFMutationTicket(_ ticket: String) throws {
        guard ticket.range(
            of: #"^npmt_[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil else {
            throw NativeBookOCRError.storage("PDF 改页 ticket 无效")
        }
    }

    private func assertPDFMutationFileAccess(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validatePDFMutationTicket(ticket)
        try validateContentSHA256(stagedContentSHA256)
        let stagedDigest = stagedContentSHA256.lowercased()
        guard pdfMutationLeasesByDigest[stagedDigest]?[lease.bookID] == lease,
              pdfMutationTargetLeaseByDigest[stagedDigest] == lease else {
            throw NativeBookOCRError.storage(
                "目标 PDF OCR 摘要租约已失效"
            )
        }
    }

    private func pdfMutationURLs(
        ticket: String,
        stagedContentSHA256: String
    ) -> (staging: URL, target: URL, backup: URL) {
        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        return (
            staging: contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket).staging",
                isDirectory: true
            ),
            target: contentRoot.appendingPathComponent(
                stagedContentSHA256.lowercased(),
                isDirectory: true
            ),
            backup: contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket).backup",
                isDirectory: true
            )
        )
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[0-9a-fA-F]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func isValidAttachmentID(_ value: String) -> Bool {
        value == "ocr-formulas"
            || value.range(
                of: #"^ocr-page-[0-9]{6}$"#,
                options: .regularExpression
            ) != nil
    }

    private static func expectedDownloadURL(
        manifest: NativeBookOCRDerivedAttachmentManifest,
        attachmentID: String
    ) -> String {
        "/pdf/api/library/attachments/\(manifest.bookId)/\(attachmentID)"
            + "?contentSha256=\(manifest.contentSha256)"
            + "&revision=\(manifest.revision)"
    }

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        return decoder
    }
}
