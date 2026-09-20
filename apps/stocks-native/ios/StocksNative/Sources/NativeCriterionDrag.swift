import SwiftUI
import UIKit

/// A condition-row touch surface. The editor owns its preview and drop targets.
struct NativeCriterionDrag: UIViewRepresentable {
    let onTap: () -> Void
    let onDrag: (CGPoint) -> Void
    let onEnd: (CGPoint?) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(callbacks: self) }

    func makeUIView(context: Context) -> NativeCriterionDragView {
        let view = NativeCriterionDragView()
        context.coordinator.attach(to: view)
        view.isUserInteractionEnabled = context.environment.isEnabled
        return view
    }

    func updateUIView(_ uiView: NativeCriterionDragView, context: Context) {
        context.coordinator.callbacks = self
        uiView.isUserInteractionEnabled = context.environment.isEnabled
        if !context.environment.isEnabled { context.coordinator.cancelDrag() }
    }

    static func dismantleUIView(_ uiView: NativeCriterionDragView, coordinator: Coordinator) {
        coordinator.detach(from: uiView)
    }

    @MainActor
    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var callbacks: NativeCriterionDrag
        private weak var sourceView: NativeCriterionDragView?
        private var pan: UIPanGestureRecognizer?
        private var tap: UITapGestureRecognizer?
        private var dragging = false

        init(callbacks: NativeCriterionDrag) { self.callbacks = callbacks }

        func attach(to view: NativeCriterionDragView) {
            sourceView = view
            let pan = UIPanGestureRecognizer(target: self, action: #selector(didPan(_:)))
            pan.minimumNumberOfTouches = 1
            pan.maximumNumberOfTouches = 1
            pan.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue),
                                     NSNumber(value: UITouch.TouchType.pencil.rawValue),
                                     NSNumber(value: UITouch.TouchType.indirectPointer.rawValue)]
            pan.cancelsTouchesInView = true
            pan.delegate = self
            let tap = UITapGestureRecognizer(target: self, action: #selector(didTap(_:)))
            tap.allowedTouchTypes = pan.allowedTouchTypes
            tap.cancelsTouchesInView = true
            tap.delegate = self
            tap.require(toFail: pan)
            view.addGestureRecognizer(pan)
            view.addGestureRecognizer(tap)
            view.onDetached = { [weak self] in self?.cancelDrag() }
            self.pan = pan
            self.tap = tap
        }

        func detach(from view: NativeCriterionDragView) {
            cancelDrag()
            view.onDetached = nil
            let gestures: [UIGestureRecognizer?] = [pan, tap]
            for gesture in gestures.compactMap({ $0 }) {
                gesture.delegate = nil
                gesture.removeTarget(self, action: nil)
                view.removeGestureRecognizer(gesture)
            }
            pan = nil
            tap = nil
            sourceView = nil
        }

        func cancelDrag() {
            guard dragging else { return }
            dragging = false
            callbacks.onEnd(nil)
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            guard let sourceView, gestureRecognizer.view === sourceView else { return false }
            return touch.view === sourceView && sourceView.bounds.contains(touch.location(in: sourceView))
        }

        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
            guard let sourceView, sourceView.window != nil, !dragging else { return false }
            guard let pan, gestureRecognizer === pan else { return true }
            let movement = pan.translation(in: sourceView)
            if abs(movement.x) + abs(movement.y) >= 2 { return abs(movement.x) > abs(movement.y) }
            let velocity = pan.velocity(in: sourceView)
            return abs(velocity.x) > abs(velocity.y)
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldBeRequiredToFailBy otherGestureRecognizer: UIGestureRecognizer) -> Bool {
            guard let pan, gestureRecognizer === pan, let scroll = nearestVerticalScrollView() else { return false }
            // Resolve direction before the immediate list scroll starts. This is a
            // per-attempt dependency, not a retained requirement on a long-lived scroll view.
            return otherGestureRecognizer === scroll.panGestureRecognizer
        }

        private func nearestVerticalScrollView() -> UIScrollView? {
            var ancestor = sourceView?.superview
            while let view = ancestor {
                if let scroll = view as? UIScrollView,
                   scroll.alwaysBounceVertical || scroll.contentSize.height > scroll.bounds.height + 1 {
                    return scroll
                }
                ancestor = view.superview
            }
            return nil
        }

        @objc private func didTap(_ gesture: UITapGestureRecognizer) {
            if gesture.state == .ended, !dragging, sourceView?.window != nil { callbacks.onTap() }
        }

        @objc private func didPan(_ gesture: UIPanGestureRecognizer) {
            switch gesture.state {
            case .began, .changed:
                guard let window = sourceView?.window else { cancelDrag(); return }
                dragging = true
                callbacks.onDrag(gesture.location(in: window))
            case .ended:
                guard dragging else { return }
                dragging = false
                callbacks.onEnd((sourceView?.window).map { gesture.location(in: $0) })
            case .cancelled, .failed:
                cancelDrag()
            default: break
            }
        }
    }
}

final class NativeCriterionDragView: UIView {
    var onDetached: (() -> Void)?

    init() {
        super.init(frame: .zero)
        backgroundColor = .clear
        isOpaque = false
        isAccessibilityElement = false
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window == nil { onDetached?() }
    }
}

/// Reports only the editor root; drop zones remain in the editor's named space.
struct NativeCriterionEditorFrame: UIViewRepresentable {
    let onFrame: (CGRect) -> Void

    func makeUIView(context: Context) -> NativeCriterionEditorFrameView {
        let view = NativeCriterionEditorFrameView()
        view.onFrame = onFrame
        return view
    }

    func updateUIView(_ uiView: NativeCriterionEditorFrameView, context: Context) {
        uiView.onFrame = onFrame
        uiView.scheduleReport()
    }

    static func dismantleUIView(_ uiView: NativeCriterionEditorFrameView, coordinator: ()) {
        uiView.onFrame = nil
    }
}

final class NativeCriterionEditorFrameView: UIView {
    var onFrame: ((CGRect) -> Void)?
    private var lastReportedFrame: CGRect?
    private var reportScheduled = false

    init() {
        super.init(frame: .zero)
        backgroundColor = .clear
        isOpaque = false
        isUserInteractionEnabled = false
        isAccessibilityElement = false
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        scheduleReport()
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        lastReportedFrame = nil
        scheduleReport()
    }

    func scheduleReport() {
        guard !reportScheduled else { return }
        reportScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.reportScheduled = false
            guard let window = self.window, self.bounds.width > 0, self.bounds.height > 0 else { return }
            let frame = self.convert(self.bounds, to: window)
            guard frame != self.lastReportedFrame else { return }
            self.lastReportedFrame = frame
            self.onFrame?(frame)
        }
    }
}
