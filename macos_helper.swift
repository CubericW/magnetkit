// MagnetKit 安装时用到的 macOS 辅助脚本（swift macos_helper.swift <命令> …）
//   set-default <App 路径>   把 App 设为 magnet: 链接和 .torrent 文件的默认打开方式
//   icon <输出.png>          生成 1024×1024 的 App 图标
import AppKit
import UniformTypeIdentifiers

func waitFor(_ start: (@escaping () -> Void) -> Void) -> Bool {
    var done = false
    start { done = true }
    let deadline = Date().addingTimeInterval(20)
    while !done && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    return done
}

func setDefault(_ appPath: String) {
    let app = URL(fileURLWithPath: appPath)
    let ok1 = waitFor { finish in
        NSWorkspace.shared.setDefaultApplication(at: app, toOpenURLsWithScheme: "magnet") { err in
            print(err.map { "magnet 链接：失败（\($0.localizedDescription)）" } ?? "magnet 链接：已设为 MagnetKit")
            finish()
        }
    }
    if !ok1 { print("magnet 链接：等待超时（如果弹出了系统确认框，请点允许）") }

    guard let type = UTType("org.bittorrent.torrent") ?? UTType(filenameExtension: "torrent") else {
        print(".torrent 文件：找不到对应的文件类型")
        return
    }
    let ok2 = waitFor { finish in
        NSWorkspace.shared.setDefaultApplication(at: app, toOpen: type) { err in
            print(err.map { ".torrent 文件：失败（\($0.localizedDescription)）" } ?? ".torrent 文件：已设为 MagnetKit")
            finish()
        }
    }
    if !ok2 { print(".torrent 文件：等待超时") }
}

func makeIcon(_ out: String) -> Bool {
    let px = 1024
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return false }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let rect = NSRect(x: 100, y: 100, width: 824, height: 824)
    let shape = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
    NSGradient(starting: NSColor(calibratedRed: 0.16, green: 0.42, blue: 0.96, alpha: 1),
               ending: NSColor(calibratedRed: 0.47, green: 0.25, blue: 0.90, alpha: 1))?.draw(in: shape, angle: -90)
    let glyph = "🧲" as NSString
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 520)]
    let size = glyph.size(withAttributes: attrs)
    glyph.draw(at: NSPoint(x: (CGFloat(px) - size.width) / 2, y: (CGFloat(px) - size.height) / 2 - 10),
               withAttributes: attrs)
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else { return false }
    return (try? png.write(to: URL(fileURLWithPath: out))) != nil
}

let args = CommandLine.arguments
switch (args.count > 2 ? args[1] : "") {
case "set-default": setDefault(args[2])
case "icon": exit(makeIcon(args[2]) ? 0 : 1)
default:
    print("用法：swift macos_helper.swift set-default <App 路径> | icon <输出.png>")
    exit(2)
}
