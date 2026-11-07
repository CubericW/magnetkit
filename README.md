# MagnetKit

macOS 上的磁力链接 / 种子下载工具：先看文件列表、确认后再下载，支持边下边播。
下载引擎是开源的 [rqbit](https://github.com/ikatson/rqbit)，MagnetKit 通过它的 HTTP API
提供命令行、后台服务和一个原生外壳的图形界面。

*A magnet-link / torrent client for macOS built on top of [rqbit](https://github.com/ikatson/rqbit):
inspect files before downloading, confirm, then download or stream. CLI + launchd services + a small
native GUI. Python standard library only.*

## 功能

- **先确认再下载**：解析磁力链接或 `.torrent`，列出文件和大小；任何下载开始前都要确认，可以只选部分文件
- **边下边播**：用 IINA / mpv / VLC 打开正在下载的视频
- **图形界面**：`MagnetKit.app`，并注册为磁力链接和 `.torrent` 文件的默认打开方式
- **后台监控**：完成通知、按分享率/时长做种后自动移除、清理未选文件的残缺块、后台解析（解析好后等待确认）、自动更新公共 tracker
- **本地安全**：rqbit API 和界面 API 只监听 `127.0.0.1`，界面 API 需要令牌；配置文件权限 600
- **零依赖**：后端只用 Python 3.9+ 标准库

## 安装

需要 macOS、Homebrew、Xcode 命令行工具（`xcode-select --install`，用来编译 App）。

```bash
git clone https://github.com/CubericW/magnetkit
cd magnetkit
./install.sh
```

`install.sh` 会：安装 rqbit（若没有）→ 把程序复制到 `~/.local/share/magnetkit` →
链接 `~/.local/bin/magnet` → 写配置、注册两个 LaunchAgent（下载服务、后台监控）→
编译 `~/Applications/MagnetKit.app` 并设为默认打开方式 → 启动服务。

默认下载目录 `~/Magnet`（并在 `~/Downloads/Magnet` 放一个快捷方式）。

## 使用

日常直接打开 MagnetKit，或点击网页上的磁力链接。命令行：

```bash
magnet info  'magnet:?xt=urn:btih:…'     # 只看文件列表，不下载
magnet add   'magnet:?xt=urn:btih:…'     # 选择文件，确认后下载
magnet add   'magnet:…' -s video -y       # 只下视频（-y 表示已确认）
magnet play  'magnet:…'                   # 边下边播最大的视频（会先确认）
magnet ls                                 # 任务列表
magnet pause|resume|rm <ID|名称|all>
magnet config                             # 查看配置；config set <键> <值> 修改
magnet service status|logs|restart
magnet doctor                             # 自检（--net 附带联网解析测试）
```

`magnet --help` 和 `magnet <命令> --help` 查看全部选项。

## 文件位置

| 内容 | 路径 |
|---|---|
| 程序 | `~/.local/share/magnetkit/` |
| 配置（含 API 密码，权限 600） | `~/.config/magnetkit/config.json` |
| 状态、元数据缓存、tracker 列表 | `~/Library/Application Support/MagnetKit/` |
| 日志 | `~/Library/Logs/MagnetKit/` |

设置环境变量 `MAGNETKIT_HOME=<目录>` 可以把配置、状态、日志都放进一个目录（测试或多实例用）。

## 卸载

```bash
./uninstall.sh          # 移除后台服务、App 和命令链接，保留配置和已下载的文件
./uninstall.sh --purge  # 连配置、缓存和日志一起删除
```

## 声明

请只下载你有权获取的内容，并遵守所在地法律。默认关闭 UPnP；做种策略可在配置中调整。

## License

MIT — see [LICENSE](LICENSE). rqbit is a separate project under its own license
(Apache-2.0) and is installed via Homebrew, not bundled here.
