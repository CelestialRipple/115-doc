# 近期蓝光发行 0.1.1

在插件市场安装并启用“近期蓝光发行”，打开插件详情页的“打开近期蓝光发行”。页面沿用同一地址下的 MoviePilot 登录状态。

- 浏览本月，也可切换最近一年与未来一年内的月份。
- 按 Blu-ray／4K UHD、已发行／即将发行筛选，每页 24 个发行版本。
- 数据来自 [Blu-ray.com 美国发行日历](https://www.blu-ray.com/movies/releasedates.php)，包括新版本、修复版和再版；不是全球发行全集，也不代表影片首映或站点资源上线。
- 当前页按原始片名和影片年份匹配 MoviePilot 已配置的 TMDB，尽量补齐中文名、海报、简介和评分。不会把光盘发行年份当作影片年份。套装、剧集及不确定的同名影片保留原名，可打开发行详情自行核对。
- 只浏览信息，没有自动添加订阅、下载、保存影视库或后台定时任务。

日历缓存 6 小时，手动更新至少间隔 1 分钟。已匹配元数据缓存 30 天，未可靠匹配缓存 1 天，网络失败可重试。来源暂时不可用时保留旧日历并显示提示。来源连接默认沿用 MoviePilot 代理，也可在插件设置中单独填写；TMDB 沿用 MoviePilot 的配置。

## 在订阅页面增加入口

安装仓库的配套静态脚本后，电影／电视剧订阅页面会显示“近期蓝光发行 ↗”按钮，新标签页打开日历：

```bash
python3 integrations/install-browser-adapter.py /path/to/moviepilot/public \
  --script integrations/moviepilot-bluray-calendar.js \
  --asset-name moviepilot-bluray-calendar.js
```

安装器保留已有搜索下载脚本，并备份入口 HTML；不需要重启 MoviePilot。MoviePilot 更新前端后需要重新安装入口脚本。没有安装脚本时仍可从插件详情页打开。

数据接口要求 MoviePilot 登录。页面只将登录凭据发送到同源插件接口，不向发行站点或 TMDB 发送用户凭据。数据源信息与图片归各自提供方；本插件使用 TMDB API，但未经 TMDB 认可或认证。
