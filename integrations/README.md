# MoviePilot 搜索结果使用浏览器下载

需要插件 **0.13.0 或更新版本**，并开启“使用浏览器下载视频”（默认开启）。只改变“腾讯文档115媒体库”提供的结果；其他 PT/BT 站点照常工作。

插件只解析目录和 115 临时地址，浏览器跟随 HTTP 302 直接连接 115。不会创建 MoviePilot、qBittorrent 或插件本地下载任务。

## 方案一：浏览器脚本，无需更换 MoviePilot 镜像

1. 使用支持用户脚本的浏览器及脚本管理器。
2. 安装本目录的 [moviepilot-browser-download.user.js](moviepilot-browser-download.user.js)。安装时把两条 `@match` 改为自己的 MoviePilot 地址，例如 `https://mp.example.com/*`，删除不使用的那条，避免在无关网站运行。
3. 刷新 MoviePilot，进入搜索页重新搜索一次。脚本仅观察同源搜索返回的数据，不读取登录 Token，不向第三方发送数据。
4. 点击本插件搜索结果的卡片或列表正文，直接打开浏览器下载。电影默认选择最大主视频；电视剧有多个视频时先选择文件。相同标题有多份无法唯一匹配的分享时，会显示选择框，避免下载错资源。
5. `ⓘ` 详情和其他操作按钮保留原行为。详情页也有“用浏览器下载视频”链接。

脚本适配的是 MoviePilot V3 的 `.torrent-card`/`.torrent-item` 结构，以及普通 JSON/SSE 搜索响应。若前端版本改变了这些结构，脚本会拒绝猜测资源；可从 `ⓘ` 详情页下载，或使用下面的源码适配。脚本没有被安装到用户浏览器中；仓库更新本身不会自动安装脚本。

## 方案二：自己维护 MoviePilot 前端

本目录的 [moviepilot-frontend-v3.patch](moviepilot-frontend-v3.patch) 增加一个独立辅助函数，并在卡片、列表和站点资源弹窗点击处理的最前面判断本插件结果。其他结果继续原下载流程，不依赖用户脚本或 DOM 匹配。

补丁已对本地 MoviePilot-Frontend 提交 `79ab32b0d3bef1120cb8f48c296d2bf41c6ef089` 检查可应用：

```bash
git apply --check /path/to/115-doc/integrations/moviepilot-frontend-v3.patch
git apply /path/to/115-doc/integrations/moviepilot-frontend-v3.patch
```

然后按该前端版本原有流程构建和部署。其他版本可参考 [moviepilotBrowserDownload.ts](moviepilotBrowserDownload.ts) 的调用方式手动接入；这里只提供补丁，不向上游前端仓库推送或替换用户镜像。

## 不安装任何适配

仍可点击搜索结果的 `ⓘ`，再点击“用浏览器下载视频”。原生卡片的点击动作由 MoviePilot 前端控制，插件后端无法将其 AJAX 下载请求变成浏览器导航。浏览器模式开启时，旧前端请求会得到明确提示，不会悄悄创建下载任务。

## 下载行为边界

- 分享必须可访问，并配置有效的 115 Cookie。直链按当前浏览器 User-Agent 签发。
- 磁力/ED2K 仍然需要先由 **115 云端离线**生成文件，再向浏览器签发直链；不是让浏览器直接下载磁力协议。NAS 不下载视频正文。
- 302 不能替 115 修改最终响应的 `Content-Disposition`。某些视频可能先在浏览器播放，此时使用浏览器“另存为”；严格强制另存为需要上游提供 attachment 响应或代理视频正文，本插件不代理正文。
- 操作链接按用途和资源签名，有效期一小时；过期后重新搜索。网关播放链接签名有效期十分钟，重启后失效。
- 浏览器下载完成状态由浏览器管理，不会进入 MoviePilot 下载器的进度、历史或自动整理生命周期。
