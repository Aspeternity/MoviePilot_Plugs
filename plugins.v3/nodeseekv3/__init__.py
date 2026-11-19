"""基于 MoviePilot V3 浏览器能力的 NodeSeek 每日签到插件。"""

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from functools import partial
from http.cookies import CookieError, SimpleCookie
import json
import random
import re
import threading
from threading import Timer
from typing import Any, TypedDict
from urllib.parse import urlsplit

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool


from app.sdk.browser import launch_browser_context
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.scheduler import add_plugin_once_job, list_scheduler_jobs, remove_plugin_once_job
from app.sdk.security import verify_token
from app import schemas
from app.schemas.types import MessageType


_BASE_URL = "https://www.nodeseek.com"
_SITE_HOST = urlsplit(_BASE_URL).netloc
_BEIJING = timezone(timedelta(hours=8))
_VERIFY_STATE_KEY = "nodeseek:cookiecloud-verification"
_CHALLENGE_TITLES = ("just a moment", "attention required", "请稍候")
_TIME_PATTERN = r"(?:[01]\d|2[0-3]):[0-5]\d"
_SIGN_SERVICE_ID = "NodeSeekV3"
_WINDOW_PLAN_KEY = "random_window_next_run"
_WINDOW_ATTEMPT_KEY = "random_window_last_attempt"
_SIGN_STATE_KEY = "sign_state_v2"
_LEGACY_SIGN_KEYS = ("sign_history", "last_result", "last_sign_date")
_HISTORY_DAYS = 30
_HISTORY_PAGE_SIZE = 10
_REWARD_PATTERN = r"(?:签到收益|签到奖励|签到获得)[：:\s]*(?:(?:是|为|获得|得到|了)\s*)?(\d{1,6})\s*(?:个|只|枚)?\s*鸡腿"
_CRONTAB_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAY_BASIS = "sunday_zero"

_LOGIN_SCRIPT = r"""async () => {
  const readUser = config => {
    if (!config || !Object.hasOwn(config, 'user')) return 'unknown';
    if (config.user === null || config.user === false) return 'signed_out';
    const id = config.user?.member_id;
    if ((Number.isSafeInteger(id) && id > 0) || (typeof id === 'string' && /^[1-9]\d*$/.test(id))) {
      return 'authenticated';
    }
    return 'unknown';
  };

  for (let attempt = 0; attempt <= 10; attempt++) {
    const current = readUser(window.__config__);
    if (current === 'authenticated') return current;

    let embedded = 'unknown';
    const text = document.getElementById('temp-script')?.textContent.trim();
    if (text) {
      try {
        const json = text.startsWith('{') ? text : new TextDecoder().decode(
          Uint8Array.from(atob(text), char => char.charCodeAt(0)));
        embedded = readUser(JSON.parse(json));
      } catch (_) {
        // 页面可能仍在初始化，稍后再检查运行时数据。
      }
    }
    if (current === 'unknown' && embedded === 'authenticated') return embedded;
    if (attempt === 10) return current === 'unknown' ? embedded : current;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  return 'unknown';
}"""

_VERIFICATION_MESSAGES = {
    "authenticated": "已确认 NodeSeek 登录成功。",
    "signed_out": "已读取 CookieCloud，但 NodeSeek 返回未登录。请在浏览器重新登录，再同步 CookieCloud。",
    "unknown": "已读取 CookieCloud，但未识别到 NodeSeek 登录状态。请确认浏览器仍已登录并同步 CookieCloud。",
    "cloud_unavailable": "无法读取 CookieCloud，请检查 MoviePilot 的 CookieCloud 设置和连接。",
    "cookie_missing": "CookieCloud 中没有可用的 NodeSeek Cookie。请在浏览器登录 NodeSeek 后同步。",
    "proxy_missing": "MoviePilot 尚未配置系统代理。请先配置代理，或关闭代理后重新验证。",
    "blocked": "NodeSeek 拒绝访问或要求站点验证。请在浏览器确认能正常访问后重试。",
    "redirected": "NodeSeek 跳转到了其他网站。请检查网络和代理设置后重试。",
    "network_error": "无法正常访问 NodeSeek。请检查 MoviePilot 的网络或代理后重试。",
    "browser_error": "MoviePilot 无法启动浏览器，请检查宿主浏览器环境后重试。",
}


class CookieCloudVerificationOptions(BaseModel):
    """仅用于本次验证的网络设置，缺省字段沿用已保存配置。"""

    use_proxy: StrictBool | None = None
    user_agent: str | None = Field(default=None, max_length=1024)


class CookieCloudVerificationResult(TypedDict):
    """提供给配置页的脱敏验证结果。"""

    success: bool
    status: str
    message: str


class SignRecord(TypedDict):
    day: str
    time: str
    success: bool | None
    message: str
    gain: int | None


class SignState(TypedDict):
    records: list[SignRecord]
    confirmed_day: str | None


def _require_administrator(token: schemas.TokenPayload = Depends(verify_token)) -> None:
    """仅允许管理员验证 CookieCloud 登录。"""
    if token.super_user is not True:
        raise HTTPException(status_code=403, detail="仅管理员可验证 NodeSeek 登录")


def _download_cookiecloud() -> tuple[dict[str, str] | None, str]:
    """按需调用宿主的 CookieCloud 兼容门面，不读取插件独立凭据。"""
    from app.helper.cookiecloud import CookieCloudHelper
    return CookieCloudHelper().download()


_SCRIPT = """
async (random) => {
  const read = async (url, options = {}) => {
    const response = await fetch(url, {credentials: 'include', cache: 'no-store',
      headers: {'Accept': 'application/json'}, ...options});
    let data = null;
    try { data = await response.json(); } catch (_) {}
    return {status: response.status, ok: response.ok, data};
  };
  const record = board => {
    const data = board && board.data;
    if (!data || typeof data !== 'object') return null;
    const current = item => item && (item.current || item.isCurrent || item.self || item.isSelf || item.mine);
    if (data.record && !Array.isArray(data.record) && typeof data.record === 'object'
        && Object.keys(data.record).length) return {gain: data.record.gain ?? null};
    if (Array.isArray(data.record)) {
      const item = data.record.find(current);
      return item ? {gain: item.gain ?? null} : null;
    }
    const item = Array.isArray(data.memberList) ? data.memberList.find(current) : null;
    return item ? {gain: item.gain ?? null} : null;
  };
  const before = await read('/api/attendance/board?page=1');
  if (before.status === 401) return {kind: 'auth', status: before.status};
  if (before.status === 403) return {kind: 'blocked', status: before.status};
  if (!before.ok || !before.data || typeof before.data !== 'object')
    return {kind: 'board_error', status: before.status};
  const previous = record(before);
  if (previous) return {kind: 'already', gain: previous.gain};
  const signed = await read('/api/attendance?random=' + Boolean(random), {method: 'POST'});
  let after = null;
  try { after = await read('/api/attendance/board?page=1'); } catch (_) {}
  const confirmed = after && after.ok ? record(after) : null;
  return {kind: 'result', status: signed.status, ok: signed.ok,
    success: signed.data && signed.data.success === true,
    message: (signed.data && typeof signed.data.message === 'string') ? signed.data.message.slice(0, 512) : null,
    gain: (signed.data && signed.data.gain) ?? (confirmed && confirmed.gain),
    confirmed: Boolean(confirmed)};
}
"""


def _cron_weekdays(field: str) -> str:
    """将表单使用的星期编号转换为 APScheduler 支持的星期名。"""
    if field == "*":
        return field
    indices = {name: index for index, name in enumerate(_CRONTAB_WEEKDAYS)}
    indices.update({str(index): index for index in range(8)})
    days = set()
    for segment in field.lower().split(","):
        selection, slash, step = segment.partition("/")
        if slash and not re.fullmatch(r"[1-7]", step):
            raise ValueError("星期步长须为 1 到 7")
        if selection == "*":
            first, last = 0, 6
        else:
            begin, dash, finish = selection.partition("-")
            if begin not in indices or (dash and finish not in indices) or (slash and not dash):
                raise ValueError("Cron 星期格式无效")
            first = indices[begin]
            last = indices[finish] if dash else first
            if dash and finish == "sun" and first > 0:
                last = 7
            if first > last:
                raise ValueError("Cron 星期范围应按起始日到结束日排列")
        for day in range(first, last + 1, int(step) if slash else 1):
            days.add(day % 7)
    return ",".join(_CRONTAB_WEEKDAYS[day] for day in sorted(days))


class RandomWindowTrigger(BaseTrigger):
    """每天在指定的北京时间窗口内随机触发一次。"""

    def __init__(
        self,
        start: str,
        end: str,
        *,
        restored: datetime | None = None,
        last_attempt: date | None = None,
        on_plan: Callable[[datetime], None] | None = None,
    ) -> None:
        """解析同一天内的起止时间，结束时间以分钟为最小单位。"""
        self.start = self._parse_time(start)
        self.end = self._parse_time(end)
        if self.start >= self.end:
            raise ValueError("开始时间必须早于结束时间，暂不支持跨午夜时段")
        self._pending: datetime | None = None
        self._last_due: datetime | None = None
        self._restored = restored
        self._last_attempt = last_attempt
        self._on_plan = on_plan

    @staticmethod
    def _parse_time(value: str) -> int:
        """将严格的 HH:MM 转换为自午夜起的秒数。"""
        if not isinstance(value, str) or not re.fullmatch(_TIME_PATTERN, value):
            raise ValueError("时间必须使用 00:00 至 23:59 的 HH:MM 格式")
        hour, minute = map(int, value.split(":"))
        return (hour * 60 + minute) * 60

    def _bounds(self, day: date) -> tuple[datetime, datetime]:
        midnight = datetime(day.year, day.month, day.day, tzinfo=_BEIJING)
        return midnight + timedelta(seconds=self.start), midnight + timedelta(seconds=self.end)

    def is_due(self, now: datetime) -> bool:
        """只接收今天已交付的任务，并在结束时间前执行。"""
        now = now.astimezone(_BEIJING)
        start, end = self._bounds(now.date())
        return self._last_due is not None and start <= self._last_due <= now < end

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime:
        """恢复仍有效的计划，并兼容 APScheduler 对同一次触发的重复查询。"""
        now = now.astimezone(_BEIJING)
        if previous_fire_time is not None:
            previous_fire_time = previous_fire_time.astimezone(_BEIJING)
            if previous_fire_time <= now and (self._last_due is None or previous_fire_time > self._last_due):
                self._last_due = previous_fire_time
        if self._pending is not None:
            if previous_fire_time is None or previous_fire_time < self._pending:
                return self._pending
        day = now.date()
        if previous_fire_time is not None and previous_fire_time.date() >= day:
            day = previous_fire_time.date() + timedelta(days=1)
        if self._last_attempt == day:
            day += timedelta(days=1)
        start, end = self._bounds(day)
        if now >= end:
            start, end = self._bounds(day + timedelta(days=1))
        lower = max(now, start)
        restored, self._restored = self._restored, None
        if restored is not None and lower <= restored < end:
            self._pending = restored
        else:
            # randrange 取不到上界，即使抽中最后一微秒也仍处在时段内。
            microseconds = (end - lower) // timedelta.resolution
            self._pending = lower + timedelta(microseconds=random.randrange(microseconds))
            if self._on_plan is not None:
                self._on_plan(self._pending)
        return self._pending

    def __str__(self) -> str:
        """提供不含凭据的调度器描述。"""
        return f"random-window[{self.start // 3600:02d}:{self.start // 60 % 60:02d}-" \
               f"{self.end // 3600:02d}:{self.end // 60 % 60:02d} Asia/Shanghai]"


class NodeSeekV3(_PluginBase):
    """通过隔离的浏览器上下文执行论坛签到。"""

    plugin_name = "NodeSeek 论坛签到"
    plugin_desc = "通过 MoviePilot 浏览器定时执行 NodeSeek 论坛签到。"
    plugin_icon = ""
    plugin_version = "1.6.1"
    plugin_label = "站点签到"
    plugin_author = "Aspeternity"
    author_url = "https://github.com/Aspeternity"
    plugin_config_prefix = "nodeseekv3_"
    plugin_order = 100
    auth_level = 2

    _defaults = {"enabled": False, "cookie": "", "cookiecloud": False, "cron": "0 8 * * *",
                 "random": True, "notify": True, "use_proxy": False,
                 "user_agent": "", "onlyonce": False, "schedule_mode": "cron",
                 "window_start": "08:00", "window_end": "10:00"}

    def __init__(self) -> None:
        """初始化实例状态和运行互斥锁。"""
        super().__init__()
        self._config = dict(self._defaults)
        self._lock = threading.Lock()
        self._window_attempt_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._history_timer: Timer | None = None
        self._history_session: object | None = None

    @staticmethod
    def _selected_window(config: dict[str, Any], key: str) -> str | None:
        """在有独立选择时合成时间；原始时间不合法时保留为无效配置。"""
        hour, minute = config.get(f"{key}_hour"), config.get(f"{key}_minute")
        if hour is None and minute is None:
            return None
        return f"{hour if hour is not None else ''}:{minute if minute is not None else ''}"

    @classmethod
    def _window_value(cls, config: dict[str, Any], key: str) -> Any:
        selected = cls._selected_window(config, key)
        return selected if selected is not None else config.get(key)

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并按需向宿主调度器提交一次性签到。"""
        self.stop_service()
        self._config = {**self._defaults, **(config or {})}
        self._config.pop("_nodeseek_cloud_fetching", None)
        self._config.pop("_nodeseek_cloud_verifying", None)
        for key in ("window_start", "window_end"):
            self._config[key] = self._window_value(self._config, key)
        with self._history_lock:
            history_session = self._history_session = object()
        self._maintain_history(history_session)
        if self._config.get("onlyonce"):
            self._config["onlyonce"] = False
            self.update_config(self._config)
            if not add_plugin_once_job(self.__class__.__name__, "once", self.sign,
                                       "NodeSeek 立即签到", delay_seconds=3):
                logger.error("NodeSeek 一次性签到未能加入调度器")

    def get_state(self) -> bool:
        """返回周期签到开关。"""
        return bool(self._config.get("enabled"))

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """注册需要管理员身份的 CookieCloud 登录验证接口。"""
        return [{"path": "/cookiecloud_verify", "endpoint": self.verify_cookiecloud_login,
                 "methods": ["POST"], "auth": "bear", "dependencies": [Depends(_require_administrator)],
                 "response_model": CookieCloudVerificationResult,
                 "summary": "验证 CookieCloud 登录"}]

    def _cloud_cookie(self) -> tuple[str, str | None]:
        """读取目标域 Cookie；仅在调用链内返回原值，失败时返回固定状态。"""
        try:
            groups, error = _download_cookiecloud()
            if error or not isinstance(groups, dict):
                return "unavailable", None
            source = groups.get("nodeseek.com")
            if not isinstance(source, str) or not self._cookies(source):
                return "missing", None
            return "found", source
        except Exception:
            return "unavailable", None

    @staticmethod
    def _route_verification_request(route: Any) -> None:
        """仅允许 NodeSeek 首页和初始化页面所需的同源脚本。"""
        request = route.request
        url = urlsplit(request.url)
        if url.scheme == "https" and url.netloc == _SITE_HOST and request.method in ("GET", "HEAD"):
            if request.is_navigation_request() and url.path == "/" and not url.query:
                route.continue_()
                return
            if request.resource_type == "script" and not url.path.startswith(("/api/", "/cdn-cgi/")):
                route.continue_()
                return
        route.abort()

    @staticmethod
    def _verification_status(page: Any, response: Any) -> str:
        """从站点响应和当前用户标记得出脱敏状态。"""
        if response is None:
            return "network_error"
        url = urlsplit(page.url)
        if url.scheme != "https" or url.netloc != _SITE_HOST:
            return "redirected"
        if (
            response.status in (403, 429)
            or response.headers.get("cf-mitigated") == "challenge"
            or any(marker in page.title().lower() for marker in _CHALLENGE_TITLES)
        ):
            return "blocked"
        if response.status == 401:
            return "signed_out"
        if not response.ok:
            return "network_error"
        status = page.evaluate(_LOGIN_SCRIPT)
        return status if status in ("authenticated", "signed_out") else "unknown"

    @staticmethod
    def _verification_result(status: str) -> CookieCloudVerificationResult:
        """记录固定状态，避免把站点响应或浏览器异常写进日志。"""
        logger.info("NodeSeek CookieCloud 验证结果：%s", status)
        return {
            "success": status == "authenticated",
            "status": status,
            "message": _VERIFICATION_MESSAGES[status],
        }

    def verify_cookiecloud_login(
        self, options: CookieCloudVerificationOptions | None = None
    ) -> CookieCloudVerificationResult:
        """使用本次表单网络设置检查 CookieCloud 登录状态。"""
        status, source = self._cloud_cookie()
        if status != "found" or source is None:
            return self._verification_result("cloud_unavailable" if status == "unavailable" else "cookie_missing")
        network = options.model_dump(exclude_none=True) if options is not None else {}
        user_agent = str(network.get("user_agent", self._config.get("user_agent")) or "").strip()
        use_proxy = network.get("use_proxy", self._config.get("use_proxy"))
        browser_options: dict[str, Any] = {"headless": True, "service_workers": "block"}
        if user_agent:
            browser_options["user_agent"] = user_agent
        if use_proxy:
            proxy = getattr(settings, "PROXY_SERVER", None)
            if not proxy:
                return self._verification_result("proxy_missing")
            browser_options["proxy"] = proxy

        try:
            context = launch_browser_context(**browser_options)
        except Exception:
            return self._verification_result("browser_error")
        try:
            context.add_cookies(self._cookies(source))
            context.route("**/*", self._route_verification_request)
            page = context.new_page()
            response = page.goto(_BASE_URL + "/", wait_until="domcontentloaded", timeout=15000)
            return self._verification_result(self._verification_status(page, response))
        except Exception:
            return self._verification_result("network_error")
        finally:
            try:
                context.close()
            except Exception:
                logger.warning("NodeSeek 浏览器上下文关闭失败")

    def _schedule_trigger(self, *, register: bool = False) -> CronTrigger | RandomWindowTrigger:
        """用同一套配置规则生成触发器，只有注册随机计划时才读写恢复状态。"""
        mode = self._config.get("schedule_mode", "cron")
        if mode == "window":
            start = self._window_value(self._config, "window_start")
            end = self._window_value(self._config, "window_end")
            if not register:
                return RandomWindowTrigger(start, end)
            return RandomWindowTrigger(
                start,
                end,
                restored=self._restore_window_time(start, end),
                last_attempt=self._window_attempt_date(),
                on_plan=partial(self._store_window_time, start, end),
            )
        if mode == "cron":
            expression = str(self._config.get("cron") or "").strip()
            if self._config.get("cron_weekday_basis") == _WEEKDAY_BASIS:
                fields = expression.split()
                if len(fields) != 5:
                    raise ValueError("Cron 表达式须包含五段")
                fields[-1] = _cron_weekdays(fields[-1])
                expression = " ".join(fields)
            return CronTrigger.from_crontab(expression, timezone=_BEIJING)
        raise ValueError("未知的签到计划模式")

    def _restore_window_time(self, start: str, end: str) -> datetime | None:
        saved = self.get_data(_WINDOW_PLAN_KEY)
        if not isinstance(saved, dict) or saved.get("start") != start or saved.get("end") != end:
            return None
        try:
            scheduled = datetime.fromisoformat(saved.get("at", ""))
            return scheduled.astimezone(_BEIJING) if scheduled.tzinfo is not None else None
        except (TypeError, ValueError):
            return None

    def _store_window_time(self, start: str, end: str, scheduled: datetime) -> None:
        try:
            self.save_data(_WINDOW_PLAN_KEY, {"start": start, "end": end, "at": scheduled.isoformat()})
        except Exception:
            logger.warning("NodeSeek 无法保存随机签到时间")

    def _window_attempt_date(self) -> date | None:
        try:
            return date.fromisoformat(self.get_data(_WINDOW_ATTEMPT_KEY) or "")
        except (TypeError, ValueError):
            return None

    def _run_window_sign(self, trigger: RandomWindowTrigger) -> None:
        """限制随机任务执行时间，手动发起的签到仍使用 sign。"""
        with self._window_attempt_lock:
            now = datetime.now(_BEIJING)
            if not trigger.is_due(now):
                logger.info("NodeSeek 随机签到已错过计划时段，本次跳过")
                return
            if self._window_attempt_date() == now.date():
                return
            self.save_data(_WINDOW_ATTEMPT_KEY, now.date().isoformat())
        self.sign()

    def get_service(self) -> list[dict[str, Any]]:
        """按配置注册签到及独立的 CookieCloud 同步任务。"""
        services = []
        if self._config.get("cookiecloud"):
            interval = getattr(settings, "COOKIECLOUD_INTERVAL", None)
            try:
                minutes = max(1, int(interval)) if interval else 1440
            except (TypeError, ValueError):
                minutes = 1440
            services.append({"id": "NodeSeekV3CookieCloud", "name": "NodeSeek CookieCloud 同步",
                             "trigger": IntervalTrigger(minutes=minutes, timezone=_BEIJING),
                             "func": self.sync_cookiecloud, "kwargs": {}})
        if not self.get_state() or (not self._config.get("cookiecloud") and not self._cookies(str(self._config.get("cookie") or ""))):
            if self.get_state() and not self._config.get("cookiecloud"):
                logger.error("NodeSeek 手动模式缺少有效 Cookie，不注册签到任务")
            return services
        try:
            trigger = self._schedule_trigger(register=True)
        except ValueError:
            logger.error("NodeSeek 签到计划无效，请检查 Cron 或随机时段设置")
            return services
        action = partial(self._run_window_sign, trigger) if isinstance(trigger, RandomWindowTrigger) else self.sign
        services.insert(0, {"id": _SIGN_SERVICE_ID, "name": "NodeSeek 论坛签到",
                            "trigger": trigger, "func": action, "kwargs": {}})
        return services

    def sync_cookiecloud(self) -> None:
        """定时刷新云端快照，不覆盖用户正在编辑的整份插件配置。"""
        if not self._config.get("cookiecloud"):
            return
        status, source = self._cloud_cookie()
        if status != "found" or source is None or not self._config.get("cookiecloud"):
            if status != "found":
                logger.warning("NodeSeek CookieCloud 自动同步失败，请检查云端配置或浏览器登录")
            return
        self.save_data("cookiecloud_synced_cookie", source)

    @staticmethod
    def _cookies(source: str) -> list[dict[str, str]]:
        """将 Cookie 请求头转换为仅注入 NodeSeek 域的条目。"""
        jar = SimpleCookie()
        try:
            jar.load(source.strip())
        except CookieError:
            return []
        return [{"name": key, "value": value.value, "url": _BASE_URL}
                for key, value in jar.items()]

    def _sign_cookies(self) -> list[dict[str, str]]:
        """按选定来源获取 Cookie；云端失败时绝不回退手动值。"""
        if self._config.get("cookiecloud"):
            status, source = self._cloud_cookie()
            if status == "found" and source is not None:
                return self._cookies(source)
            logger.warning("NodeSeek CookieCloud 同步失败或未找到可用的 NodeSeek Cookie，本次不会使用手动 Cookie")
            return []
        return self._cookies(str(self._config.get("cookie") or ""))

    @staticmethod
    def _page_diagnostic(page: Any, response: Any) -> str:
        """识别常见验证页面，仅返回固定诊断，不记录页面内容。"""
        if response is None:
            return "首页未返回 HTTP 响应，请检查网络"
        if not page.url.startswith(_BASE_URL + "/"):
            return "首页跳转到其他地址，请检查网络或站点验证"
        try:
            title = page.title().lower()
        except Exception:
            title = ""
        if any(marker in title for marker in _CHALLENGE_TITLES):
            return "检测到可能的站点验证页面，请在浏览器中人工处理后检查 Cookie"
        if not response.ok:
            status = getattr(response, "status", None)
            if status == 403:
                return "首页返回 HTTP 403，可能被站点防护拦截"
            if isinstance(status, int):
                return f"首页访问失败（HTTP {status}）"
            return "首页无法正常访问，请检查网络或站点验证"
        return ""

    @staticmethod
    def _gain(value: Any, message: Any = None) -> int | None:
        """仅从可信形态的奖励字段或明确的鸡腿数量描述提取整数。"""
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100000:
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{1,6}", value.strip()):
            amount = int(value.strip())
            return amount if amount <= 100000 else None
        if isinstance(message, str):
            match = re.search(_REWARD_PATTERN, message)
            if match:
                amount = int(match.group(1))
                return amount if amount <= 100000 else None
        return None

    @classmethod
    def _interpret_result(cls, result: Any) -> dict[str, Any]:
        """解释站点结果，奖励未知时不推测固定值或零值。"""
        output: dict[str, Any] = {"success": False, "message": "站点返回了无法识别的签到结果", "gain": None}
        if not isinstance(result, dict):
            return output
        kind, status = result.get("kind"), result.get("status")
        if kind == "auth" or status == 401:
            output["message"] = "站点拒绝访问，请检查 Cookie 或站点验证"
        elif kind == "blocked" or status == 403:
            output["message"] = "签到接口返回 HTTP 403，可能被站点防护拦截"
        elif kind == "board_error":
            suffix = f"（HTTP {status}）" if isinstance(status, int) else ""
            output["message"] = "无法读取签到状态，请检查网络或站点响应" + suffix
        elif kind == "already":
            output.update(success=True, message="今日已签到", gain=cls._gain(result.get("gain")))
        elif kind == "result":
            raw_message = result.get("message")
            already = isinstance(raw_message, str) and bool(re.search(r"(?:已经|已).{0,5}签到|签到.{0,5}(?:完成|过)", raw_message))
            gain = cls._gain(result.get("gain"), raw_message)
            explicit_gain = cls._gain(None, raw_message) is not None
            auth_error = isinstance(raw_message, str) and bool(re.search(r"未登录|请先登录|登录失效|USER NOT FOUND", raw_message, re.I))
            if result.get("ok") and auth_error:
                output["message"] = "站点未识别登录状态，请检查 Cookie 是否有效"
            elif (result.get("ok") and (result.get("success") is True or already or explicit_gain)) or result.get("confirmed"):
                output.update(success=True, message="今日已签到" if already else "签到成功", gain=gain)
            else:
                suffix = f"（HTTP {status}）" if isinstance(status, int) else ""
                output["message"] = "签到未确认" + suffix
        return output

    def _request_sign(self) -> dict[str, Any]:
        """执行一次浏览器签到，不向日志传递远端正文或凭据。"""
        cookies = self._sign_cookies()
        if not cookies:
            message = ("CookieCloud 未取得可用的 NodeSeek Cookie，请检查同步或关闭 CookieCloud 后手动填写"
                       if self._config.get("cookiecloud") else "未配置有效的 Cookie")
            return {"success": False, "message": message, "gain": None}
        options: dict[str, Any] = {"headless": True}
        if self._config.get("user_agent"):
            options["user_agent"] = str(self._config["user_agent"]).strip()
        if self._config.get("use_proxy"):
            proxy = getattr(settings, "PROXY_SERVER", None)
            if not proxy:
                return {"success": False, "message": "已启用代理，但 MoviePilot 未配置代理地址", "gain": None}
            options["proxy"] = proxy
        context = None
        stage = "启动浏览器"
        try:
            context = launch_browser_context(**options)
            stage = "注入 Cookie"
            context.add_cookies(cookies)
            stage = "打开首页"
            page = context.new_page()
            page.set_default_timeout(60000)
            response = page.goto(_BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
            diagnostic = self._page_diagnostic(page, response)
            if diagnostic:
                return {"success": False, "message": diagnostic, "gain": None}
            stage = "请求签到接口"
            result = page.evaluate(_SCRIPT, bool(self._config.get("random")))
        except Exception:
            return {"success": False, "message": f"{stage}失败，请检查浏览器环境、网络或站点验证", "gain": None}
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.warning("NodeSeek 浏览器上下文关闭失败")
        return self._interpret_result(result)

    def sign(self) -> None:
        """串行执行签到，仅在成功确认后记录北京时间日期。"""
        if not self._lock.acquire(blocking=False):
            logger.warning("NodeSeek 签到正在运行，本次跳过")
            return
        try:
            sign_day = datetime.now(_BEIJING).date()
            with self._history_lock:
                _, _, state = self._read_sign_state(sign_day)
            if state["confirmed_day"] == sign_day.isoformat():
                logger.info("NodeSeek 今日已确认签到，跳过重复执行")
                return
            result = self._request_sign()
            finished = datetime.now(_BEIJING)
            record = self._record_from_data({**result, "day": sign_day.isoformat(), "time": finished.isoformat()})
            if record is None:
                return
            with self._history_lock:
                _, legacy, state = self._read_sign_state(finished.date())
                state["records"].insert(0, record)
                if record["success"] is True:
                    state["confirmed_day"] = max(state["confirmed_day"] or "", record["day"])
                if self._save_sign_state(state):
                    self._discard_legacy_sign_data(legacy)
                self._arm_history_timer()
            gain_text = self._reward_text(record)
            text = f"结果：{record['message']}\n签到奖励：{gain_text}\n时间：{self._format_time(record['time'])}"
            logger.info("NodeSeek 签到结果：%s，奖励：%s", record["message"], gain_text)
            if self._config.get("notify"):
                self.post_message(mtype=MessageType.SiteMessage, title="NodeSeek 论坛签到", text=text)
        finally:
            self._lock.release()

    @staticmethod
    def _card(title: str, icon: str, content: list[dict]) -> dict:
        """构造与宿主 Vuetify JSON 兼容的分组卡片。"""
        return {"component": "VCard", "props": {"class": "mb-4", "variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center"},
                     "content": [{"component": "VIcon", "props": {"color": "primary", "class": "mr-2"},
                                  "text": icon}, {"component": "span", "text": title}]},
                    {"component": "VDivider"}, {"component": "VCardText", "content": content}]}

    @staticmethod
    def _col(component: str, model: str, label: str, width: int = 6, **props: Any) -> dict:
        """构造响应式表单字段列。"""
        return {"component": "VCol", "props": {"cols": 12, "md": width}, "content": [
            {"component": component, "props": {"model": model, "label": label, **props}}]}

    @staticmethod
    def _format_time(value: Any) -> str:
        """将 ISO 时间按北京时间显示，旧格式不作错误转换。"""
        if not value:
            return "尚未执行"
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(_BEIJING)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)

    @staticmethod
    def _parse_day(value: Any) -> date | None:
        try:
            return date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    @classmethod
    def _record_from_data(cls, data: Any) -> SignRecord | None:
        """规范化旧记录，无法识别日期的记录不能判断是否仍在保留期。"""
        if not isinstance(data, dict) or not isinstance(data.get("time"), str):
            return None
        try:
            finished = datetime.fromisoformat(data["time"])
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=_BEIJING)
            finished = finished.astimezone(_BEIJING)
        except (ValueError, OverflowError):
            return None
        day = cls._parse_day(data.get("day")) or finished.date()
        success = data.get("success")
        message = data.get("message")
        if isinstance(message, str):
            message = " ".join(message.split())[:300]
        else:
            message = ""
        return {
            "day": day.isoformat(),
            "time": finished.isoformat(timespec="microseconds"),
            "success": success if isinstance(success, bool) else None,
            "message": message or ("签到成功" if success is True else "签到未确认"),
            "gain": cls._gain(data.get("gain"), message) if success is True else None,
        }

    def _read_sign_state(
        self, today: date, *, read_legacy: bool = False
    ) -> tuple[Any, dict[str, Any], SignState]:
        """读取一个签到快照，必要时合并旧记录。读取本身不写入。"""
        stored = self.get_data(_SIGN_STATE_KEY)
        has_snapshot = isinstance(stored, dict) and isinstance(stored.get("records"), list)
        legacy = {}
        if read_legacy or not has_snapshot:
            for key in _LEGACY_SIGN_KEYS:
                value = self.get_data(key)
                if value is not None:
                    legacy[key] = value
        if has_snapshot:
            raw_records = stored["records"]
            confirmed = self._parse_day(stored.get("confirmed_day"))
        else:
            raw_records = legacy.get("sign_history")
            confirmed = self._parse_day(legacy.get("last_sign_date"))
        records = [
            record for item in raw_records if (record := self._record_from_data(item)) is not None
        ] if isinstance(raw_records, list) else []
        if has_snapshot and isinstance(legacy.get("sign_history"), list):
            seen = {tuple(record.values()) for record in records}
            for item in legacy["sign_history"]:
                restored = self._record_from_data(item)
                if restored is not None and tuple(restored.values()) not in seen:
                    records.append(restored)
                    seen.add(tuple(restored.values()))
        if legacy:
            latest = self._record_from_data(legacy.get("last_result"))
            if latest is not None and latest not in records:
                records.append(latest)
            legacy_day = self._parse_day(legacy.get("last_sign_date"))
            if legacy_day and legacy_day <= today and (confirmed is None or confirmed > today or legacy_day > confirmed):
                confirmed = legacy_day
        today_text = today.isoformat()
        cutoff = (today - timedelta(days=_HISTORY_DAYS - 1)).isoformat()
        confirmed_day = confirmed.isoformat() if confirmed and confirmed <= today else None
        records = [record for record in records if cutoff <= record["day"] <= today_text]
        records.sort(key=lambda record: record["time"], reverse=True)
        for record in records:
            if record["success"] is True and (confirmed_day is None or record["day"] > confirmed_day):
                confirmed_day = record["day"]
        return stored, legacy, {"records": records, "confirmed_day": confirmed_day}

    def _save_sign_state(self, state: SignState) -> bool:
        try:
            self.save_data(_SIGN_STATE_KEY, state)
            # MoviePilot 的保存接口没有返回值，读回确认后才允许移除旧数据。
            if self.get_data(_SIGN_STATE_KEY) == state:
                return True
        except Exception:
            pass
        logger.error("NodeSeek 签到记录保存失败")
        return False

    def _discard_legacy_sign_data(self, legacy: dict[str, Any]) -> bool:
        try:
            for key in legacy:
                self.del_data(key)
            if all(self.get_data(key) is None for key in legacy):
                return True
        except Exception:
            pass
        logger.warning("NodeSeek 旧签到数据清理失败，将在下次清理时重试")
        return False

    def _arm_history_timer(self) -> None:
        """在下一个北京时间零点清理。历史任务随插件停止和重载而取消。"""
        if self._history_session is None or self._history_timer is not None:
            return
        now = datetime.now(_BEIJING)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        timer = Timer(max(1.0, (midnight - now).total_seconds()), self._maintain_history, (self._history_session,))
        timer.daemon = True
        self._history_timer = timer
        timer.start()

    def _maintain_history(self, session: object) -> None:
        with self._history_lock:
            if session is not self._history_session:
                return
            self._history_timer = None
            try:
                stored, legacy, state = self._read_sign_state(datetime.now(_BEIJING).date(), read_legacy=True)
                needs_write = stored != state and (stored is not None or bool(legacy))
                saved = stored == state or (needs_write and self._save_sign_state(state))
                legacy_clean = not legacy or (saved and self._discard_legacy_sign_data(legacy))
                if saved and legacy_clean and (needs_write or legacy):
                    logger.info("NodeSeek 签到历史已整理，保留 %s 条", len(state["records"]))
                if state["records"] or not legacy_clean or (stored is not None and not saved):
                    self._arm_history_timer()
            except Exception:
                logger.warning("NodeSeek 签到历史清理失败，将在次日重试")
                self._arm_history_timer()

    @staticmethod
    def _record_status(record: SignRecord) -> str:
        if record["success"] is True:
            return "站点已签到" if record["message"] == "今日已签到" else "签到成功"
        if record["success"] is None or record["message"].startswith("签到未确认"):
            return "未确认"
        return "失败"

    @staticmethod
    def _reward_text(record: SignRecord) -> str:
        if record["success"] is not True:
            return "—"
        gain = record["gain"]
        return f"{gain} 个鸡腿" if gain is not None else "未知"

    def _legacy_cron_weekdays(self) -> str:
        """显示现有数字星期真正匹配的日期，避免用编辑器的编号猜测。"""
        expression = str(self._config.get("cron") or "").split()
        if self._config.get("cron_weekday_basis") == _WEEKDAY_BASIS or len(expression) != 5:
            return ""
        weekdays = expression[-1]
        if not re.search(r"\d", weekdays):
            return ""
        try:
            trigger = CronTrigger(day_of_week=weekdays, hour=0, minute=0, timezone=_BEIJING)
            monday = datetime(2024, 1, 1, tzinfo=_BEIJING)
            labels = []
            for offset, label in enumerate(_WEEKDAY_LABELS):
                day = monday + timedelta(days=offset)
                if trigger.get_next_fire_time(None, day) == day:
                    labels.append(label)
            return "、".join(labels)
        except ValueError:
            return ""

    def _schedule_form(self) -> dict[str, Any]:
        """生成计划模式、分组时间输入及随输入更新的摘要。"""
        def time_group(key: str, label: str, error: dict | None = None) -> dict:
            fields = []
            for part, name, count in (("hour", "小时", 24), ("minute", "分钟", 60)):
                fields.append({"component": "VCol", "props": {"cols": 6}, "content": [
                    {"component": "VSelect", "props": {
                        "model": f"{key}_{part}", "label": name, "aria-label": f"{label}{name}",
                        "items": [f"{value:02d}" for value in range(count)], "hide-details": True,
                    }},
                ]})
            children = [
                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-medium mb-2"}, "text": label},
                {"component": "VRow", "props": {"dense": True}, "content": fields},
            ]
            if error is not None:
                children.append(error)
            return {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": children}

        def time_text(key: str) -> str:
            return f"(String(model.{key}_hour ?? '') + ':' + String(model.{key}_minute ?? ''))"

        start, end = time_text("window_start"), time_text("window_end")
        complete = f"(/^{_TIME_PATTERN}$/.test({start}) && /^{_TIME_PATTERN}$/.test({end}))"
        valid = f"({complete} && {start} < {end})"
        error = {"component": "p", "props": {
            "show": f"{{{{!{valid}}}}}", "class": "text-caption text-error mt-2 mb-0",
            "role": "alert", "textContent": (
                f"{{{{{complete} ? '结束时间须晚于开始时间，且在同一天。' : '请选择完整的开始和结束时间。'}}}}"
            ),
        }}
        # MoviePilot 的 Cron 输入和可视编辑器共用更新事件。只有实际改动星期才切换编号方式。
        edit_cron = r"""function(value) {
          const previous = String(model.cron ?? '').trim().split(/\s+/);
          const next = String(value ?? '').trim().split(/\s+/);
          if (next.length === 5 && (previous.length !== 5 || previous[4].toLowerCase() !== next[4].toLowerCase())) {
            model.cron_weekday_basis = 'sunday_zero';
          }
          model.cron = value;
        }"""
        cron_content = [self._col(
            "VCronField", "cron", "Cron 表达式", 12, placeholder="0 8 * * *",
            hint="北京时间 · 星期可填写 mon（周一）或 sun（周日）。",
            **{"persistent-hint": True, "onUpdate:modelValue": edit_cron},
        )]
        legacy_days = self._legacy_cron_weekdays()
        if legacy_days:
            cron_content.append({"component": "VCol", "props": {
                "cols": 12, "show": "{{model.cron_weekday_basis !== 'sunday_zero'}}",
                "class": "text-caption text-medium-emphasis pt-0",
            }, "content": [{"component": "p", "text": f"旧规则当前按{legacy_days}执行。修改星期时，0 表示周日。"}]})
        return self._card("签到计划", "mdi-clock-outline", [
            {"component": "VBtnToggle", "props": {
                "model": "schedule_mode", "mandatory": True, "color": "primary", "variant": "outlined",
                "rounded": "lg", "class": "w-100 h-auto mb-4", "aria-label": "计划模式",
            }, "content": [
                {"component": "VBtn", "props": {
                    "value": f"{{{{'{mode}'}}}}", "height": 44,
                    "class": "flex-grow-1 text-none text-body-2 font-weight-medium",
                    "type": "button", "aria-pressed": f"{{{{schedule_mode === '{mode}'}}}}",
                }, "text": label}
                for mode, label in (("window", "随机时段"), ("cron", "Cron 定时"))
            ]},
            {"component": "VRow", "props": {"show": "{{schedule_mode === 'cron'}}"}, "content": cron_content},
            {"component": "div", "props": {"show": "{{schedule_mode === 'window'}}"}, "content": [
                {"component": "VRow", "props": {"dense": True}, "content": [
                    time_group("window_start", "开始时间"), time_group("window_end", "结束时间", error),
                ]},
                {"component": "p", "props": {
                    "show": f"{{{{{valid}}}}}", "class": "text-caption text-medium-emphasis mt-3 mb-0",
                    "textContent": f"{{{{'每天 ' + {start} + '–' + {end} + ' 随机签到一次 · 北京时间'}}}}",
                }},
            ]},
        ])

    def _account_form(self) -> dict[str, Any]:
        """呈现登录方式、实时验证结果和可折叠的网络与浏览器设置。"""
        verify_path = json.dumps(f"plugin/{self.__class__.__name__}/cookiecloud_verify")
        state_key = json.dumps(_VERIFY_STATE_KEY)
        state = f"model[Symbol.for({state_key})]"
        agent = "String(model.user_agent || '').trim()"
        proxy_enabled = "(model.use_proxy === true)"
        verify_click = f"""async function() {{
          const key = Symbol.for({state_key});
          if (model.cookiecloud !== true || model[key]?.loading) return;

          const network = {{use_proxy: {proxy_enabled}, user_agent: {agent}}};
          model[key] = {{network, loading: true}};
          try {{
            const result = await window.MoviePilotAPI.post(
              {verify_path}, network, {{feedback: 'silent', timeout: 60000}});
            if (typeof result?.success !== 'boolean' || typeof result.message !== 'string') {{
              throw new TypeError('Unexpected verification response');
            }}
            const type = result.success ? 'success' :
              ['signed_out', 'unknown', 'cookie_missing', 'proxy_missing'].includes(result.status) ? 'warning' : 'error';
            model[key] = {{network, loading: false, type, message: result.message}};
          }} catch (_) {{
            model[key] = {{network, loading: false, type: 'error',
              message: '未收到验证结果。请检查 MoviePilot 连接后重试。'}};
          }}
        }}"""
        current_result = (
            f"{state}?.message && {state}.network.use_proxy === {proxy_enabled} "
            f"&& {state}.network.user_agent === {agent}"
        )
        return self._card("登录与网络", "mdi-shield-account-outline", [
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-medium mb-2"}, "text": "登录方式"},
            {"component": "VBtnToggle", "props": {
                "model": "cookiecloud", "mandatory": True, "color": "primary", "variant": "outlined",
                "density": "comfortable", "rounded": "lg", "class": "w-100 h-auto flex-column flex-sm-row mb-4",
                "aria-label": "登录方式",
            }, "content": [
                {"component": "VBtn", "props": {
                    "value": True, "prepend-icon": "mdi-cloud-outline", "height": 44,
                    "class": "flex-grow-1 text-none text-body-2 font-weight-medium",
                    "type": "button", "aria-pressed": "{{cookiecloud}}",
                }, "text": "CookieCloud"},
                {"component": "VBtn", "props": {
                    "value": False, "prepend-icon": "mdi-key-outline", "height": 44,
                    "class": "flex-grow-1 text-none text-body-2 font-weight-medium",
                    "type": "button", "aria-pressed": "{{!cookiecloud}}",
                }, "text": "手动 Cookie"},
            ]},
            {"component": "div", "props": {"show": "{{!cookiecloud}}", "class": "mb-4"}, "content": [
                {"component": "VTextField", "props": {
                    "model": "cookie", "label": "NodeSeek Cookie", "type": "password", "autocomplete": "off",
                    "hide-details": True, "variant": "outlined",
                }},
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                 "text": "粘贴已登录浏览器的 NodeSeek Cookie。Cookie 是登录凭据，请妥善保管。"},
            ]},
            {"component": "div", "props": {"show": "{{cookiecloud}}", "class": "mb-4"}, "content": [
                {"component": "p", "props": {"class": "text-body-2 text-medium-emphasis mb-3"},
                 "text": "使用 MoviePilot 的 CookieCloud。验证采用下方当前设置，仅检查登录。"},
                {"component": "VBtn", "props": {
                    "color": "primary", "variant": "tonal", "class": "text-none", "type": "button",
                    "prepend-icon": "mdi-shield-check-outline", "onclick": verify_click,
                    "loading": f"{{{{Boolean({state}?.loading)}}}}", "disabled": f"{{{{Boolean({state}?.loading)}}}}",
                    "aria-busy": f"{{{{Boolean({state}?.loading)}}}}",
                }, "text": "验证 CookieCloud 登录"},
                {"component": "div", "props": {"role": "status", "aria-live": "polite"}, "content": [
                    {"component": "p", "props": {
                        "show": f"{{{{Boolean({state}?.loading)}}}}", "class": "text-caption mt-2",
                    }, "text": "正在验证登录…"},
                    {"component": "VAlert", "props": {
                        "show": f"{{{{Boolean({current_result})}}}}", "type": f"{{{{{state}?.type || 'info'}}}}",
                        "variant": "tonal", "density": "compact", "class": "mt-3",
                    }, "content": [
                        {"component": "span", "props": {"textContent": f"{{{{{state}?.message || ''}}}}"}},
                    ]},
                ]},
            ]},
            {"component": "VExpansionPanels", "props": {"variant": "accordion"}, "content": [
                {"component": "VExpansionPanel", "props": {"elevation": 0, "class": "border rounded-lg"}, "content": [
                    {"component": "VExpansionPanelTitle", "props": {"class": "px-4"}, "content": [
                        {"component": "VIcon", "props": {"size": "small", "class": "mr-2"},
                         "text": "mdi-lan-connect"},
                        {"component": "span", "props": {"class": "font-weight-medium"}, "text": "代理与 User-Agent"},
                        {"component": "VChip", "props": {"size": "small", "variant": "tonal", "class": "ml-3"},
                         "content": [{"component": "span", "props": {
                             "textContent": "{{use_proxy ? '使用系统代理' : '直连'}}",
                         }}]},
                    ]},
                    {"component": "VExpansionPanelText", "content": [
                        {"component": "VSwitch", "props": {
                            "model": "use_proxy", "label": "使用 MoviePilot 系统代理", "color": "primary",
                            "hide-details": True,
                        }},
                        {"component": "VTextField", "props": {
                            "model": "user_agent", "label": "浏览器 User-Agent", "variant": "outlined",
                            "hint": "可选。留空使用宿主浏览器的默认值。", "persistent-hint": True,
                            "maxlength": 1024, "class": "mt-2",
                        }},
                    ]},
                ]},
            ]},
        ])

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回分组的 Vuetify JSON 配置表单，兼容既有模型。"""
        col = self._col
        row = lambda fields: {"component": "VRow", "content": fields}
        basic = self._card("签到设置", "mdi-calendar-check", [row([
            col("VSwitch", "enabled", "启用定时签到", 6, color="primary"),
            col("VSwitch", "onlyonce", "保存后立即签到一次", 6, color="success"),
            col("VSwitch", "random", "随机奖励（关闭为固定奖励）", 6, color="primary"),
            col("VSwitch", "notify", "发送签到通知", 6, color="primary"),
        ])])
        defaults = dict(self._defaults)
        for key in ("window_start", "window_end"):
            value = self._window_value(self._config, key)
            if isinstance(value, str) and re.fullmatch(_TIME_PATTERN, value):
                defaults[f"{key}_hour"], defaults[f"{key}_minute"] = value.split(":")
            else:
                defaults[f"{key}_hour"] = None
                defaults[f"{key}_minute"] = None
        return [{"component": "VForm", "content": [basic, self._schedule_form(), self._account_form()]}], defaults

    def _schedule_overview(self) -> tuple[str, str]:
        """读取实际登记的签到任务，避免页面访问影响随机计划。"""
        if not self.get_state():
            return "自动签到已关闭", ""
        try:
            self._schedule_trigger()
        except ValueError:
            return "计划无效", "请检查签到计划中的时间或 Cron 表达式。"
        job_id = f"{self.__class__.__name__}_{_SIGN_SERVICE_ID}"
        try:
            job = next((item for item in list_scheduler_jobs() if item.id == job_id), None)
        except Exception:
            logger.warning("NodeSeek 无法读取已登记的签到任务")
            return "自动签到已开启", "暂时无法读取执行时间"
        if job is None:
            if not self._config.get("cookiecloud") and not self._cookies(str(self._config.get("cookie") or "")):
                return "签到任务未登记", "手动模式缺少可用 Cookie。"
            return "签到任务未登记", ""
        if job.next_run:
            return "自动签到已开启", str(job.next_run)
        return "自动签到已开启", str(job.status or "")

    @staticmethod
    def _overview_cell(title: str, value: str, icon: str, color: str, *details: str) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
            {"component": "VCard", "props": {"variant": "tonal", "color": color, "class": "pa-3 h-100"}, "content": [
                {"component": "div", "props": {"class": "d-flex align-center text-caption"},
                 "content": [{"component": "VIcon", "props": {"size": "small", "class": "mr-2"}, "text": icon},
                             {"component": "span", "text": title}]},
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold mt-2"}, "text": value},
                *[
                    {"component": "div", "props": {"class": "text-caption mt-1 text-wrap"}, "text": detail}
                    for detail in details if detail
                ],
            ]},
        ]}

    def _history_table(self, records: list[SignRecord]) -> dict:
        rows = []
        for record in records:
            label = self._record_status(record)
            description = label if record["message"] in {label, "今日已签到"} else f"{label} · {record['message']}"
            rows.append({"component": "tr", "content": [
                {"component": "td", "props": {"class": "text-caption text-wrap"}, "text": self._format_time(record["time"])},
                {"component": "td", "props": {"class": "text-wrap", "style": {"overflow-wrap": "anywhere"}},
                 "text": description},
                {"component": "td", "props": {"class": "text-end text-no-wrap px-2"}, "text": self._reward_text(record)},
            ]})
        return {"component": "VTable", "props": {"density": "compact"}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "props": {"style": {"width": "116px"}}, "text": "时间"},
                {"component": "th", "text": "结果"},
                {"component": "th", "props": {"class": "text-end px-2", "style": {"width": "80px"}}, "text": "奖励"},
            ]}]},
            {"component": "tbody", "content": rows},
        ]}

    def _history_page(self, records: list[SignRecord]) -> dict:
        content = [{"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-2"},
                    "text": f"最近 {_HISTORY_DAYS} 天 · 北京时间"}]
        if not records:
            content.append({"component": "p", "props": {"class": "text-body-2 text-medium-emphasis mb-0"},
                            "text": "暂无签到记录"})
        elif len(records) <= _HISTORY_PAGE_SIZE:
            content.append(self._history_table(records))
        else:
            pages = []
            for start in range(0, len(records), _HISTORY_PAGE_SIZE):
                page = start // _HISTORY_PAGE_SIZE + 1
                end = min(start + _HISTORY_PAGE_SIZE, len(records))
                pages.append({"component": "VExpansionPanel", "props": {"value": page, "elevation": 0}, "content": [
                    {"component": "VExpansionPanelTitle", "text": f"第 {page} 页 · {start + 1}–{end} 条"},
                    {"component": "VExpansionPanelText", "content": [self._history_table(records[start:end])]},
                ]})
            content.append({"component": "VExpansionPanels", "props": {"modelValue": 1, "variant": "accordion"},
                            "content": pages})
        return self._card("签到历史", "mdi-history", content)

    def get_page(self) -> list[dict]:
        """显示签到快照和实际登记的调度任务，页面查询只读。"""
        today = datetime.now(_BEIJING).date()
        with self._history_lock:
            _, _, history_state = self._read_sign_state(today)
        records = history_state["records"]
        today_text = today.isoformat()
        recent_today = next((record for record in records if record["day"] == today_text), None)
        if history_state["confirmed_day"] == today_text:
            today_status, today_color = "已确认签到", "success"
        elif recent_today is not None and self._record_status(recent_today) == "失败":
            today_status, today_color = "今日最近一次失败", "error"
        elif recent_today is not None:
            today_status, today_color = "今日最近一次未确认", "warning"
        else:
            today_status, today_color = "今日暂无确认记录", "info"

        latest = records[0] if records else None
        latest_status = self._record_status(latest) if latest else "暂无签到记录"
        latest_details = []
        if latest:
            if latest["message"] not in {latest_status, "今日已签到"}:
                latest_details.append(latest["message"])
            latest_details.extend([self._format_time(latest["time"]), f"奖励：{self._reward_text(latest)}"])
        state, schedule_detail = self._schedule_overview()
        next_value, next_detail = (schedule_detail, state) if state == "自动签到已开启" and schedule_detail else (
            state, schedule_detail
        )
        overview = self._card("签到概览", "mdi-chart-box-outline", [
            {"component": "VRow", "props": {"dense": True}, "content": [
                self._overview_cell("今日签到", today_status, "mdi-calendar-check-outline", today_color),
                self._overview_cell("最近一次", latest_status, "mdi-clock-outline", "primary", *latest_details),
                self._overview_cell(
                    "下次执行", next_value, "mdi-calendar-clock", "warning" if state == "计划无效" else "primary",
                    next_detail,
                ),
            ]},
        ])
        return [overview, self._history_page(records)]

    def stop_service(self) -> None:
        """取消历史清理和尚未执行的一次性签到。"""
        with self._history_lock:
            self._history_session = None
            if self._history_timer is not None:
                self._history_timer.cancel()
                self._history_timer = None
        try:
            remove_plugin_once_job(self.__class__.__name__, "once")
        except RuntimeError:
            pass
