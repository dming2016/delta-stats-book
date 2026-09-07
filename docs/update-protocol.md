# 更新与启动协议

## 稳定身份

显示品牌已经是“三角洲战绩本”，但以下技术身份用于兼容旧客户端，不能随品牌重命名：

- 外层启动器：`三角洲情报助手.exe`
- App 入口：`DeltaStatsApp.exe`
- 安装目录：`%LOCALAPPDATA%\Programs\DeltaStatsAssistant`
- 数据目录：`%LOCALAPPDATA%\DeltaForceIntelAssistant`
- Inno AppId：`{F0BB66C0-D6D8-49C2-9C42-8B7CA6AC291B}`
- 安装状态注册表：`HKCU\Software\DeltaStatsAssistant`

## 当前协议值

| 项目 | 当前值 | 权威代码 |
|---|---|---|
| App pointer schema | `1` | `update_protocol.py` |
| pending schema | `1` | `app_updater.py` / `launcher.py` |
| package format | `pyinstaller-onedir-zip-v1` | `update_protocol.py` |
| launcher protocol | `2` | `app_version.py` |

两个发布脚本会先获取同一个生产发布锁，再读取生产清单、校验 launcher protocol、package format 和版本迁移；并发发布会明确失败，不会交错比较或写入。低版本清单会被拒绝；同版本只允许五件套名称、大小和 SHA-256 全部相同的幂等发布。无论 App 是否升级，只要候选文件名已存在于生产发布目录，其大小和 SHA-256 就必须与候选清单一致；这也覆盖 App 升级但复用独立 Launcher 版本的情况，禁止覆盖已经进入 immutable 缓存的版本化文件。备份和 staging 就绪后，脚本会在命令错误或 `INT`、`TERM`、`HUP` 中断时只执行一次回滚，并保留原错误或常规信号退出码。升级任一协议时，必须同时修改代码、发布脚本、清单构建和交付测试；不能只改客户端。

## 安装布局

```text
<install-root>/
  三角洲情报助手.exe
  app/
    current.json
    previous.json
    pending.json                 # 仅更新切换期间存在
    versions/<version>/
      DeltaStatsApp.exe
      _internal/
  .updates/
```

`current.json` 和 `previous.json` 都是 `{"schema":1,"version":"x.y.z"}`。非对象 JSON、未知 schema、非法版本号和损坏文件会被当作无效指针忽略，不会让启动器因类型错误崩溃。

安装版和便携版运行同一套 versioned-onedir App。安装器只负责首次安装、开始菜单/桌面入口、卸载注册和初始版本；应用内更新不会重新运行 Inno Setup，而是写入新版本目录并切换 pointer。

## 检查与下载

页面加载完成后延迟触发版本检查，不阻塞外层窗口出现。自动检查失败保持安静；发现新版后由用户确认下载。当前生产清单包含五件套元数据、launcher protocol、package format、分组 `release_history` 和兼容旧客户端的扁平 `release_notes`。新版更新所需的包和协议字段是硬契约；客户端仍兼容缺少 `release_history` 或 `release_notes` 的旧清单，只是不展示相应更新说明。

AppDir ZIP 使用稳定 `.part` 文件和 4 MiB 顺序 Range。连续 12 秒无进展时从已有字节重新连接，最多重连 4 次；取消后有效 `.part` 可继续复用。完成后以文件大小和 SHA-256 为最终判断。

下载和解压在 App 内完成。校验通过后写 `app/pending.json`，再由外层启动器等待旧进程退出并完成激活。

## 激活与回退

1. 启动器验证 pending 路径、schema、package format、目标版本、入口和暂存启动器哈希。
2. 等待旧 App 退出，并确认全局实例锁已经释放。
3. 启动候选 App，要求页面就绪握手。
4. 候选成功后切换 `current.json`；只有此前运行的是不同版本的 versioned-onedir App 时才写 `previous.json`，旧 onefile 迁移不会生成该指针。随后同步安装版的 `DisplayVersion` 与 `InstalledVersion`。
5. 候选失败时先确认失败进程树已经停止，再启动上一版本；如果无法确认停止，不会同时拉起旧版。

正常打开程序时，当前 pointer 指向的版本启动失败也会尝试 `previous.json`。回退成功后，启动器会把 `current.json` 和安装版注册表版本同步回实际运行版本。便携版、安装目录不匹配或注册表不可写时，元数据同步会安全跳过。

候选 App 在 `pending.json` 仍存在时不得打开 `current.json` 轮询安装元数据：Windows 读取句柄会与旧启动器的一次性原子替换冲突，造成候选已就绪却以 WinError 5 回退。1.9.3 的 App 等 pending 删除后才核实 current，因此兼容旧启动器；新启动器同时对 Windows 5/32/33 的短暂替换冲突有限重试两秒，不删除旧指针、不改 ACL。schema 和协议不变。

## 旧客户端兼容

生产仍提供旧 onefile App、旧下载 ZIP/安装器兼容跳转和扁平 `release_notes`，以便早期客户端迁移到当前布局。`updater_ui.py` 没有被当前源码链调用；它代表已发布旧客户端曾使用的独立窗口，不应被当作当前更新入口。

持有 `1.5.0` 之前启动器的用户需要重新运行安装器或重新下载完整便携 ZIP，才能获得启动器自更新能力。

## 已知限制

- 启动器自身替换通过异步命令重试完成。App 激活成功后 pending 已删除；如果启动器替换最终失败，目前只有日志，没有持久 repair marker。
- 初始公开源码将启动器替换助手改为隐藏 PowerShell 进程，路径以 Unicode 环境变量传入并使用 LiteralPath，修复英文 Windows 上 ANSI 批处理无法表达中文目录的问题；只有当前安装 `.updates` 中的候选可以进入该助手。此源码修复没有回写已发布的历史 1.9.0 二进制。
- 同版本且 App 布局正常时，更新检查不会重新比较或修复根启动器。只有下一次 App 版本更新时才会因哈希不匹配再次尝试。
- `1.8.3` 起至当前版本的 launcher protocol 都是 `2`，旧启动器仍能启动新版 App；但在协议升级前必须解决上面的持久修复问题。
- 外层启动器仍是 PyInstaller onefile，冷启动可能受解包和 Defender 扫描影响。消除这类波动需要 native 或 onedir 启动器，不是页面加载优化能够解决的。

## 协议变更检查

修改协议前至少核对：

- `app_version.py`
- `update_protocol.py`
- `app_updater.py`
- `launcher.py`
- `install_metadata.py`
- `build_friend_package.py`
- `installer/DeltaStatsAssistant.iss`
- `deploy/publish_release.sh`
- `deploy/publish_download_site.sh`
- `test_app_updater.py`、`test_launcher.py`、`test_release_delivery.py`
