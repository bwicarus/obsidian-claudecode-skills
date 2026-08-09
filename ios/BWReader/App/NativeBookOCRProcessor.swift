import CryptoKit
import Foundation
import NaturalLanguage
import PDFKit
import UIKit
import Vision

struct NativeBookOCRPageGeometry: Equatable, Sendable {
    let page: Int
    let cropBoxX: Double
    let cropBoxY: Double
    let pageWidth: Double
    let pageHeight: Double
    let rotation: Int
    let renderPixelWidth: Int
    let renderPixelHeight: Int
}

actor NativeBookOCRProcessor {
    private static let embeddedEngineRevision = "pdfkit-embedded-text/1"

    private let document: PDFDocument

    init(fileURL: URL) throws {
        guard let document = PDFDocument(url: fileURL) else {
            throw NativeBookOCRError.unreadableBook
        }
        self.document = document
    }

    func numberOfPages() -> Int {
        document.pageCount
    }

    func geometry(
        pageNumber: Int,
        configuration: NativeBookOCRConfiguration
    ) throws -> NativeBookOCRPageGeometry {
        guard pageNumber >= 1,
              let page = document.page(at: pageNumber - 1) else {
            throw NativeBookOCRError.pageUnavailable
        }
        let bounds = page.bounds(for: .cropBox)
        guard bounds.width.isFinite, bounds.height.isFinite,
              bounds.width > 0, bounds.height > 0 else {
            throw NativeBookOCRError.pageUnavailable
        }
        let rotation = Self.canonicalRotation(page.rotation)
        guard [0, 90, 180, 270].contains(rotation) else {
            throw NativeBookOCRError.unsupportedPageRotation(rotation)
        }
        let displayedSize = Self.displayedPageSize(
            unrotatedWidth: bounds.width,
            unrotatedHeight: bounds.height,
            rotation: rotation
        )
        let longEdge = max(displayedSize.width, displayedSize.height)
        let scale = max(
            1,
            CGFloat(configuration.renderLongEdgePixels) / longEdge
        )
        return NativeBookOCRPageGeometry(
            page: pageNumber,
            cropBoxX: Double(bounds.minX),
            cropBoxY: Double(bounds.minY),
            pageWidth: Double(displayedSize.width),
            pageHeight: Double(displayedSize.height),
            rotation: rotation,
            renderPixelWidth: max(1, Int((displayedSize.width * scale).rounded())),
            renderPixelHeight: max(1, Int((displayedSize.height * scale).rounded()))
        )
    }

    func processPage(
        pageNumber: Int,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration,
        forceVision: Bool = false
    ) throws -> NativeBookOCRPageCharacters {
        try Task.checkCancellation()
        guard pageNumber >= 1,
              let page = document.page(at: pageNumber - 1) else {
            throw NativeBookOCRError.pageUnavailable
        }
        let geometry = try geometry(
            pageNumber: pageNumber,
            configuration: configuration
        )

        if !forceVision, geometry.rotation == 0,
           let embedded = embeddedCharacters(
                page: page,
                geometry: geometry,
                minimumCharacters: configuration.minimumEmbeddedCharacters
           ) {
            let digest = Self.geometryDigest(
                contentSHA256: contentSHA256,
                geometry: geometry,
                engineRevision: Self.embeddedEngineRevision
            )
            return NativeBookOCRPageCharacters(
                schema: NativeBookOCRPageCharacters.schema,
                contentSHA256: contentSHA256,
                page: pageNumber,
                pageWidth: geometry.pageWidth,
                pageHeight: geometry.pageHeight,
                rotation: geometry.rotation,
                geometryDigest: digest,
                engineRevision: Self.embeddedEngineRevision,
                status: embedded.characters.isEmpty ? .readyEmpty : .ready,
                source: .apple,
                chars: embedded.characters,
                furigana: [],
                wordSegmentation: embedded.wordSegmentation,
                characterGeometry: embedded.characterGeometry,
                formulaCoverage: .unavailable,
                formulaRegions: [],
                createdAt: Date(),
                error: nil
            )
        }

        try Task.checkCancellation()
        let image = page.thumbnail(
            of: CGSize(
                width: CGFloat(geometry.renderPixelWidth),
                height: CGFloat(geometry.renderPixelHeight)
            ),
            for: .cropBox
        )
        guard let cgImage = image.cgImage else {
            throw NativeBookOCRError.unreadableBook
        }
        let actualGeometry = NativeBookOCRPageGeometry(
            page: geometry.page,
            cropBoxX: geometry.cropBoxX,
            cropBoxY: geometry.cropBoxY,
            pageWidth: geometry.pageWidth,
            pageHeight: geometry.pageHeight,
            rotation: geometry.rotation,
            renderPixelWidth: cgImage.width,
            renderPixelHeight: cgImage.height
        )
        let result = try recognize(
            cgImage: cgImage,
            geometry: actualGeometry,
            configuration: configuration
        )
        let digest = Self.geometryDigest(
            contentSHA256: contentSHA256,
            geometry: actualGeometry,
            engineRevision: NativeBookOCRConfiguration.engineRevision
        )
        return NativeBookOCRPageCharacters(
            schema: NativeBookOCRPageCharacters.schema,
            contentSHA256: contentSHA256,
            page: pageNumber,
            pageWidth: actualGeometry.pageWidth,
            pageHeight: actualGeometry.pageHeight,
            rotation: actualGeometry.rotation,
            geometryDigest: digest,
            engineRevision: NativeBookOCRConfiguration.engineRevision,
            status: result.status,
            source: .apple,
            chars: result.characters,
            furigana: [],
            wordSegmentation: result.wordSegmentation,
            characterGeometry: result.characterGeometry,
            formulaCoverage: result.formulaRegions.isEmpty
                ? .unavailable : .partial,
            formulaRegions: result.formulaRegions,
            createdAt: Date(),
            error: result.error
        )
    }

    static func failurePage(
        contentSHA256: String,
        geometry: NativeBookOCRPageGeometry,
        message: String
    ) -> NativeBookOCRPageCharacters {
        NativeBookOCRPageCharacters(
            schema: NativeBookOCRPageCharacters.schema,
            contentSHA256: contentSHA256,
            page: geometry.page,
            pageWidth: geometry.pageWidth,
            pageHeight: geometry.pageHeight,
            rotation: geometry.rotation,
            geometryDigest: geometryDigest(
                contentSHA256: contentSHA256,
                geometry: geometry,
                engineRevision: NativeBookOCRConfiguration.engineRevision
            ),
            engineRevision: NativeBookOCRConfiguration.engineRevision,
            status: .failed,
            source: .apple,
            chars: [],
            furigana: [],
            wordSegmentation: .unavailable,
            characterGeometry: .unavailable,
            formulaCoverage: .unknown,
            formulaRegions: [],
            createdAt: Date(),
            error: message
        )
    }

    static func geometryDigest(
        contentSHA256: String,
        geometry: NativeBookOCRPageGeometry,
        engineRevision: String
    ) -> String {
        let value = [
            "reader-native-page-geometry/1",
            contentSHA256.lowercased(),
            String(geometry.page),
            String(format: "%.6f", geometry.cropBoxX),
            String(format: "%.6f", geometry.cropBoxY),
            String(format: "%.6f", geometry.pageWidth),
            String(format: "%.6f", geometry.pageHeight),
            String(geometry.rotation),
            String(geometry.renderPixelWidth),
            String(geometry.renderPixelHeight),
            engineRevision,
        ].joined(separator: "\u{0}")
        return SHA256.hash(data: Data(value.utf8)).map {
            String(format: "%02x", $0)
        }.joined()
    }

    private struct PageExtraction {
        let characters: [NativeBookOCRCharacter]
        let wordSegmentation: NativeBookOCRWordSegmentationState
        let characterGeometry: NativeBookOCRCharacterGeometryState
    }

    private struct VisionExtraction {
        let status: NativeBookOCRPageState
        let characters: [NativeBookOCRCharacter]
        let wordSegmentation: NativeBookOCRWordSegmentationState
        let characterGeometry: NativeBookOCRCharacterGeometryState
        let formulaRegions: [NativeBookOCRFormulaRegion]
        let error: String?
    }

    private func embeddedCharacters(
        page: PDFPage,
        geometry: NativeBookOCRPageGeometry,
        minimumCharacters: Int
    ) -> PageExtraction? {
        guard let text = page.string else { return nil }
        let visibleCount = text.filter { !$0.isWhitespace }.count
        guard visibleCount >= minimumCharacters else { return nil }

        let tokens = Self.wordTokenRanges(in: text)
        var nextBlock = 0
        var characters: [NativeBookOCRCharacter] = []
        var exactGeometry = true
        text.enumerateSubstrings(
            in: text.startIndex..<text.endIndex,
            options: .byComposedCharacterSequences
        ) { substring, range, _, _ in
            guard let substring, !substring.isEmpty else { return }
            if substring.contains("\n") || substring.contains("\r") {
                nextBlock += 1
                return
            }
            let nsRange = NSRange(range, in: text)
            let rawBounds = page.selection(for: nsRange)?.bounds(for: page)
            let rect: CGRect
            if let rawBounds,
               rawBounds.width.isFinite, rawBounds.height.isFinite,
               rawBounds.width > 0, rawBounds.height > 0 {
                let pageBounds = page.bounds(for: .cropBox)
                rect = CGRect(
                    x: rawBounds.minX - pageBounds.minX,
                    y: pageBounds.maxY - rawBounds.maxY,
                    width: rawBounds.width,
                    height: rawBounds.height
                )
            } else if let previous = characters.last {
                exactGeometry = false
                let fallbackWidth = max(1, previous.x1 - previous.x0) * 0.45
                rect = CGRect(
                    x: previous.x1,
                    y: previous.y0,
                    width: fallbackWidth,
                    height: max(1, previous.y1 - previous.y0)
                )
            } else {
                exactGeometry = false
                return
            }
            guard rect.minX >= -1, rect.minY >= -1,
                  rect.maxX <= CGFloat(geometry.pageWidth) + 1,
                  rect.maxY <= CGFloat(geometry.pageHeight) + 1 else {
                exactGeometry = false
                return
            }
            let wordID = Self.wordID(for: range, tokens: tokens)
            characters.append(NativeBookOCRCharacter(
                c: substring,
                x0: Self.rounded(rect.minX),
                y0: Self.rounded(rect.minY),
                x1: Self.rounded(rect.maxX),
                y1: Self.rounded(rect.maxY),
                sp: substring.allSatisfy(\.isWhitespace) ? 1 : 0,
                w: substring.allSatisfy(\.isWhitespace) ? -1 : wordID,
                b: 0,
                bk: nextBlock
            ))
        }
        let recognizedCount = characters.filter { $0.sp == 0 }.count
        guard recognizedCount >= minimumCharacters else { return nil }
        let segmentation = Self.segmentationState(
            characters: characters,
            expectedTextCharacters: recognizedCount
        )
        return PageExtraction(
            characters: characters,
            wordSegmentation: segmentation,
            characterGeometry: exactGeometry ? .exact : .estimated
        )
    }

    private func recognize(
        cgImage: CGImage,
        geometry: NativeBookOCRPageGeometry,
        configuration: NativeBookOCRConfiguration
    ) throws -> VisionExtraction {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = configuration.usesLanguageCorrection
        request.automaticallyDetectsLanguage = true
        let supportedLanguages: Set<String>
        do {
            supportedLanguages = Set(try request.supportedRecognitionLanguages())
        } catch {
            throw NativeBookOCRError.unsupported
        }
        let requestedLanguages = configuration.recognitionLanguages.filter {
            supportedLanguages.contains($0)
        }
        guard !requestedLanguages.isEmpty else {
            throw NativeBookOCRError.unsupported
        }
        request.recognitionLanguages = requestedLanguages

        let handler = VNImageRequestHandler(
            cgImage: cgImage,
            orientation: .up,
            options: [:]
        )
        do {
            try handler.perform([request])
        } catch {
            let nsError = error as NSError
            let unsupportedCodes: Set<Int> = [
                VNErrorCode.unsupportedRequest.rawValue,
                VNErrorCode.unsupportedRevision.rawValue,
                VNErrorCode.unsupportedComputeDevice.rawValue,
                VNErrorCode.unsupportedComputeStage.rawValue,
                VNErrorCode.notImplemented.rawValue,
            ]
            if nsError.domain == VNErrorDomain,
               unsupportedCodes.contains(nsError.code) {
                throw NativeBookOCRError.unsupported
            }
            throw error
        }
        try Task.checkCancellation()

        let observations = request.results ?? []
        var characters: [NativeBookOCRCharacter] = []
        var formulaRegions: [NativeBookOCRFormulaRegion] = []
        var nextWordID = 0
        var estimatedGeometry = false
        var segmentationUnavailable = false
        var segmentationPartial = false
        var qualityWeight: Float = 0
        var weightedConfidence: Float = 0

        for (blockID, observation) in observations.enumerated() {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                continue
            }
            let isFormula = Self.looksLikeFormula(text)
            if isFormula {
                let rect = Self.pageRect(
                    normalized: observation.boundingBox,
                    geometry: geometry
                )
                formulaRegions.append(NativeBookOCRFormulaRegion(
                    id: "formula-p\(geometry.page)-\(blockID + 1)",
                    x0: Self.rounded(rect.minX),
                    y0: Self.rounded(rect.minY),
                    x1: Self.rounded(rect.maxX),
                    y1: Self.rounded(rect.maxY),
                    state: .pending,
                    latex: nil,
                    multiline: nil,
                    error: nil
                ))
                // Vision does not understand LaTeX. Keep the candidate as a
                // pending formula region and do not leak its glyph guess into
                // the ordinary character layer or word-quality calculation.
                continue
            } else {
                let weight = Float(max(1, text.filter { !$0.isWhitespace }.count))
                qualityWeight += weight
                weightedConfidence += candidate.confidence * weight
            }

            let tokenRanges = Self.wordTokenRanges(in: text)
            let hasTokens = !tokenRanges.isEmpty
            if !hasTokens { segmentationUnavailable = true }
            var blockCharacters: [NativeBookOCRCharacter] = []
            var localWordIDs: [Int: Int] = [:]
            for token in tokenRanges {
                localWordIDs[token.id] = nextWordID
                nextWordID += 1
            }
            var allExact = true

            // Two passes, because a line may mix exact and estimated boxes.
            //
            // Vision returns per-character boxes for some glyphs and refuses
            // others, especially in dense CJK. Filling the refusals by dividing
            // the line into equal widths put those characters somewhere the
            // text is not, while their neighbours stayed correct -- so a
            // selection swept across the line caught only the accurate ones and
            // skipped the rest. That is the "every few characters" selection:
            // not missing text, misplaced text.
            //
            // The first pass records what Vision actually knows. The second
            // fills each gap by interpolating between the nearest known boxes
            // on either side, which keeps positions ordered and non-overlapping
            // within the line. Equal division survives only for a line Vision
            // could not place at all, where there is nothing better to infer
            // from.
            var pieces: [(text: String, range: Range<String.Index>, rect: CGRect?)] = []
            text.enumerateSubstrings(
                in: text.startIndex..<text.endIndex,
                options: .byComposedCharacterSequences
            ) { substring, range, _, _ in
                guard let substring, !substring.isEmpty else { return }
                let exact = (try? candidate.boundingBox(for: range))?.boundingBox
                if exact == nil { allExact = false }
                pieces.append((substring, range, exact))
            }

            let resolved = Self.resolvedCharacterRects(
                pieces: pieces.map { $0.rect },
                observation: observation.boundingBox
            )

            for (offset, piece) in pieces.enumerated() {
                let rect = Self.pageRect(
                    normalized: resolved[offset],
                    geometry: geometry
                )
                let localWord = Self.wordID(for: piece.range, tokens: tokenRanges)
                let wordID = localWordIDs[localWord] ?? -1
                if !piece.text.allSatisfy(\.isWhitespace) && wordID < 0 {
                    segmentationPartial = true
                }
                blockCharacters.append(NativeBookOCRCharacter(
                    c: piece.text,
                    x0: Self.rounded(rect.minX),
                    y0: Self.rounded(rect.minY),
                    x1: Self.rounded(rect.maxX),
                    y1: Self.rounded(rect.maxY),
                    sp: piece.text.allSatisfy(\.isWhitespace) ? 1 : 0,
                    w: piece.text.allSatisfy(\.isWhitespace) ? -1 : wordID,
                    b: 0,
                    bk: blockID
                ))
            }
            if !allExact { estimatedGeometry = true }
            characters.append(contentsOf: blockCharacters)
        }

        let meaningfulCharacters = characters.filter { $0.sp == 0 }
        let status: NativeBookOCRPageState
        let error: String?
        if meaningfulCharacters.isEmpty, !formulaRegions.isEmpty {
            status = .readyEmpty
            error = nil
        } else if meaningfulCharacters.isEmpty {
            if Self.imageHasVisibleInk(cgImage) {
                status = .failed
                error = NativeBookOCRError.noRecognizedText.localizedDescription
            } else {
                status = .readyEmpty
                error = nil
            }
        } else if qualityWeight > 0,
                  weightedConfidence / qualityWeight
                    < configuration.minimumVisionConfidence {
            // Formula-like observations are deliberately excluded: Vision is
            // not a LaTeX engine and formula glyphs must not fail ordinary OCR.
            status = .failed
            error = NativeBookOCRError.lowConfidence.localizedDescription
        } else {
            status = .ready
            error = nil
        }

        let wordSegmentation: NativeBookOCRWordSegmentationState
        if meaningfulCharacters.isEmpty || segmentationUnavailable {
            wordSegmentation = segmentationPartial ? .partial : .unavailable
        } else if segmentationPartial {
            wordSegmentation = .partial
        } else {
            wordSegmentation = .ready
        }
        return VisionExtraction(
            status: status,
            characters: characters,
            wordSegmentation: wordSegmentation,
            characterGeometry: characters.isEmpty
                ? .unavailable : (estimatedGeometry ? .estimated : .exact),
            formulaRegions: formulaRegions,
            error: error
        )
    }

    private struct WordTokenRange {
        let id: Int
        let range: Range<String.Index>
    }

    private static func wordTokenRanges(in text: String) -> [WordTokenRange] {
        guard !text.isEmpty else { return [] }
        let tokenizer = NLTokenizer(unit: .word)
        tokenizer.string = text
        if let language = NLLanguageRecognizer.dominantLanguage(for: text) {
            tokenizer.setLanguage(language)
        }
        var ranges: [WordTokenRange] = []
        tokenizer.enumerateTokens(
            in: text.startIndex..<text.endIndex
        ) { range, _ in
            ranges.append(WordTokenRange(id: ranges.count, range: range))
            return true
        }
        return ranges
    }

    private static func wordID(
        for characterRange: Range<String.Index>,
        tokens: [WordTokenRange]
    ) -> Int {
        tokens.first(where: {
            $0.range.contains(characterRange.lowerBound)
        })?.id ?? -1
    }

    private static func segmentationState(
        characters: [NativeBookOCRCharacter],
        expectedTextCharacters: Int
    ) -> NativeBookOCRWordSegmentationState {
        let assigned = characters.filter { $0.sp == 0 && $0.w >= 0 }.count
        if assigned == 0 { return .unavailable }
        return assigned >= expectedTextCharacters ? .ready : .partial
    }

    private static func looksLikeFormula(_ text: String) -> Bool {
        let compact = text.replacingOccurrences(of: " ", with: "")
        guard !compact.isEmpty else { return false }
        let explicit = CharacterSet(charactersIn: "=±×÷∑∫√∞≈≠≤≥∂∆∇∈∉∪∩⊂⊃→↔^_{}[]")
        let symbolCount = compact.unicodeScalars.filter {
            explicit.contains($0)
        }.count
        if symbolCount >= 2 { return true }
        if symbolCount == 1,
           compact.range(
                of: #"[0-9A-Za-z\)\]]\s*[=<>±×÷≈≠≤≥]\s*[0-9A-Za-z\(\[]"#,
                options: .regularExpression
           ) != nil {
            return true
        }
        return false
    }

    /// Fills in the characters Vision could not place, using the ones it could.
    ///
    /// Vision returns a box for some glyphs in a line and declines others. The
    /// declined ones used to be spread evenly across the whole line, which put
    /// them at coordinates the text does not occupy while their neighbours
    /// stayed correct. A selection dragged across such a line then matched only
    /// the accurate characters and stepped over the rest.
    ///
    /// Known boxes are kept exactly as reported. Each run of unknowns is spread
    /// between the last known box before it and the first known box after it,
    /// so order and spacing stay plausible and no two characters overlap. A run
    /// at either end extends from the single neighbour it has. Only a line with
    /// no known box at all falls back to equal division, where nothing better
    /// can be inferred.
    private static func resolvedCharacterRects(
        pieces: [CGRect?],
        observation: CGRect
    ) -> [CGRect] {
        let count = pieces.count
        guard count > 0 else { return [] }
        let vertical = observation.height > observation.width * 1.4
        var output = [CGRect](repeating: .zero, count: count)
        let knownIndexes = (0..<count).filter { pieces[$0] != nil }
        guard !knownIndexes.isEmpty else {
            for index in 0..<count {
                output[index] = estimatedCharacterRect(
                    observation: observation,
                    index: index,
                    count: count
                )
            }
            return output
        }
        for index in knownIndexes { output[index] = pieces[index]! }

        // Spans the gap between two placed characters, or off the end of one.
        func fill(from lower: Int?, to upper: Int?) {
            let start = (lower.map { $0 + 1 }) ?? 0
            let end = (upper ?? count) - 1
            guard start <= end else { return }
            let span = end - start + 1
            let leading = lower.map { output[$0] }
            let trailing = upper.map { output[$0] }
            let origin: CGFloat
            let extent: CGFloat
            if vertical {
                let top = leading.map { $0.minY } ?? observation.maxY
                let bottom = trailing.map { $0.maxY } ?? observation.minY
                origin = min(top, bottom)
                extent = max(0, abs(top - bottom))
            } else {
                let left = leading.map { $0.maxX } ?? observation.minX
                let right = trailing.map { $0.minX } ?? observation.maxX
                origin = min(left, right)
                extent = max(0, abs(right - left))
            }
            let step = extent / CGFloat(span)
            for offset in 0..<span {
                let index = start + offset
                if vertical {
                    output[index] = CGRect(
                        x: observation.minX,
                        y: origin + CGFloat(span - offset - 1) * step,
                        width: observation.width,
                        height: step
                    )
                } else {
                    output[index] = CGRect(
                        x: origin + CGFloat(offset) * step,
                        y: observation.minY,
                        width: step,
                        height: observation.height
                    )
                }
            }
        }

        fill(from: nil, to: knownIndexes.first)
        for pair in zip(knownIndexes, knownIndexes.dropFirst()) {
            fill(from: pair.0, to: pair.1)
        }
        fill(from: knownIndexes.last, to: nil)
        return output
    }

    private static func estimatedCharacterRect(
        observation: CGRect,
        index: Int,
        count: Int
    ) -> CGRect {
        let denominator = CGFloat(max(1, count))
        if observation.height > observation.width * 1.4 {
            let height = observation.height / denominator
            return CGRect(
                x: observation.minX,
                y: observation.maxY - CGFloat(index + 1) * height,
                width: observation.width,
                height: height
            )
        }
        let width = observation.width / denominator
        return CGRect(
            x: observation.minX + CGFloat(index) * width,
            y: observation.minY,
            width: width,
            height: observation.height
        )
    }

    private static func pageRect(
        normalized: CGRect,
        geometry: NativeBookOCRPageGeometry
    ) -> CGRect {
        // PDFPage.thumbnail is already rotated for display. Its Vision boxes
        // therefore map directly into the rotated PDF.js viewport; applying
        // rotation a second time would mirror 90/270-degree pages.
        let pageWidth = CGFloat(geometry.pageWidth)
        let pageHeight = CGFloat(geometry.pageHeight)
        return CGRect(
            x: normalized.minX * pageWidth,
            y: (1 - normalized.maxY) * pageHeight,
            width: normalized.width * pageWidth,
            height: normalized.height * pageHeight
        ).standardized
    }

    private static func imageHasVisibleInk(_ image: CGImage) -> Bool {
        let width = 64
        let height = 64
        var pixels = [UInt8](repeating: 255, count: width * height)
        let colorSpace = CGColorSpaceCreateDeviceGray()
        let rendered = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else { return false }
            context.setFillColor(gray: 1, alpha: 1)
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
            context.interpolationQuality = .low
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard rendered else { return true }
        let dark = pixels.reduce(into: 0) { count, value in
            if value < 232 { count += 1 }
        }
        return dark >= max(8, pixels.count / 200)
    }

    private static func canonicalRotation(_ value: Int) -> Int {
        let normalized = value % 360
        return normalized < 0 ? normalized + 360 : normalized
    }

    static func displayedPageSize(
        unrotatedWidth: CGFloat,
        unrotatedHeight: CGFloat,
        rotation: Int
    ) -> CGSize {
        switch canonicalRotation(rotation) {
        case 90, 270:
            return CGSize(width: unrotatedHeight, height: unrotatedWidth)
        default:
            return CGSize(width: unrotatedWidth, height: unrotatedHeight)
        }
    }

    private static func rounded(_ value: CGFloat) -> Double {
        (Double(value) * 100).rounded() / 100
    }
}
