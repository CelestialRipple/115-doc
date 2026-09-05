import json
import re
from threading import Lock
from time import sleep, time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlsplit

try:
    from app.sdk.config import settings
    from app.sdk.logging import logger
    from app.sdk.network import RequestUtils
except ImportError:
    from app.core.config import settings
    from app.log import logger
    from app.utils.http import RequestUtils


TENCENT_DOCS_BASE_URL = "https://docs.qq.com"
TENCENT_DOCS_MCP_URL = "https://docs.qq.com/openapi/mcp"
MAX_RANGE_ROWS = 1000
MAX_RANGE_CELLS = 10000
MAX_RANGE_COLUMNS = 200


class TencentDocumentError(RuntimeError):
    """腾讯文档 API 请求或响应错误"""


class TencentDocumentClient:
    """
    腾讯文档在线表格 V3 客户端

    客户端只读取工作表信息和单元格范围，并在令牌失效时使用 Refresh Token 刷新
    """

    def __init__(
        self,
        client_id: str,
        open_id: str,
        access_token: str,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        access_token_expires_at: Optional[float] = None,
        timeout: int = 30,
        retry_count: int = 3,
        on_token_refresh: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        初始化腾讯文档客户端

        :param client_id (str): 腾讯文档应用 Client ID
        :param open_id (str): 用户 Open ID
        :param access_token (str): 用户访问令牌
        :param client_secret (str): 应用 Client Secret
        :param refresh_token (str): 用户刷新令牌
        :param access_token_expires_at (float): Access Token 过期时间戳
        :param timeout (int): 单次请求超时秒数
        :param retry_count (int): 限流或服务异常最大重试次数
        :param on_token_refresh (Callable): 令牌刷新后的持久化回调
        """
        self.client_id = str(client_id or "").strip()
        self.open_id = str(open_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.access_token_expires_at = float(access_token_expires_at or 0)
        self.retry_count = max(int(retry_count or 0), 0)
        self.on_token_refresh = on_token_refresh
        self._request = RequestUtils(
            proxies=settings.PROXY,
            timeout=max(int(timeout or 30), 5),
        )

    @staticmethod
    def extract_encoded_id(document_url: str) -> str:
        """
        从腾讯文档链接提取 encodedID

        :param document_url (str): 腾讯文档链接或 encodedID

        :return str: encodedID
        """
        value = str(document_url or "").strip()
        if not value:
            raise TencentDocumentError("未配置腾讯文档地址")
        if "/" not in value:
            return value
        path_parts = [part for part in urlsplit(value).path.split("/") if part]
        if len(path_parts) < 2 or path_parts[-2] not in {"sheet", "doc"}:
            raise TencentDocumentError("无法从腾讯文档地址提取 encodedID")
        return path_parts[-1]

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Access-Token": self.access_token,
            "Client-Id": self.client_id,
            "Open-Id": self.open_id,
        }

    def _ensure_configured(self) -> None:
        missing = []
        if not self.client_id:
            missing.append("Client ID")
        if not self.open_id:
            missing.append("Open ID")
        if not self.access_token:
            missing.append("Access Token")
        if missing:
            raise TencentDocumentError(f"缺少腾讯文档配置：{', '.join(missing)}")

    @staticmethod
    def _payload_error(payload: Dict[str, Any]) -> Optional[str]:
        code = payload.get("ret")
        if code is None:
            code = payload.get("code")
        if code in (None, 0, "0"):
            return None
        return str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or f"业务错误码 {code}"
        )

    def _refresh_access_token(self) -> None:
        if not self.client_secret or not self.refresh_token:
            raise TencentDocumentError(
                "Access Token 已失效，且未配置 Client Secret 和 Refresh Token"
            )
        response = self._request.get_res(
            url=f"{TENCENT_DOCS_BASE_URL}/oauth/v2/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            headers={"Accept": "application/json"},
        )
        if response is None:
            raise TencentDocumentError("刷新腾讯文档 Access Token 失败：无响应")
        try:
            payload = response.json()
            if response.status_code != 200:
                raise TencentDocumentError(
                    f"刷新腾讯文档 Access Token 失败：HTTP {response.status_code}"
                )
        finally:
            response.close()
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise TencentDocumentError(
                str(payload.get("error_description") or "刷新响应缺少 Access Token")
            )
        expires_in = max(int(payload.get("expires_in") or 0), 0)
        self.access_token = access_token
        self.access_token_expires_at = time() + expires_in if expires_in else 0
        refreshed_open_id = (
            payload.get("open_id") or payload.get("openid") or payload.get("user_id")
        )
        if refreshed_open_id:
            self.open_id = str(refreshed_open_id)
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"])
        refreshed = {
            "access_token": self.access_token,
            "access_token_expires_at": self.access_token_expires_at,
            "open_id": self.open_id,
            "refresh_token": self.refresh_token,
        }
        if self.on_token_refresh:
            self.on_token_refresh(refreshed)
        logger.info("腾讯文档 Access Token 已自动刷新")

    def _request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        allow_refresh: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_configured()
        if (
            allow_refresh
            and self.access_token_expires_at
            and self.access_token_expires_at - time() < 300
        ):
            self._refresh_access_token()
        last_error = "腾讯文档 API 请求失败"
        for attempt in range(self.retry_count + 1):
            response = self._request.get_res(
                url=url,
                params=params,
                headers=self._headers(),
            )
            if response is None:
                last_error = "腾讯文档 API 无响应"
            else:
                try:
                    status_code = response.status_code
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise TencentDocumentError(
                            f"腾讯文档 API 返回了无效 JSON：HTTP {status_code}"
                        ) from error
                finally:
                    response.close()
                if status_code == 401 and allow_refresh:
                    self._refresh_access_token()
                    return self._request_json(url, params, allow_refresh=False)
                if status_code == 429:
                    last_error = "腾讯文档 API 请求过于频繁"
                elif status_code >= 500:
                    last_error = f"腾讯文档 API 服务异常：HTTP {status_code}"
                elif status_code != 200:
                    message = self._payload_error(payload) or str(
                        payload.get("message") or payload.get("msg") or "请求失败"
                    )
                    raise TencentDocumentError(
                        f"腾讯文档 API 请求失败：HTTP {status_code}，{message}"
                    )
                else:
                    payload_error = self._payload_error(payload)
                    if payload_error:
                        business_code = str(
                            payload.get("ret")
                            if payload.get("ret") is not None
                            else payload.get("code")
                        )
                        if allow_refresh and business_code in {
                            "37019",
                            "37020",
                            "37021",
                        }:
                            self._refresh_access_token()
                            return self._request_json(
                                url,
                                params,
                                allow_refresh=False,
                            )
                        raise TencentDocumentError(
                            f"腾讯文档 API 返回错误：{payload_error}"
                        )
                    return payload
            if attempt < self.retry_count:
                sleep(min(2**attempt, 8))
        raise TencentDocumentError(last_error)

    @staticmethod
    def _unwrap_data(payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def convert_file_id(self, document_url: str) -> str:
        """
        把文档链接中的 encodedID 转换为 Open API fileID

        :param document_url (str): 腾讯文档链接或 encodedID

        :return str: Open API fileID
        """
        encoded_id = self.extract_encoded_id(document_url)
        payload = self._request_json(
            url=f"{TENCENT_DOCS_BASE_URL}/openapi/drive/v2/util/converter",
            params={"type": 2, "value": encoded_id},
        )
        file_id = str(self._unwrap_data(payload).get("fileID") or "").strip()
        if not file_id:
            raise TencentDocumentError("腾讯文档 fileID 转换响应缺少 fileID")
        return file_id

    @staticmethod
    def _property_int(item: Dict[str, Any], *names: str) -> int:
        for name in names:
            value = item.get(name)
            if value is None and isinstance(item.get("gridProperties"), dict):
                value = item["gridProperties"].get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def get_sheets(self, file_id: str) -> List[Dict[str, Any]]:
        """
        查询在线表格中的所有工作表信息

        :param file_id (str): Open API fileID

        :return List: 标准化工作表信息列表
        """
        payload = self._request_json(
            url=f"{TENCENT_DOCS_BASE_URL}/openapi/spreadsheet/v3/files/{quote(file_id, safe='$')}",
            params={"concise": 0},
        )
        data = self._unwrap_data(payload)
        properties = data.get("properties") or payload.get("properties") or []
        sheets = []
        for item in properties:
            if not isinstance(item, dict):
                continue
            sheet_id = str(item.get("sheetId") or item.get("sheetID") or "").strip()
            title = str(item.get("title") or item.get("name") or "").strip()
            if not sheet_id or not title:
                continue
            row_count = self._property_int(item, "rowTotal", "rowCount")
            column_count = self._property_int(item, "columnTotal", "columnCount")
            used_row_count = self._property_int(
                item,
                "usedRowCount",
                "rowCount",
                "rowTotal",
            )
            used_column_count = self._property_int(
                item,
                "usedColumnCount",
                "columnCount",
                "columnTotal",
            )
            sheets.append(
                {
                    "sheet_id": sheet_id,
                    "title": title,
                    "row_count": row_count,
                    "column_count": column_count,
                    "used_row_count": used_row_count,
                    "used_column_count": used_column_count,
                }
            )
        if not sheets:
            raise TencentDocumentError("腾讯文档响应中没有可用工作表")
        return sheets

    @staticmethod
    def column_name(index: int) -> str:
        """
        把从一开始的列序号转换为 A1 列名

        :param index (int): 从一开始的列序号

        :return str: A1 列名
        """
        if index < 1:
            raise ValueError("列序号必须大于零")
        result = ""
        current = index
        while current:
            current, remainder = divmod(current - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @classmethod
    def safe_page_rows(cls, requested_rows: int, column_count: int) -> int:
        """
        根据腾讯文档限制计算安全分页行数

        :param requested_rows (int): 用户期望分页行数
        :param column_count (int): 查询列数

        :return int: 安全分页行数
        """
        safe_columns = min(max(int(column_count or 1), 1), MAX_RANGE_COLUMNS)
        cell_limited_rows = max(MAX_RANGE_CELLS // safe_columns, 1)
        return min(max(int(requested_rows or 1), 1), MAX_RANGE_ROWS, cell_limited_rows)

    @staticmethod
    def _extract_scalar(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            for item in value:
                scalar = TencentDocumentClient._extract_scalar(item)
                if scalar:
                    return scalar
            return ""
        if isinstance(value, dict):
            preferred_keys = (
                "url",
                "link",
                "text",
                "stringValue",
                "numberValue",
                "value",
                "formattedValue",
            )
            for key in preferred_keys:
                if key in value:
                    scalar = TencentDocumentClient._extract_scalar(value[key])
                    if scalar:
                        return scalar
            for nested in value.values():
                scalar = TencentDocumentClient._extract_scalar(nested)
                if scalar:
                    return scalar
        return ""

    @classmethod
    def _cell_text(cls, cell: Dict[str, Any]) -> str:
        cell_value = cell.get("cellValue")
        value = cls._extract_scalar(cell_value)
        if value:
            return value
        return cls._extract_scalar(cell)

    def get_range(
        self,
        file_id: str,
        sheet_id: str,
        start_row: int,
        row_count: int,
        column_count: int,
    ) -> Dict[str, Any]:
        """
        获取一个符合腾讯文档限制的 A1 范围

        :param file_id (str): Open API fileID
        :param sheet_id (str): 工作表 ID
        :param start_row (int): 从一开始的起始行
        :param row_count (int): 请求行数
        :param column_count (int): 请求列数

        :return Dict: 标准化行数据、起始行和原始返回行数
        """
        safe_columns = min(max(int(column_count or 1), 1), MAX_RANGE_COLUMNS)
        safe_rows = self.safe_page_rows(row_count, safe_columns)
        end_row = start_row + safe_rows - 1
        range_name = f"A{start_row}:{self.column_name(safe_columns)}{end_row}"
        payload = self._request_json(
            url=(
                f"{TENCENT_DOCS_BASE_URL}/openapi/spreadsheet/v3/files/"
                f"{quote(file_id, safe='$')}/{quote(sheet_id, safe='')}/"
                f"{quote(range_name, safe=':')}"
            )
        )
        data = self._unwrap_data(payload)
        grid_data = data.get("gridData") or payload.get("gridData") or {}
        raw_rows = grid_data.get("rows") or []
        rows = []
        for row in raw_rows:
            values = row.get("values") if isinstance(row, dict) else None
            rows.append(
                [
                    self._cell_text(cell if isinstance(cell, dict) else {})
                    for cell in (values or [])
                ]
            )
        return {
            "rows": rows,
            "start_row": int(grid_data.get("startRow") or start_row - 1) + 1,
            "requested_rows": safe_rows,
            "raw_row_count": len(raw_rows),
        }


class TencentDocumentMcpClient:
    """腾讯文档 MCP 客户端，用于发现和分页读取智能表格。"""

    def __init__(
        self,
        token: str,
        timeout: int = 30,
        retry_count: int = 3,
    ):
        self.token = str(token or "").strip()
        self.retry_count = max(int(retry_count or 0), 0)
        self._request = RequestUtils(
            proxies=settings.PROXY,
            timeout=max(int(timeout or 30), 5),
        )
        self._request_id = 0
        self._lock = Lock()

    @staticmethod
    def document_context(document_url: str) -> Dict[str, str]:
        """提取 encodedID，以及链接中仅对当前子表有效的 tab/viewId。"""
        value = str(document_url or "").strip()
        encoded_id = TencentDocumentClient.extract_encoded_id(value)
        query = parse_qs(urlsplit(value).query) if "/" in value else {}
        return {
            "file_id": encoded_id,
            "sheet_id": str((query.get("tab") or [""])[0]).strip(),
            "view_id": str((query.get("viewId") or [""])[0]).strip(),
        }

    def _ensure_configured(self) -> None:
        if not self.token:
            raise TencentDocumentError("未配置腾讯文档 MCP 个人 Token")

    @staticmethod
    def _result_data(result: Dict[str, Any]) -> Dict[str, Any]:
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        return result

    def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_configured()
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
        last_error = "腾讯文档 MCP 请求失败"
        for attempt in range(self.retry_count + 1):
            response = self._request.post_res(
                url=TENCENT_DOCS_MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                headers={
                    "Authorization": self.token,
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            if response is None:
                last_error = "腾讯文档 MCP 无响应"
            else:
                try:
                    status_code = int(response.status_code)
                    try:
                        payload = response.json()
                    except ValueError as error:
                        payload = None
                        for line in str(response.text or "").splitlines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                candidate = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            if isinstance(candidate, dict):
                                payload = candidate
                                break
                        if payload is None:
                            raise TencentDocumentError(
                                f"腾讯文档 MCP 返回了无效 JSON：HTTP {status_code}"
                            ) from error
                finally:
                    response.close()
                if status_code in {429, 500, 502, 503, 504}:
                    last_error = f"腾讯文档 MCP 临时异常：HTTP {status_code}"
                elif status_code != 200:
                    raise TencentDocumentError(
                        f"腾讯文档 MCP 请求失败：HTTP {status_code}"
                    )
                elif isinstance(payload.get("error"), dict):
                    error = payload["error"]
                    raise TencentDocumentError(
                        str(error.get("message") or error.get("code") or "MCP 调用失败")
                    )
                else:
                    result = payload.get("result")
                    if not isinstance(result, dict):
                        raise TencentDocumentError("腾讯文档 MCP 响应缺少 result")
                    if result.get("isError"):
                        data = self._result_data(result)
                        raise TencentDocumentError(
                            str(
                                data.get("message")
                                or data.get("error")
                                or "MCP 工具调用失败"
                            )
                        )
                    return self._result_data(result)
            if attempt < self.retry_count:
                sleep(min(2**attempt, 8))
        raise TencentDocumentError(last_error)

    def _tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    @staticmethod
    def _find_list(data: Any, *keys: str) -> List[Dict[str, Any]]:
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            for value in data.values():
                found = TencentDocumentMcpClient._find_list(value, *keys)
                if found:
                    return found
        return []

    @staticmethod
    def _find_value(data: Any, *keys: str, default: Any = None) -> Any:
        if isinstance(data, dict):
            for key in keys:
                if key in data:
                    return data[key]
            for value in data.values():
                found = TencentDocumentMcpClient._find_value(value, *keys, default=None)
                if found is not None:
                    return found
        return default

    def get_sheet_info(self, file_id: str) -> List[Dict[str, Any]]:
        """返回文档内普通子表和智能子表的统一清单。"""
        data = self._tool("sheet.get_sheet_info", {"file_id": file_id})
        raw_sheets = self._find_list(
            data,
            "sheets",
            "sheet_list",
            "sheetList",
            "properties",
        )
        sheets: List[Dict[str, Any]] = []
        for item in raw_sheets:
            sheet_id = str(
                item.get("sheet_id") or item.get("sheetId") or item.get("id") or ""
            ).strip()
            title = str(
                item.get("sheet_name") or item.get("title") or item.get("name") or ""
            ).strip()
            if not sheet_id or not title:
                continue
            sheet_type = (
                str(item.get("sheet_type") or item.get("sheetType") or "worksheet")
                .strip()
                .lower()
            )
            sheets.append(
                {
                    "sheet_id": sheet_id,
                    "title": title,
                    "sheet_type": (
                        "smartsheet" if sheet_type == "smartsheet" else "worksheet"
                    ),
                    "row_count": TencentDocumentClient._property_int(
                        item, "row_count", "rowCount", "rowTotal"
                    ),
                    "column_count": TencentDocumentClient._property_int(
                        item, "column_count", "columnCount", "columnTotal"
                    ),
                }
            )
        if not sheets:
            raise TencentDocumentError("腾讯文档 MCP 响应中没有可用工作表")
        return sheets

    def get_fields(
        self,
        file_id: str,
        sheet_id: str,
        view_id: str = "",
    ) -> List[str]:
        """取得智能表字段标题；字段只用于表头适配，不读取附件正文。"""
        offset = 0
        titles: List[str] = []
        while True:
            arguments: Dict[str, Any] = {
                "file_id": file_id,
                "sheet_id": sheet_id,
                "offset": offset,
                "limit": 100,
            }
            if view_id:
                arguments["view_id"] = view_id
            data = self._tool("smartsheet.list_fields", arguments)
            fields = self._find_list(data, "fields", "field_list", "fieldList")
            for field in fields:
                title = str(
                    field.get("field_title")
                    or field.get("title")
                    or field.get("name")
                    or field.get("field")
                    or ""
                ).strip()
                if title and title not in titles:
                    titles.append(title)
            has_more = bool(
                self._find_value(data, "has_more", "hasMore", default=False)
            )
            next_offset = self._find_value(data, "next", "next_offset", "nextOffset")
            if not has_more or not fields:
                break
            try:
                offset = int(next_offset)
            except (TypeError, ValueError):
                offset += len(fields)
        if not titles:
            raise TencentDocumentError(f"智能表 {sheet_id} 没有可读取字段")
        return titles

    @classmethod
    def _value_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return str(value).strip()
        if isinstance(value, list):
            parts = [cls._value_text(item) for item in value]
            return "\n".join(dict.fromkeys(part for part in parts if part))
        if isinstance(value, dict):
            link = cls._value_text(value.get("link"))
            text = cls._value_text(value.get("text"))
            if text.lower().startswith(("magnet:?", "ed2k://")):
                return text
            if link:
                return link
            for key in (
                "items",
                "value",
                "formatted_value",
                "formattedValue",
                "title",
                "name",
            ):
                if key in value:
                    parsed = cls._value_text(value[key])
                    if parsed:
                        return parsed
            parts = [
                cls._value_text(item)
                for key, item in value.items()
                if key not in {"type", "id", "field_id", "color", "status"}
            ]
            return "\n".join(dict.fromkeys(part for part in parts if part))
        return ""

    @classmethod
    def record_values(cls, record: Dict[str, Any]) -> Dict[str, str]:
        """把智能表多种字段值结构压平成“字段名 → 文本”。"""
        values: Dict[str, str] = {}
        raw_values = record.get("field_values") or record.get("fieldValues") or []
        if isinstance(raw_values, dict):
            raw_values = [
                {"field": field, "value": value} for field, value in raw_values.items()
            ]
        for item in raw_values:
            if not isinstance(item, dict):
                continue
            field = str(
                item.get("field")
                or item.get("field_title")
                or item.get("title")
                or item.get("name")
                or ""
            ).strip()
            if not field:
                continue
            candidates = [
                value
                for key, value in item.items()
                if key not in {"field", "field_title", "title", "name", "field_id"}
            ]
            text = cls._value_text(candidates)
            if text:
                values[field] = text
        return values

    def get_records(
        self,
        file_id: str,
        sheet_id: str,
        offset: int,
        limit: int,
        view_id: str = "",
    ) -> Dict[str, Any]:
        """按 offset 分页读取智能表记录，单次最多 100 条。"""
        safe_limit = min(max(int(limit or 1), 1), 100)
        arguments: Dict[str, Any] = {
            "file_id": file_id,
            "sheet_id": sheet_id,
            "offset": max(int(offset or 0), 0),
            "limit": safe_limit,
        }
        if view_id:
            arguments["view_id"] = view_id
        data = self._tool("smartsheet.list_records", arguments)
        records = self._find_list(data, "records", "record_list", "recordList")
        total = self._find_value(data, "total", "total_count", "totalCount", default=0)
        next_offset = self._find_value(data, "next", "next_offset", "nextOffset")
        has_more = bool(self._find_value(data, "has_more", "hasMore", default=False))
        try:
            total = int(total or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            next_offset = int(next_offset)
        except (TypeError, ValueError):
            next_offset = int(offset or 0) + len(records)
        return {
            "records": records,
            "total": total,
            "next": next_offset,
            "has_more": has_more,
            "requested_rows": safe_limit,
        }


def looks_like_url(value: str) -> bool:
    """
    判断文本是否包含 HTTP 地址

    :param value (str): 待判断文本

    :return bool: 包含 HTTP 地址时返回 True
    """
    return bool(re.search(r"https?://", str(value or ""), re.IGNORECASE))
