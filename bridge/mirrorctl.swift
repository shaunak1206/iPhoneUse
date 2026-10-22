// mirrorctl: the macOS half of iphone-mcp.
//
// Drives the iPhone Mirroring app (com.apple.ScreenContinuity): finds its
// window, captures it with ScreenCaptureKit, runs Vision OCR on the capture,
// and posts mouse/keyboard events into it. Every command prints one JSON
// object to stdout. Errors print {"error": "..."} and exit 1.
//
// Build: swiftc -O bridge/mirrorctl.swift -o bin/mirrorctl

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

let bundleID = "com.apple.ScreenContinuity"

// MARK: - output

func emit(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func fail(_ msg: String) -> Never {
    emit(["error": msg])
    exit(1)
}

// MARK: - app + windows

func mirrorApp() -> NSRunningApplication? {
    NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first
}

struct Win {
    let id: CGWindowID
    let frame: CGRect
    let onscreen: Bool
    var dict: [String: Any] {
        ["id": Int(id), "x": frame.minX, "y": frame.minY, "w": frame.width, "h": frame.height, "onscreen": onscreen]
    }
}

func appWindows(_ pid: pid_t) -> [Win] {
    guard let list = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else { return [] }
    var out: [Win] = []
    for w in list {
        guard (w[kCGWindowOwnerPID as String] as? Int).map({ pid_t($0) }) == pid,
              (w[kCGWindowLayer as String] as? Int) == 0,
              let b = w[kCGWindowBounds as String] as? [String: CGFloat],
              let num = w[kCGWindowNumber as String] as? Int else { continue }
        let r = CGRect(x: b["X"] ?? 0, y: b["Y"] ?? 0, width: b["Width"] ?? 0, height: b["Height"] ?? 0)
        if r.height < 100 { continue }  // menu-bar slivers
        out.append(Win(id: CGWindowID(num), frame: r, onscreen: (w[kCGWindowIsOnscreen as String] as? Bool) ?? false))
    }
    return out
}

/// The mirrored-phone window: on screen and phone-shaped (tall).
func phoneWindow(_ pid: pid_t) -> Win? {
    appWindows(pid)
        .filter { $0.onscreen && $0.frame.height / max($0.frame.width, 1) > 1.6 }
        .max { $0.frame.height < $1.frame.height }
}

// MARK: - accessibility

func ax(_ e: AXUIElement, _ a: String) -> AnyObject? {
    var v: AnyObject?
    AXUIElementCopyAttributeValue(e, a as CFString, &v)
    return v
}

func axChildren(_ e: AXUIElement) -> [AXUIElement] {
    (ax(e, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func axLabel(_ e: AXUIElement) -> String {
    for a in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
        if let s = ax(e, a) as? String, !s.isEmpty { return s }
    }
    return ""
}

/// Text and buttons shown by the Mirroring app's own windows (onboarding,
/// "iPhone in use", connection errors). The phone's content is a video
/// surface, so it shows up here as nothing.
func axWindowSummary(_ pid: pid_t) -> [[String: Any]] {
    let root = AXUIElementCreateApplication(pid)
    var wins: [[String: Any]] = []
    for w in (ax(root, kAXWindowsAttribute) as? [AXUIElement]) ?? [] {
        var texts: [String] = []
        var buttons: [String] = []
        func walk(_ e: AXUIElement, _ d: Int) {
            if d > 10 { return }
            let role = ax(e, kAXRoleAttribute) as? String ?? ""
            let label = axLabel(e)
            if role == "AXStaticText", !label.isEmpty { texts.append(label) }
            if role == "AXButton" || role == "AXLink", !label.isEmpty { buttons.append(label) }
            for c in axChildren(e) { walk(c, d + 1) }
        }
        walk(w, 0)
        wins.append([
            "title": ax(w, kAXTitleAttribute) as? String ?? "",
            "identifier": ax(w, kAXIdentifierAttribute) as? String ?? "",
            "texts": texts,
            "buttons": buttons,
        ])
    }
    return wins
}

func axPressButton(_ pid: pid_t, _ label: String) -> Bool {
    let root = AXUIElementCreateApplication(pid)
    func find(_ e: AXUIElement, _ d: Int) -> AXUIElement? {
        if d > 10 { return nil }
        let role = ax(e, kAXRoleAttribute) as? String ?? ""
        if role == "AXMenuBar" { return nil }
        if (role == "AXButton" || role == "AXLink"), axLabel(e).caseInsensitiveCompare(label) == .orderedSame { return e }
        for c in axChildren(e) { if let f = find(c, d + 1) { return f } }
        return nil
    }
    guard let b = find(root, 0) else { return false }
    return AXUIElementPerformAction(b, kAXPressAction as CFString) == .success
}

func axPressMenu(_ pid: pid_t, _ title: String) -> (found: Bool, enabled: Bool, ok: Bool) {
    let root = AXUIElementCreateApplication(pid)
    guard let mb = ax(root, kAXMenuBarAttribute) else { return (false, false, false) }
    for top in axChildren(mb as! AXUIElement) {
        for menu in axChildren(top) {
            for item in axChildren(menu) where (ax(item, kAXTitleAttribute) as? String) == title {
                let enabled = (ax(item, kAXEnabledAttribute) as? Bool) ?? false
                if !enabled { return (true, false, false) }
                return (true, true, AXUIElementPerformAction(item, kAXPressAction as CFString) == .success)
            }
        }
    }
    return (false, false, false)
}

// MARK: - capture + OCR

final class Box<T>: @unchecked Sendable { var value: Result<T, Error>? }

/// Runs async work from this synchronous CLI, spinning the main run loop
/// (ScreenCaptureKit wants one) until it finishes.
func runAsync<T>(_ body: @escaping () async throws -> T) throws -> T {
    let box = Box<T>()
    Task { do { box.value = .success(try await body()) } catch { box.value = .failure(error) } }
    while box.value == nil { RunLoop.main.run(until: Date().addingTimeInterval(0.005)) }
    return try box.value!.get()
}

func captureWindow(_ id: CGWindowID) throws -> CGImage {
    try runAsync {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let w = content.windows.first(where: { $0.windowID == id }) else {
            throw NSError(domain: "mirrorctl", code: 2, userInfo: [NSLocalizedDescriptionKey: "window \(id) not capturable (is it on screen?)"])
        }
        let filter = SCContentFilter(desktopIndependentWindow: w)
        let cfg = SCStreamConfiguration()
        let scale = CGFloat(filter.pointPixelScale)
        cfg.width = Int(w.frame.width * scale)
        cfg.height = Int(w.frame.height * scale)
        cfg.showsCursor = false
        cfg.ignoreShadowsSingleWindow = true
        return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
    }
}

func loadImage(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

/// Redraws `img` (optionally cropped, normalized rect) onto an opaque canvas
/// of the given width. Opaque because the window has transparent rounded
/// corners, which JPEG would turn into noise.
func render(_ img: CGImage, crop: CGRect?, width: Int?) -> CGImage {
    var src = img
    if let c = crop {
        let r = CGRect(x: c.minX * CGFloat(img.width), y: c.minY * CGFloat(img.height),
                       width: c.width * CGFloat(img.width), height: c.height * CGFloat(img.height)).integral
        if let cropped = img.cropping(to: r) { src = cropped }
    }
    let w = width ?? src.width
    let h = max(1, Int((CGFloat(src.height) * CGFloat(w) / CGFloat(src.width)).rounded()))
    let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    ctx.setFillColor(CGColor(gray: 0, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.interpolationQuality = .high
    ctx.draw(src, in: CGRect(x: 0, y: 0, width: w, height: h))
    return ctx.makeImage()!
}

func write(_ img: CGImage, _ path: String, jpeg: Bool) throws {
    let type = (jpeg ? UTType.jpeg : UTType.png).identifier as CFString
    guard let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, type, 1, nil) else {
        throw NSError(domain: "mirrorctl", code: 3, userInfo: [NSLocalizedDescriptionKey: "cannot write \(path)"])
    }
    let props: [CFString: Any] = jpeg ? [kCGImageDestinationLossyCompressionQuality: 0.8] : [:]
    CGImageDestinationAddImage(dest, img, props as CFDictionary)
    CGImageDestinationFinalize(dest)
}

/// Vision OCR. Boxes are normalized to the image, origin top-left.
func ocr(_ img: CGImage, fast: Bool) throws -> [[String: Any]] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fast ? .fast : .accurate
    req.usesLanguageCorrection = !fast
    try VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    var out: [[String: Any]] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        out.append([
            "text": cand.string,
            "conf": (Double(cand.confidence) * 100).rounded() / 100,
            "x": b.minX, "y": 1 - b.maxY, "w": b.width, "h": b.height,
        ])
    }
    return out
}

// MARK: - input

let source = CGEventSource(stateID: .hidSystemState)

func post(_ e: CGEvent?) {
    e?.post(tap: .cghidEventTap)
}

func sleepMs(_ ms: Double) {
    if ms > 0 { usleep(useconds_t(ms * 1000)) }
}

func mouse(_ type: CGEventType, _ p: CGPoint, clickState: Int64 = 1) {
    let e = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p, mouseButton: .left)
    e?.setIntegerValueField(.mouseEventClickState, value: clickState)
    post(e)
}

func click(_ p: CGPoint, holdMs: Double, count: Int) {
    mouse(.mouseMoved, p)
    sleepMs(40)
    for i in 1...max(1, count) {
        mouse(.leftMouseDown, p, clickState: Int64(i))
        sleepMs(holdMs)
        mouse(.leftMouseUp, p, clickState: Int64(i))
        if i < count { sleepMs(60) }
    }
}

func drag(_ a: CGPoint, _ b: CGPoint, ms: Double, holdMs: Double) {
    mouse(.mouseMoved, a)
    sleepMs(40)
    mouse(.leftMouseDown, a)
    sleepMs(max(holdMs, 30))
    let steps = max(8, Int(ms / 12))
    for i in 1...steps {
        let t = Double(i) / Double(steps)
        // ease in-out so the phone sees a natural pan, not a fling
        let k = CGFloat(t < 0.5 ? 2 * t * t : 1 - pow(-2 * t + 2, 2) / 2)
        mouse(.leftMouseDragged, CGPoint(x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k))
        sleepMs(ms / Double(steps))
    }
    sleepMs(60)
    mouse(.leftMouseUp, b)
}

/// Trackpad-style continuous scroll with gesture phases, in pixels.
func scroll(_ p: CGPoint, dx: Int32, dy: Int32, steps: Int) {
    mouse(.mouseMoved, p)
    sleepMs(40)
    func ev(_ y: Int32, _ x: Int32, phase: Int64) {
        let e = CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2, wheel1: y, wheel2: x, wheel3: 0)
        e?.location = p
        e?.setIntegerValueField(.scrollWheelEventIsContinuous, value: 1)
        e?.setIntegerValueField(.scrollWheelEventScrollPhase, value: phase)
        post(e)
    }
    let n = max(1, steps)
    ev(0, 0, phase: 1)  // began
    for _ in 0..<n {
        ev(dy / Int32(n), dx / Int32(n), phase: 2)  // changed
        sleepMs(12)
    }
    ev(0, 0, phase: 4)  // ended
}

let keyNames: [String: CGKeyCode] = [
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
]

// US ANSI layout: char -> (keycode, shift)
let charKeys: [Character: (CGKeyCode, Bool)] = {
    var m: [Character: (CGKeyCode, Bool)] = [:]
    let plain: [(String, CGKeyCode)] = [
        ("a", 0), ("s", 1), ("d", 2), ("f", 3), ("h", 4), ("g", 5), ("z", 6), ("x", 7), ("c", 8), ("v", 9),
        ("b", 11), ("q", 12), ("w", 13), ("e", 14), ("r", 15), ("y", 16), ("t", 17), ("1", 18), ("2", 19),
        ("3", 20), ("4", 21), ("6", 22), ("5", 23), ("=", 24), ("9", 25), ("7", 26), ("-", 27), ("8", 28),
        ("0", 29), ("]", 30), ("o", 31), ("u", 32), ("[", 33), ("i", 34), ("p", 35), ("l", 37), ("j", 38),
        ("'", 39), ("k", 40), (";", 41), ("\\", 42), (",", 43), ("/", 44), ("n", 45), ("m", 46), (".", 47),
        ("`", 50), (" ", 49), ("\n", 36), ("\t", 48),
    ]
    for (s, k) in plain { m[Character(s)] = (k, false) }
    for (s, k) in plain where s.count == 1 && s.first!.isLetter { m[Character(s.uppercased())] = (k, true) }
    let shifted: [(String, String)] = [
        ("!", "1"), ("@", "2"), ("#", "3"), ("$", "4"), ("%", "5"), ("^", "6"), ("&", "7"), ("*", "8"),
        ("(", "9"), (")", "0"), ("_", "-"), ("+", "="), ("{", "["), ("}", "]"), ("|", "\\"), (":", ";"),
        ("\"", "'"), ("<", ","), (">", "."), ("?", "/"), ("~", "`"),
    ]
    for (s, base) in shifted { m[Character(s)] = (m[Character(base)]!.0, true) }
    return m
}()

func parseMods(_ s: String) -> CGEventFlags {
    var f: CGEventFlags = []
    for part in s.lowercased().split(whereSeparator: { $0 == "," || $0 == "+" }) {
        switch part {
        case "cmd", "command": f.insert(.maskCommand)
        case "shift": f.insert(.maskShift)
        case "opt", "option", "alt": f.insert(.maskAlternate)
        case "ctrl", "control": f.insert(.maskControl)
        default: break
        }
    }
    return f
}

func key(_ code: CGKeyCode, _ flags: CGEventFlags) {
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down)
        e?.flags = flags
        post(e)
        sleepMs(down ? 25 : 15)
    }
}

/// Types text with real keycodes (the phone sees a hardware keyboard).
/// Returns characters that have no US-layout key and were sent as unicode.
func typeText(_ text: String, delayMs: Double) -> [String] {
    var unmapped: [String] = []
    for ch in text {
        if let (code, shift) = charKeys[ch] {
            key(code, shift ? .maskShift : [])
        } else {
            unmapped.append(String(ch))
            let utf16 = Array(String(ch).utf16)
            for down in [true, false] {
                let e = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)
                e?.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: utf16)
                post(e)
            }
        }
        sleepMs(delayMs)
    }
    return unmapped
}

// MARK: - command dispatch

// MARK: - phone content rect

/// The phone screen inside the window capture, in pixels. The window has a
/// transparent border plus a taller transparent strip on top (where the
/// hover controls appear), so the screen is found from the alpha channel.
/// `aspect` (height / width of the phone in points), when given, overrides a
/// top edge that disagrees with it, which happens while the hover title bar
/// is drawn.
func contentRect(_ img: CGImage, aspect: Double?) -> CGRect {
    let w = img.width, h = img.height
    let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    let buf = ctx.data!.bindMemory(to: UInt8.self, capacity: w * h * 4)
    func alpha(_ x: Int, _ y: Int) -> UInt8 { buf[(y * w + x) * 4 + 3] }  // row 0 = top
    let midY = h / 2, midX = w / 2
    guard let left = (0..<w).first(where: { alpha($0, midY) > 128 }),
          let right = (0..<w).reversed().first(where: { alpha($0, midY) > 128 }),
          let bottom = (0..<h).reversed().first(where: { alpha(midX, $0) > 128 }),
          let top = (0..<h).first(where: { alpha(midX, $0) > 128 }) else {
        return CGRect(x: 0, y: 0, width: w, height: h)
    }
    let width = right - left + 1
    var r = CGRect(x: left, y: top, width: width, height: bottom - top + 1)
    if let aspect, abs(r.height / r.width - aspect) / aspect > 0.02 {
        let height = Int((Double(width) * aspect).rounded())
        r = CGRect(x: left, y: max(0, bottom + 1 - height), width: width, height: min(height, bottom + 1))
    }
    return r
}

// MARK: - commands

struct CmdError: Error { let msg: String }

final class Args {
    var a: [String]
    init(_ a: [String]) { self.a = a }
    func flag(_ name: String) -> String? {
        guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
        return a[i + 1]
    }
    func has(_ name: String) -> Bool { a.contains(name) }
    func num(_ i: Int) throws -> Double {
        guard i < a.count, let v = Double(a[i]) else { throw CmdError(msg: "expected number at argument \(i + 1)") }
        return v
    }
    var first: String? { a.first }
}

/// NSRunningApplication.activate can be refused for background callers on
/// macOS 14+; the AX frontmost attribute is honored with Accessibility access.
func bringToFront(_ app: NSRunningApplication) {
    let el = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetAttributeValue(el, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    if !app.isActive { app.activate(options: [.activateIgnoringOtherApps]) }
}

func requireApp() throws -> NSRunningApplication {
    guard let app = mirrorApp() else { throw CmdError(msg: "iPhone Mirroring is not running") }
    return app
}

func viewMenu(_ pid: pid_t) -> [String: Bool] {
    var menu: [String: Bool] = [:]
    let root = AXUIElementCreateApplication(pid)
    if let mb = ax(root, kAXMenuBarAttribute) {
        for top in axChildren(mb as! AXUIElement) where (ax(top, kAXTitleAttribute) as? String) == "View" {
            for m in axChildren(top) {
                for item in axChildren(m) {
                    if let t = ax(item, kAXTitleAttribute) as? String, !t.isEmpty {
                        menu[t] = (ax(item, kAXEnabledAttribute) as? Bool) ?? false
                    }
                }
            }
        }
    }
    return menu
}

func handle(_ cmd: String, _ args: Args) throws -> [String: Any] {
    switch cmd {
    case "ping":
        return ["ok": true]

    case "state":
        var out: [String: Any] = [
            "ax_trusted": AXIsProcessTrusted(),
            "screen_access": CGPreflightScreenCaptureAccess(),
            "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "",
        ]
        if let app = mirrorApp() {
            out["running"] = true
            out["pid"] = Int(app.processIdentifier)
            out["active"] = app.isActive
            out["windows"] = appWindows(app.processIdentifier).map { $0.dict }
            out["phone_window"] = phoneWindow(app.processIdentifier)?.dict ?? NSNull()
            out["ax"] = axWindowSummary(app.processIdentifier)
            out["view_menu"] = viewMenu(app.processIdentifier)
        } else {
            out["running"] = false
        }
        return out

    case "launch":
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
            throw CmdError(msg: "iPhone Mirroring.app not found (needs macOS 15+)")
        }
        let cfg = NSWorkspace.OpenConfiguration()
        cfg.activates = !args.has("--background")
        var done = false
        var err: Error?
        NSWorkspace.shared.openApplication(at: url, configuration: cfg) { _, e in err = e; done = true }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        if let err { throw CmdError(msg: "launch failed: \(err.localizedDescription)") }
        return ["ok": true]

    case "activate":
        let app = try requireApp()
        bringToFront(app)
        for _ in 0..<50 where !(mirrorApp()?.isActive ?? false) { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        return ["ok": mirrorApp()?.isActive ?? false]

    case "frontmost":
        let f = NSWorkspace.shared.frontmostApplication
        return ["pid": Int(f?.processIdentifier ?? 0), "bundle": f?.bundleIdentifier ?? ""]

    case "restore":
        let pid = pid_t(try args.num(0))
        if let app = NSRunningApplication(processIdentifier: pid), app.bundleIdentifier != bundleID {
            bringToFront(app)
        }
        return ["ok": true]

    case "menu":
        guard let title = args.first else { throw CmdError(msg: "menu <title>") }
        let r = axPressMenu(try requireApp().processIdentifier, title)
        if !r.found { throw CmdError(msg: "menu item '\(title)' not found") }
        if !r.enabled { throw CmdError(msg: "menu item '\(title)' is disabled (phone not connected?)") }
        return ["ok": r.ok]

    case "button":
        guard let label = args.first else { throw CmdError(msg: "button <label>") }
        return ["ok": axPressButton(try requireApp().processIdentifier, label)]

    case "shot":
        // shot --window ID [--content [--aspect h/w]] [--crop x,y,w,h] [--png out] [--jpg out --width N] [--ocr] [--fast]
        // --content crops to the phone screen first; --crop is then relative to that.
        guard let idStr = args.flag("--window"), let id = UInt32(idStr) else { throw CmdError(msg: "shot --window <id>") }
        var img: CGImage
        do { img = try captureWindow(CGWindowID(id)) } catch { throw CmdError(msg: "capture failed: \(error.localizedDescription)") }
        var out: [String: Any] = ["window_px_width": img.width, "window_px_height": img.height]
        if args.has("--content") {
            let r = contentRect(img, aspect: args.flag("--aspect").flatMap { Double($0) })
            out["content"] = ["x": r.minX, "y": r.minY, "w": r.width, "h": r.height]
            img = img.cropping(to: r) ?? img
        }
        if let c = args.flag("--crop") {
            let p = c.split(separator: ",").compactMap { Double($0) }
            guard p.count == 4 else { throw CmdError(msg: "--crop x,y,w,h (normalized)") }
            img = render(img, crop: CGRect(x: p[0], y: p[1], width: p[2], height: p[3]), width: nil)
        }
        out["width"] = img.width
        out["height"] = img.height
        if let p = args.flag("--png") { try write(img, p, jpeg: false) }
        if let p = args.flag("--jpg") {
            let scaled = render(img, crop: nil, width: args.flag("--width").flatMap { Int($0) })
            try write(scaled, p, jpeg: true)
            out["jpg_width"] = scaled.width
            out["jpg_height"] = scaled.height
        }
        if args.has("--ocr") { out["ocr"] = try ocr(img, fast: args.has("--fast")) }
        return out

    case "ocr":
        guard let path = args.first, let img = loadImage(path) else { throw CmdError(msg: "ocr <image>") }
        return ["width": img.width, "height": img.height, "ocr": try ocr(img, fast: args.has("--fast"))]

    case "click":
        // click x y [--hold ms] [--count n]
        click(CGPoint(x: try args.num(0), y: try args.num(1)), holdMs: args.flag("--hold").flatMap { Double($0) } ?? 60,
              count: args.flag("--count").flatMap { Int($0) } ?? 1)
        return ["ok": true]

    case "drag":
        // drag x1 y1 x2 y2 [--ms dur] [--hold ms]
        drag(CGPoint(x: try args.num(0), y: try args.num(1)), CGPoint(x: try args.num(2), y: try args.num(3)),
             ms: args.flag("--ms").flatMap { Double($0) } ?? 300, holdMs: args.flag("--hold").flatMap { Double($0) } ?? 30)
        return ["ok": true]

    case "scroll":
        // scroll x y dy dx [--steps n]   (pixels)
        scroll(CGPoint(x: try args.num(0), y: try args.num(1)), dx: Int32(try args.num(3)), dy: Int32(try args.num(2)),
               steps: args.flag("--steps").flatMap { Int($0) } ?? 10)
        return ["ok": true]

    case "key":
        // key <name|keycode|char> [--mods cmd,shift]
        guard let k = args.first else { throw CmdError(msg: "key <name>") }
        let flags = parseMods(args.flag("--mods") ?? "")
        if let code = keyNames[k.lowercased()] {
            key(code, flags)
        } else if let code = UInt16(k), k.count > 1 {
            key(code, flags)
        } else if k.count == 1, let (code, shift) = charKeys[Character(k)] {
            key(code, shift ? flags.union(.maskShift) : flags)
        } else {
            throw CmdError(msg: "unknown key '\(k)'")
        }
        return ["ok": true]

    case "type":
        guard let text = args.first else { throw CmdError(msg: "type <text>") }
        let unmapped = typeText(text, delayMs: args.flag("--delay").flatMap { Double($0) } ?? 20)
        return ["ok": true, "unicode_fallback": unmapped]

    case "cursor":
        let p = CGEvent(source: nil)?.location ?? .zero
        return ["x": p.x, "y": p.y]

    case "warp":
        CGWarpMouseCursorPosition(CGPoint(x: try args.num(0), y: try args.num(1)))
        CGAssociateMouseAndMouseCursorPosition(1)
        return ["ok": true]

    case "request-screen-access":
        return ["granted": CGRequestScreenCaptureAccess()]

    default:
        throw CmdError(msg: "unknown command '\(cmd)'")
    }
}

func runOne(_ argv: [String]) -> [String: Any] {
    guard let cmd = argv.first else { return ["error": "empty command"] }
    do {
        return try handle(cmd, Args(Array(argv.dropFirst())))
    } catch let e as CmdError {
        return ["error": e.msg]
    } catch {
        return ["error": "\(error.localizedDescription)"]
    }
}

// MARK: - main

// ScreenCaptureKit asserts without a window-server connection; this opens
// one without giving the helper a Dock icon.
NSApplication.shared.setActivationPolicy(.prohibited)

let argv = Array(CommandLine.arguments.dropFirst())
guard let first = argv.first else {
    fail("usage: mirrorctl serve | <state|launch|activate|frontmost|restore|menu|button|shot|ocr|click|drag|scroll|key|type|cursor|warp|request-screen-access> ...")
}

if first == "serve" {
    // One JSON array of arguments per stdin line, one JSON object per stdout
    // line. Keeps the Vision model warm between calls.
    while let line = readLine() {
        guard let data = line.data(using: .utf8),
              let cmd = (try? JSONSerialization.jsonObject(with: data)) as? [String] else {
            emit(["error": "bad request line"])
            continue
        }
        emit(runOne(cmd))
    }
    exit(0)
}

let result = runOne(argv)
emit(result)
exit(result["error"] == nil ? 0 : 1)
