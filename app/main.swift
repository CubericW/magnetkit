// MagnetKit.app —— 原生外壳：一个窗口 + WKWebView，加载 magnet.py 提供的本地界面（127.0.0.1，带令牌）。
// 负责：接管 magnet: 链接和 .torrent 文件、菜单与快捷键、需要时拉起后台服务、原生通知（点击跳到对应页面）。
// 编译：swiftc -O -swift-version 5 -o MagnetKit main.swift（安装脚本会自动完成）
import AppKit
import UniformTypeIdentifiers
import UserNotifications
import WebKit

let home = FileManager.default.homeDirectoryForCurrentUser
let stateDir = home.appendingPathComponent("Library/Application Support/MagnetKit")
let agentLabels = ["com.magnetkit.rqbit", "com.magnetkit.watcher"]

struct UIInfo {
    let port: Int
    let token: String
}

/// 后台服务写的 ui.json（权限 600）：界面端口和访问令牌
func readUIInfo() -> UIInfo? {
    guard let data = try? Data(contentsOf: stateDir.appendingPathComponent("ui.json")),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let port = obj["port"] as? Int, let token = obj["token"] as? String else { return nil }
    return UIInfo(port: port, token: token)
}

/// 把任意字符串安全地变成 JS 字符串字面量
func jsString(_ s: String) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: [s]),
          let arr = String(data: data, encoding: .utf8) else { return "\"\"" }
    return String(arr.dropFirst().dropLast())
}

func tildePath(_ path: String) -> String {
    let h = home.path
    if path == h { return "~" }
    return path.hasPrefix(h + "/") ? "~" + String(path.dropFirst(h.count)) : path
}

/// App 自己的日志：~/Library/Logs/MagnetKit/app.log
func appLog(_ message: String) {
    let dir = home.appendingPathComponent("Library/Logs/MagnetKit")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let file = dir.appendingPathComponent("app.log")
    let fmt = DateFormatter()
    fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
    let line = Data("[\(fmt.string(from: Date()))] \(message)\n".utf8)
    if let h = try? FileHandle(forWritingTo: file) {
        h.seekToEndOfFile()
        h.write(line)
        try? h.close()
    } else {
        try? line.write(to: file)
    }
}

/// 记下通知权限，供「诊断」页显示
func saveNotificationStatus(_ status: UNAuthorizationStatus) {
    let names: [UNAuthorizationStatus: String] = [.authorized: "authorized", .denied: "denied",
                                                  .notDetermined: "notDetermined", .provisional: "provisional"]
    let obj: [String: Any] = ["notifications": names[status] ?? "other", "updated": Date().timeIntervalSince1970]
    if let data = try? JSONSerialization.data(withJSONObject: obj) {
        try? data.write(to: stateDir.appendingPathComponent("app-status.json"))
    }
}

@discardableResult
func run(_ path: String, _ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler, UNUserNotificationCenterDelegate {
    var window: NSWindow?
    var webView: WKWebView?
    var pageReady = false
    var pendingJS: [String] = []
    var windowRequested = false
    var sawNotify = false
    var connectGeneration = 0

    // MARK: - 启动

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSWindow.allowsAutomaticWindowTabbing = false
        UNUserNotificationCenter.current().delegate = self
        buildMenu()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().getNotificationSettings { saveNotificationStatus($0.authorizationStatus) }
        // 稍等片刻再决定是否开窗口：如果只是被后台服务叫起来发通知，就不打扰用户，发完自动退出
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            if self.sawNotify && !self.windowRequested {
                DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                    if self.window == nil { NSApp.terminate(nil) }
                }
            } else {
                self.showMainWindow()
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    // 关掉窗口就退出 App；下载由后台服务继续
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: - 打开磁力链接 / 种子文件

    func application(_ application: NSApplication, open urls: [URL]) {
        var needWindow = false
        for url in urls {
            if url.scheme == "magnetkit" {
                handleInternal(url)
                continue
            }
            needWindow = true
            if url.isFileURL {
                openTorrentFile(url)
            } else {
                callJS("MK.openSource(\(jsString(url.absoluteString)))")
            }
        }
        if needWindow { showMainWindow() }
    }

    func handleInternal(_ url: URL) {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ key: String) -> String { items.first { $0.name == key }?.value ?? "" }
        switch url.host ?? "" {
        case "notify":
            sawNotify = true
            // 只接受带正确令牌的请求，网页里的 magnetkit:// 链接没法冒充
            guard let info = readUIInfo(), !info.token.isEmpty, value("token") == info.token else { return }
            postNotification(title: value("title"), subtitle: value("subtitle"), body: value("body"), view: value("view"))
        case "show":
            showMainWindow()
            let view = value("view")
            if !view.isEmpty { callJS("MK.go(\(jsString(view)))") }
        default:
            break
        }
    }

    /// 种子文件由 App 读取（用户打开的文件 App 有权限读），再把内容交给界面
    func openTorrentFile(_ url: URL) {
        guard let data = try? Data(contentsOf: url) else {
            callJS("MK.toast(\(jsString("读不了文件：" + url.lastPathComponent)), 'error')")
            return
        }
        callJS("MK.openTorrentData(\(jsString(url.lastPathComponent)), \(jsString(data.base64EncodedString())))")
    }

    func callJS(_ js: String) {
        if pageReady, let wv = webView {
            wv.evaluateJavaScript(js, completionHandler: nil)
        } else {
            pendingJS.append(js)
        }
    }

    // MARK: - 窗口

    func showMainWindow() {
        windowRequested = true
        if window == nil {
            buildWindow()
            connect()
        }
        window?.makeKeyAndOrderFront(nil)
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "native")
        let wv = WKWebView(frame: .zero, configuration: config)
        wv.navigationDelegate = self
        wv.uiDelegate = self
        wv.setValue(false, forKey: "drawsBackground")   // 页面加载前透出窗口底色，避免深色模式下闪白
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 660),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "MagnetKit"
        win.minSize = NSSize(width: 760, height: 480)
        win.contentView = wv
        win.isReleasedWhenClosed = false
        win.center()
        win.setFrameAutosaveName("MagnetKitMain")
        window = win
        webView = wv
    }

    // MARK: - 连接本地界面

    func connect(attempt: Int = 0) {
        if attempt == 0 { connectGeneration += 1 }
        let gen = connectGeneration
        guard let info = readUIInfo() else {
            retry(attempt, gen)
            return
        }
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(info.port)/api/ping")!)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                guard gen == self.connectGeneration else { return }
                if ok {
                    self.pageReady = false
                    // 带上时间戳：否则和当前页面只差 # 后面的部分，WebKit 只会页内跳转而不重新加载
                    let stamp = Int(Date().timeIntervalSince1970 * 1000)
                    let url = URL(string: "http://127.0.0.1:\(info.port)/?v=\(stamp)#token=\(info.token)")!
                    self.webView?.load(URLRequest(url: url))
                } else {
                    self.retry(attempt, gen)
                }
            }
        }.resume()
    }

    func retry(_ attempt: Int, _ gen: Int) {
        if attempt == 0 {
            showStatus("正在启动后台服务…", retry: false)
            DispatchQueue.global().async { self.startAgents() }
        }
        if attempt >= 40 {
            showStatus("后台服务没有启动。可以在终端运行 magnet doctor 查看原因。", retry: true)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            guard gen == self.connectGeneration else { return }
            self.connect(attempt: attempt + 1)
        }
    }

    func startAgents() {
        let uid = getuid()
        for label in agentLabels {
            let plist = home.appendingPathComponent("Library/LaunchAgents/\(label).plist").path
            run("/bin/launchctl", ["bootstrap", "gui/\(uid)", plist])
            run("/bin/launchctl", ["kickstart", "gui/\(uid)/\(label)"])
        }
    }

    func showStatus(_ text: String, retry: Bool) {
        let button = retry
            ? "<p><button onclick=\"window.webkit.messageHandlers.native.postMessage({cmd:'reconnect'})\">重试</button></p>"
            : ""
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><style>
        body{margin:0;height:100vh;display:grid;place-items:center;text-align:center;color:#86868b;
        font:13px -apple-system,"PingFang SC",sans-serif}
        button{font:inherit;padding:4px 14px;border-radius:7px;border:1px solid rgba(128,128,128,.4);
        background:transparent;color:inherit}
        </style></head><body><div><p>\(text)</p>\(button)</div></body></html>
        """
        pageReady = false
        webView?.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView.url?.host == "127.0.0.1" else { return }
        markPageReady()
    }

    /// 页面可以接收指令了：把排队的操作（打开的链接 / 种子等）交给它
    func markPageReady() {
        pageReady = true
        let queued = pendingJS
        pendingJS = []
        for js in queued { webView?.evaluateJavaScript(js, completionHandler: nil) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showStatus("连不上后台服务：\(error.localizedDescription)", retry: true)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // 界面里的外部链接交给默认浏览器
        if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url,
           url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        pageReady = false
        connect()
    }

    // MARK: - WKUIDelegate

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        if let t = UTType(filenameExtension: "torrent") { panel.allowedContentTypes = [t] }
        guard let win = window else {
            completionHandler(nil)
            return
        }
        panel.beginSheetModal(for: win) { resp in completionHandler(resp == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    // MARK: - 页面发来的消息

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        let id = body["id"] as? String ?? ""
        switch cmd {
        case "ready":
            if webView?.url?.host == "127.0.0.1" { markPageReady() }
        case "reconnect":
            connect()
        case "chooseFolder":
            guard let win = window else { return }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.prompt = "选择"
            if let start = body["start"] as? String, !start.isEmpty {
                panel.directoryURL = URL(fileURLWithPath: (start as NSString).expandingTildeInPath)
            }
            panel.beginSheetModal(for: win) { resp in
                let path = resp == .OK ? tildePath(panel.url?.path ?? "") : ""
                self.webView?.evaluateJavaScript("MK.nativeResult(\(jsString(id)), \(jsString(path)))", completionHandler: nil)
            }
        default:
            break
        }
    }

    // MARK: - 菜单

    @objc func menuAddMagnet() {
        showMainWindow()
        callJS("MK.focusAdd()")
    }

    @objc func menuOpenTorrent() {
        showMainWindow()
        guard let win = window else { return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        if let t = UTType(filenameExtension: "torrent") { panel.allowedContentTypes = [t] }
        panel.beginSheetModal(for: win) { resp in
            guard resp == .OK else { return }
            for url in panel.urls { self.openTorrentFile(url) }
        }
    }

    @objc func menuGo(_ sender: NSMenuItem) {
        showMainWindow()
        if let view = sender.representedObject as? String { callJS("MK.go(\(jsString(view)))") }
    }

    @objc func menuReload() {
        pageReady = false
        connect()
    }

    func buildMenu() {
        let main = NSMenu()
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String,
                 _ mods: NSEvent.ModifierFlags = [.command], view: String? = nil) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = mods
            item.representedObject = view
            menu.addItem(item)
        }
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem()
            let menu = NSMenu(title: title)
            item.submenu = menu
            main.addItem(item)
            return menu
        }

        let appMenu = submenu("MagnetKit")
        add(appMenu, "关于 MagnetKit", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), "")
        appMenu.addItem(.separator())
        add(appMenu, "设置…", #selector(menuGo(_:)), ",", view: "settings")
        appMenu.addItem(.separator())
        add(appMenu, "隐藏 MagnetKit", #selector(NSApplication.hide(_:)), "h")
        add(appMenu, "隐藏其他", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        appMenu.addItem(.separator())
        add(appMenu, "退出 MagnetKit", #selector(NSApplication.terminate(_:)), "q")

        let fileMenu = submenu("文件")
        add(fileMenu, "添加磁力链接", #selector(menuAddMagnet), "n")
        add(fileMenu, "打开种子文件…", #selector(menuOpenTorrent), "o")
        fileMenu.addItem(.separator())
        add(fileMenu, "关闭窗口", #selector(NSWindow.performClose(_:)), "w")

        // 没有「编辑」菜单的话，网页里的输入框不能用 ⌘C / ⌘V
        let editMenu = submenu("编辑")
        add(editMenu, "撤销", Selector(("undo:")), "z")
        add(editMenu, "重做", Selector(("redo:")), "z", [.command, .shift])
        editMenu.addItem(.separator())
        add(editMenu, "剪切", #selector(NSText.cut(_:)), "x")
        add(editMenu, "拷贝", #selector(NSText.copy(_:)), "c")
        add(editMenu, "粘贴", #selector(NSText.paste(_:)), "v")
        add(editMenu, "全选", #selector(NSText.selectAll(_:)), "a")

        let viewMenu = submenu("显示")
        add(viewMenu, "下载", #selector(menuGo(_:)), "1", view: "tasks")
        add(viewMenu, "待确认", #selector(menuGo(_:)), "2", view: "pending")
        add(viewMenu, "已完成", #selector(menuGo(_:)), "3", view: "done")
        add(viewMenu, "诊断", #selector(menuGo(_:)), "4", view: "diag")
        viewMenu.addItem(.separator())
        add(viewMenu, "重新载入", #selector(menuReload), "r")

        let windowMenu = submenu("窗口")
        add(windowMenu, "最小化", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(windowMenu, "缩放", #selector(NSWindow.performZoom(_:)), "")
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }

    // MARK: - 通知

    func postNotification(title: String, subtitle: String, body: String, view: String) {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            switch settings.authorizationStatus {
            case .notDetermined:
                center.requestAuthorization(options: [.alert, .sound]) { granted, error in
                    appLog("请求通知权限：\(granted ? "已允许" : "未允许")\(error.map { "（\($0.localizedDescription)）" } ?? "")")
                    center.getNotificationSettings { saveNotificationStatus($0.authorizationStatus) }
                    if granted {
                        self.deliver(title, subtitle, body, view)
                    } else if error != nil {
                        self.fallbackNotify(title, subtitle, body)
                    }
                }
            case .denied:
                saveNotificationStatus(.denied)
                appLog("通知被用户关闭，跳过：\(title)")   // 尊重用户在系统设置里的选择
            default:
                saveNotificationStatus(settings.authorizationStatus)
                self.deliver(title, subtitle, body, view)
            }
        }
    }

    func deliver(_ title: String, _ subtitle: String, _ body: String, _ view: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        if !subtitle.isEmpty { content.subtitle = subtitle }
        content.body = body
        content.sound = .default
        content.userInfo = ["view": view]
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { error in
            if let error = error {
                appLog("原生通知发送失败，改用脚本通知：\(error.localizedDescription)")
                self.fallbackNotify(title, subtitle, body)
            } else {
                appLog("已发送通知：\(title)")
            }
        }
    }

    /// 系统不让这个本地编译的 App 发通知时（出错，而不是用户拒绝），退回 AppleScript 通知
    func fallbackNotify(_ title: String, _ subtitle: String, _ body: String) {
        func quote(_ s: String) -> String {
            let t = s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
                .replacingOccurrences(of: "\n", with: " ")
            return "\"" + t + "\""
        }
        var script = "display notification \(quote(body)) with title \(quote(title))"
        if !subtitle.isEmpty { script += " subtitle \(quote(subtitle))" }
        run("/usr/bin/osascript", ["-e", script])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let view = response.notification.request.content.userInfo["view"] as? String ?? ""
        DispatchQueue.main.async {
            self.showMainWindow()
            if !view.isEmpty { self.callJS("MK.go(\(jsString(view)))") }
        }
        completionHandler()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
