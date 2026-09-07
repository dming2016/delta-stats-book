# 开发与构建

## 环境

使用 Windows x64、Python 3.12、Node.js 22 LTS。推荐独立虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe friend_client.py
```

源码模式创建回环 HTTP 服务并用 pywebview 打开窗口；不包含正式构建时的更新 bootstrap。

## 测试与预览

```powershell
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe tools/preview_ui.py --port 4178
```

预览根路径为下载官网，`/delta-stats-page.html` 为虚构战绩。它不会读取真实保险箱或调用腾讯接口。不要把预览服务绑定外网。

Windows 测试会跳过两项依赖 POSIX signal/flock 的发布事务测试；GitHub Actions 的独立 Ubuntu job 运行完整 `test_release_delivery`，覆盖这两项，不要求 Windows 开发者另外安装 WSL。

在另一个终端运行浏览器检查：

```powershell
npm ci
node tools/verify_ui.cjs --no-assets
```

脚本使用 Playwright、Sharp 和已安装的 Microsoft Edge。`UI_PREVIEW_URL` 可指定隔离服务地址；脚本会先核对虚构数据标识。截图、控制台检查和多宽度结果保存在 `artifacts/ui-redesign/`。

去掉 `--no-assets` 会更新官网演示截图，必须在构建前完成，不能在发布过程中修改同名素材。生成原创地图占位图：

`node tools/verify_startup.cjs` 使用同一隔离服务（默认 4180，可用 `UI_PREVIEW_URL` 指定），模拟桌面桥接、90 天前缓存及腾讯繁忙，验证浅色、旧缓存可见、启动/手动自动拉起、成功重试、失败不循环、同账号与关闭取消。报告默认写入 `artifacts/light-recovery/`，可用 `UI_REPORT_DIR` 隔离批次。不要只用近期演示数据验收启动后的空列表。

```powershell
.\.venv\Scripts\python.exe tools/generate_map_placeholders.py
```

## 构建

需要 PyInstaller 和 Inno Setup 6.5 或更新版本。Inno 编译器不在默认位置时设置 `INNO_SETUP_COMPILER`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_friend_package.py
```

构建会重建整个 `artifacts/local-package/`。不要在此目录保存手工文件。五件套的名称、大小、SHA-256 以构建生成的 `release/version.json` 为准。

生产二进制与源码包是不同分发物。初始开源标签基于 1.9.0，使用原创地图占位图，并修复英文 Windows 下中文路径的启动器替换问题，不宣称能逐字节重现历史官网下载的 PE 文件。仓库提供手动 `Build Windows Package` 工作流，只构建和上传测试产物，不自动部署生产或创建公开二进制 Release。

## 修改版分发

分发 Fork 前，至少设置独立更新服务：

```powershell
$env:DELTA_UPDATE_BASE_URL = 'https://updates.example.com'
$env:DELTA_UPDATE_ALTERNATE_BASE_URLS = ''
.\.venv\Scripts\python.exe build_friend_package.py
```

还应审查安装 AppId、Mutex、安装目录、数据目录、注册表键和产品名称，避免与上游冲突。不要单独改一个 AppId 就认为已隔离；相关文件见 [更新协议](update-protocol.md)。

## 服务端参考

`deploy/` 是上游 Nginx/systemd 和发布事务实现的参考，含固定域名、Linux 路径及服务名称，不是通用一键部署器。不要在不理解影响范围时以 root 执行。部署自己的实例需要先适配目录、域名、证书和服务账号，不使用上游生产服务器。

可选共享服务必须单独配置认证和反向代理，只在回环地址监听。其配置不在公开仓库。接口和安全边界见 [集成指南](integration-guide.md) 与 [SECURITY.md](../SECURITY.md)。

## 常见问题

更新提示失败时先读 launcher/desktop 日志：候选 ready 后 `current.json.new -> current.json` 的 WinError 5 属于激活文件冲突，不是下载失败。1.9.3 避免候选轮询与旧启动器替换并发，并保留 Windows 短暂冲突重试。若本地 HTTP readiness 超时，新版最多创建三个独立回环端口，每次失败先完整关闭旧服务；日志含端口和最后错误。隔离复现曾观察到同进程 TCP 长时间 `SYN_RECEIVED`，底层系统原因尚未确定，不归咎腾讯或账号。`python tools/diagnose_startup.py --recovery` 可进行 60 次纯静态 HTTP 启动验证，不启动窗口或查询账号。

- 用户反馈旧小程序登录态可能返回 `-108 / 腾讯繁忙`，启动/手动刷新应自动拉起小程序、定向恢复同账号并重试一次。不要只让用户等待；但不能因此把所有繁忙或网络错误持久化为登录过期，历史缓存仍应可看。
- QQ 区也从电脑版微信中的官方小程序读取，不需要电脑版 QQ。
- “打开微信成功”只表示 Windows 接收了协议请求，不证明已经打开指定页面。
- 源码 Windows GUI 测试不应覆盖现有正式安装。安装器即使使用不同 `/DIR`，仍可能共用注册表和开始菜单项。
- 提交日志前脱敏，不公开账号保险箱、原始战绩或 HAR。
