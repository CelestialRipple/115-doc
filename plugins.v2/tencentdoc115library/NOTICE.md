# 第三方项目归属说明

本插件内置 Emby 直链网关的 `MediaSourceId` 识别、通过 Emby
`ItemsService` 查询原始 STRM 媒体项，以及修改播放信息的整体思路参考：

- MediaWarp：https://github.com/AkimioJR/MediaWarp
- Copyright (C) AkimioJR
- License：修改版 GNU Affero General Public License 3，附加非商业使用、
  开源修改及注明来源的要求

ISO 媒体按需触发 Emby OpenStream 探测、为直链使用 HTTP 307 以及保持
MediaSourceId 的流程还参考：

- go-emby2openlist：https://github.com/AmbitiousJun/go-emby2openlist
- Copyright (C) AmbitiousJun
- License：GNU General Public License v3.0

本插件针对 MoviePilot 与腾讯文档 115 分享按需解析场景独立重新实现上述流程，
没有直接复制 Go 源文件；源代码继续在
https://github.com/CelestialRipple/115-doc 公开提供。
