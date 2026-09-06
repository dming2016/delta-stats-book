# 第三方组件与素材

项目原创代码采用 MIT；以下内容保留各自权利，不由根 LICENSE 重新授权。

## 仓库内分发

| 内容 | 来源 | 许可与说明 |
|---|---|---|
| `web/assets/lucide-0.468.0.min.js` | Lucide 0.468.0 | ISC，部分图标源自 MIT 的 Feather。许可见 `web/assets/lucide-LICENSE.txt` 和 `licenses/Feather-MIT.txt` |
| `installer/ChineseSimplified.isl` | Inno Setup 官方简体中文翻译 | 保留文件头原始归属与说明，项目许可见 `licenses/Inno-Setup.txt` |
| 应用图标及其母版 | 为本项目制作的自定义视觉标识 | 随项目提供使用；不表示获得游戏或腾讯商标权 |
| 地图缩略图 | `tools/generate_map_placeholders.py` | 本项目原创生成的通用位置示意，MIT；不是游戏地图截图 |
| 程序展示截图 | 实际页面 + 虚构账号战绩 + 上述原创占位图 | 本项目生成，不含真实用户信息或提取的游戏美术 |

## 安装时获取的依赖

依赖通过 pip/npm 安装，不把整个运行时复制进源码仓库。以下是直接依赖的许可概览；二进制再分发应同时遵守对应版本及其传递依赖的完整许可。

| 依赖 | 主要许可 |
|---|---|
| requests | Apache-2.0 |
| pycryptodome | BSD-2-Clause / 部分代码公有领域 |
| pywin32 | PSF |
| pywebview | BSD-3-Clause |
| tzdata | Apache-2.0 / IANA 时区数据公有领域 |
| Pillow | MIT-CMU（较早版本曾标为 HPND，具体以安装版本为准） |
| PyInstaller | GPL-2.0-or-later，含用于分发构建产物的例外 |
| Playwright | Apache-2.0 |
| Sharp | Apache-2.0，含需单独留意的 libvips 等依赖 |

Windows、WebView2、微信、《三角洲行动》及其官方接口属于各自权利人。本项目的 MIT 许可证不授予其商标、官方素材或服务使用权。源码不包含提取的微信小程序包、游戏美术、生产密钥或账号数据。
