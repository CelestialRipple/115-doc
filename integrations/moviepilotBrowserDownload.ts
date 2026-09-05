/** 只接受本插件、本资源、同源的短时浏览器下载地址。 */
export function openPluginBrowserDownload(torrent?: {
  site_name?: string; enclosure?: string; page_url?: string;
}): boolean {
  try {
    if (['PanSou聚合搜索', 'BT4G网页搜索'].includes(torrent?.site_name || '')) {
      const id = /^pansou:\/\/([a-f0-9]{32})$/.exec(torrent?.enclosure || '')?.[1]
      const detail = new URL(torrent?.page_url || '', location.origin)
      if (!id || detail.origin !== location.origin || detail.pathname !== '/api/v1/plugin/PanSouAggregate/resource/' + id
        || !detail.hash.startsWith('#mp-pansou=')) return false
      const raw = new URLSearchParams(detail.hash.slice('#mp-pansou='.length)).get('url')
      if (!raw) return false
      const target = new URL(raw, location.origin)
      if (target.origin !== location.origin || target.pathname !== '/api/v1/plugin/PanSouAggregate/download/' + id
        || !/^[a-f0-9]{64}$/.test(target.searchParams.get('sig') || '')
        || !/^\d{1,12}$/.test(target.searchParams.get('expires') || '')
        || Number(target.searchParams.get('expires')) * 1000 <= Date.now()) return false
      window.open(target.href, '_blank', 'noopener,noreferrer')
      return true
    }
    if (torrent?.site_name !== '腾讯文档115媒体库'  || !torrent.enclosure || !torrent.page_url) return false
    const marker = new URL(torrent.enclosure)
    const resourceId = marker.searchParams.get('x.td115')
    if (marker.protocol !== 'magnet:' || !resourceId) return false
    const detail = new URL(torrent.page_url, location.origin)
    if (detail.origin !== location.origin || !detail.hash.startsWith('#mp115-browser=')) return false
    const value = new URLSearchParams(detail.hash.slice('#mp115-browser='.length)).get('url')
    if (!value) return false
    const target = new URL(value, location.origin)
    const token = target.searchParams.get('token') || ''
    if (target.origin !== location.origin
      || target.pathname !== '/api/v1/plugin/TencentDoc115Library/resources/browser/' + encodeURIComponent(resourceId)
      || !/^\d{1,12}\.[a-f0-9]{64}$/.test(token)
      || Number(token.split('.')[0]) * 1000 <= Date.now()) return false
    window.open(target.href, '_blank', 'noopener,noreferrer')
    return true
  } catch { return false }
}
