// 色戻し（Untint）
// V-160HD などの外部機器に繋いだあと、画面が黄ばんだまま戻らない状態を、再起動せずに直す。
//  1) Night Shift を一度オン → オフにして、Night Shift の色補正を外させる
//     （CoreBrightness の CBBlueLightClient。Apple 非公開の仕組みなので、使えない macOS では飛ばす）
//  2) 全画面の色の補正値（ガンマ）を、いま設定中のカラープロファイルの値に戻す
//     （CGDisplayRestoreColorSyncSettings。Apple 公開の命令）
import AppKit
import CoreGraphics

private func setNightShift(_ client: NSObject, _ on: Bool) -> Bool {
    let sel = NSSelectorFromString("setEnabled:")
    guard client.responds(to: sel) else { return false }
    typealias SetEnabled = @convention(c) (AnyObject, Selector, Bool) -> Bool
    let fn = unsafeBitCast(client.method(for: sel), to: SetEnabled.self)
    return fn(client, sel, on)
}

/// Night Shift をオン → オフ。結果を日本語で返す
private func resetNightShift() -> String {
    guard let bundle = Bundle(path: "/System/Library/PrivateFrameworks/CoreBrightness.framework"),
          bundle.load(),
          let cls = NSClassFromString("CBBlueLightClient") as? NSObject.Type else {
        return "Night Shift：この macOS では操作できませんでした（色の補正値の戻しだけ行いました）"
    }
    let client = cls.init()
    _ = setNightShift(client, true)
    Thread.sleep(forTimeInterval: 0.5)
    let ok = setNightShift(client, false)
    return ok ? "Night Shift：オフにしました" : "Night Shift：オフにできませんでした（色の補正値の戻しだけ行いました）"
}

let nightShift = resetNightShift()
Thread.sleep(forTimeInterval: 0.3)
CGDisplayRestoreColorSyncSettings()

var count: UInt32 = 0
CGGetActiveDisplayList(0, nil, &count)

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.activate(ignoringOtherApps: true)

let alert = NSAlert()
alert.messageText = "画面の色を戻しました"
alert.informativeText = "\(nightShift)\n画面の色の補正値：\(count) 台の画面すべてで、カラープロファイルの値に戻しました\n\nまだ黄ばんでいる場合は、もう一度開いてください。"
alert.addButton(withTitle: "閉じる")
alert.runModal()
