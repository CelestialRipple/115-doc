# 115-doc

MoviePilot V2/V3 插件“腾讯文档115媒体库”。插件可将多个腾讯文档中的 115 分享资源同步到本地目录，使用 MoviePilot 识别和刮削并生成 STRM；播放时按需获取 115 临时直链，不转存影视文件。

## 安装

在 MoviePilot 的“设置 → 插件 → 插件仓库”中添加：

```text
https://github.com/CelestialRipple/115-doc/
```

刷新插件市场并安装“腾讯文档115媒体库”。完整配置、Docker 路径映射和测试方法见[使用说明](docs/tencentdoc115library/README.md)。

## 安全说明

腾讯文档 Token、115 Cookie 和播放密钥只保存在 MoviePilot 的插件配置中，不应提交到本仓库。建议定期更新访问令牌，并为 MoviePilot 管理界面启用安全的访问控制。
