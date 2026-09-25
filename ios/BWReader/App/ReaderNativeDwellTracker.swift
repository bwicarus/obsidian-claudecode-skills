import Foundation
import UIKit
import UIKit.UIGestureRecognizerSubclass

/// 原生 PDF 的读页停留统计（活动账本「读过这页」的原始数据）。
///
/// 判定照搬网页端 `reader.src/30-dwell.js`，阈值不在这里改 —— 「读过」由服务端聚合器判：
/// ① 只在阅读区真的显示着、App 在前台时计秒（sample 返回 nil = 这一秒不算）；
/// ② 单页 <3s 的碎片不上报（翻过 ≠ 读过）；
/// ③ 60s 无任何触摸/笔操作即停表（挂机不算）；
/// ④ 每 30s 上报一次，切后台/关书立即上报。
///
/// 为什么要有它：974 起 PDF 由 PDFView 显示，网页壳里没有 `.page-wrap` 页元素，
/// 触摸也不进 WebView —— 30-dwell.js 每秒都判「无可见页」，停留与随行定位从
/// 2026-09-25 05:18 起全空（实机账本查实）。
@MainActor
final class ReaderNativeDwellTracker {
    static let idleSeconds: TimeInterval = 60
    static let minimumSeconds = 3
    static let flushInterval: TimeInterval = 30

    /// (当前页, 最近一次用户操作)；nil = 这一秒不计（不在前台 / 阅读区没显示）。
    private let sample: () -> (page: Int, lastInteraction: Date)?
    private let flush: ([(page: Int, seconds: Int)]) -> Void
    private var seconds: [Int: Int] = [:]
    private var lastFlush = Date()
    private var timer: Timer?

    init(sample: @escaping () -> (page: Int, lastInteraction: Date)?,
         flush: @escaping ([(page: Int, seconds: Int)]) -> Void) {
        self.sample = sample
        self.flush = flush
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)   // 滚动时（tracking 模式）也要计秒
        self.timer = timer
    }

    private func tick() {
        if let value = sample(), value.page > 0,
           Date().timeIntervalSince(value.lastInteraction) <= Self.idleSeconds {
            seconds[value.page, default: 0] += 1
        }
        if Date().timeIntervalSince(lastFlush) >= Self.flushInterval { flushNow() }
    }

    /// 切后台 / 关书时调；也由定时器每 30s 调一次。
    func flushNow() {
        lastFlush = Date()
        let ready = seconds.filter { $0.value >= Self.minimumSeconds }
        guard !ready.isEmpty else { return }
        ready.keys.forEach { seconds[$0] = nil }
        flush(ready.sorted { $0.key < $1.key }.map { (page: $0.key, seconds: $0.value) })
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        flushNow()
    }
}

/// 只记录「有人碰了阅读区」，从不识别：touchesBegan 记时间后立刻 .failed，
/// 不延迟、不取消触摸、与所有手势同时识别 —— 滚动、选词、Pencil 都不受影响。
final class ReaderInteractionRecorder: UIGestureRecognizer, UIGestureRecognizerDelegate {
    private let onTouch: () -> Void

    init(onTouch: @escaping () -> Void) {
        self.onTouch = onTouch
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
        delegate = self
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        onTouch()
        state = .failed
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
}
