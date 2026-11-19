"""基于 MoviePilot V3 浏览器能力的 NodeSeek 每日签到插件。"""

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
import json
import random
import re
import threading
from threading import Timer
from time import monotonic
from typing import Any, NamedTuple, TypedDict
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
from app.sdk.scheduler import add_plugin_once_job, list_scheduler_jobs, remove_plugin_once_job, update_plugin_job
from app.sdk.security import verify_token
from app import schemas
from app.schemas.types import MessageType


_BASE_URL = "https://www.nodeseek.com"
_SITE_HOST = urlsplit(_BASE_URL).netloc
_BEIJING = timezone(timedelta(hours=8))
_LOGIN_STATE_KEY = "login_validation"
_RECHECK_FIELD = "_nodeseek_verify_on_save"
_RECENT_LOGIN_SECONDS = 300
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
    "manual_missing": "请先填写 NodeSeek Cookie。",
    "manual_invalid": "NodeSeek Cookie 格式无效，请重新复制。",
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


class ManualVerificationOptions(CookieCloudVerificationOptions):
    cookie: str = Field(default="", repr=False)


class ConfigLoginOptions(ManualVerificationOptions):
    cookiecloud: StrictBool
    force: StrictBool = False


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


class ScheduleOverview(NamedTuple):
    state: str
    label: str
    detail: str = ""
    next_time: datetime | None = None
    has_plan_today: bool | None = None


def _require_administrator(token: schemas.TokenPayload = Depends(verify_token)) -> None:
    """仅允许管理员验证 NodeSeek 登录。"""
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


class SignupCronTrigger(CronTrigger):
    """记录调度器选中的时间，供详情页读取。"""

    next_time: datetime | None = None

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime | None:
        self.next_time = super().get_next_fire_time(previous_fire_time, now)
        return self.next_time


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

    @property
    def next_time(self) -> datetime | None:
        return self._pending

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
        self._config_lock = threading.Lock()
        self._window_attempt_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._history_timer: Timer | None = None
        self._history_session: object | None = None
        self._running_day: date | None = None
        self._registered_trigger: SignupCronTrigger | RandomWindowTrigger | None = None
        self._login_failure = ""
        self._login_blocks_schedule = False
        self._recent_login_checks: dict[str, float] = {}

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
        """验证启用条件，再向宿主登记签到任务。"""
        self.stop_service()
        desired = {**self._defaults, **(config or {})}
        for key in ("_nodeseek_cloud_fetching", "_nodeseek_cloud_verifying"):
            desired.pop(key, None)
        recheck = desired.pop(_RECHECK_FIELD, None)
        for key in ("window_start", "window_end"):
            desired[key] = self._window_value(desired, key)

        enabled, once = desired["enabled"] is True, desired["onlyonce"] is True
        desired.update(enabled=enabled, onlyonce=False)
        with self._config_lock:
            binding = self._login_binding(desired)
            known = self._saved_login(binding)
            status, blocked_schedule = str(known.get("status") or ""), known.get("scheduled") is True
            if enabled or once:
                if recheck is not True and self._checked_recently(binding):
                    status = "authenticated"
                elif recheck is True or status not in ("authenticated", "signed_out"):
                    status = self._check_login(desired)
                blocked_schedule = (enabled or blocked_schedule) and status != "authenticated"
                if enabled or status != "authenticated":
                    self._save_login(binding, status, blocked_schedule)
                else:
                    self._clear_login()
                desired["enabled"] = enabled and status == "authenticated"
                if status != "authenticated":
                    logger.warning(f"NodeSeek 登录校验未通过，未登记签到：{status}")
            elif status == "authenticated" or not known:
                self._clear_login()
                status = ""
            self._login_failure = status if status in _VERIFICATION_MESSAGES and status != "authenticated" else ""
            self._login_blocks_schedule = bool(self._login_failure) and blocked_schedule
            self._config = desired
            if once or desired["enabled"] != enabled or recheck is not None:
                self._persist_config(desired)

        with self._history_lock:
            history_session = self._history_session = object()
        self._maintain_history(history_session)
        if once and status == "authenticated":
            if not add_plugin_once_job(self.__class__.__name__, "once", partial(self.sign, desired),
                                       "NodeSeek 立即签到", delay_seconds=3):
                logger.error("NodeSeek 一次性签到未能加入调度器")

    def get_state(self) -> bool:
        """返回周期签到开关。"""
        return self._config["enabled"] is True

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """不注册远程命令。"""
        return []

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        return "vue", "dist/assets"

    def get_api(self) -> list[dict[str, Any]]:
        """注册需要管理员身份的只读登录验证接口。"""
        return [
            {"path": path, "endpoint": endpoint, "methods": ["POST"], "auth": "bear",
             "dependencies": [Depends(_require_administrator)], "response_model": CookieCloudVerificationResult,
             "summary": title}
            for path, endpoint, title in (
                ("/cookiecloud_verify", self.verify_cookiecloud_login, "验证 CookieCloud 登录"),
                ("/manual_verify", self.verify_manual_login, "验证手动 Cookie"),
                ("/config_login_check", self.check_config_login, "保存前检查 NodeSeek 登录"),
            )
        ]

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
    def _use_cloud(config: dict[str, Any]) -> bool:
        return config.get("cookiecloud") is True

    def _login_binding(self, config: dict[str, Any]) -> str:
        """只保存验过的登录配置摘要，避免在验证状态中保存 Cookie。"""
        cloud = self._use_cloud(config)
        if cloud:
            credentials = [str(getattr(settings, key, "") or "")
                           for key in ("COOKIECLOUD_HOST", "COOKIECLOUD_KEY", "COOKIECLOUD_PASSWORD",
                                       "COOKIECLOUD_ENABLE_LOCAL")]
        else:
            credentials = str(config.get("cookie") or "").strip()
        use_proxy = config.get("use_proxy") is True
        proxy = str(getattr(settings, "PROXY_SERVER", "") or "") if use_proxy else None
        values = (cloud, credentials, proxy, str(config.get("user_agent") or "").strip())
        return sha256(json.dumps(values).encode("utf-8")).hexdigest()

    def _saved_login(self, binding: str) -> dict:
        state = self.get_data(_LOGIN_STATE_KEY)
        return state if isinstance(state, dict) and state.get("binding") == binding else {}

    def _save_login(self, binding: str, status: str, scheduled: bool) -> None:
        state = {"binding": binding, "status": status, "scheduled": scheduled}
        try:
            if self.get_data(_LOGIN_STATE_KEY) != state:
                self.save_data(_LOGIN_STATE_KEY, state)
                if self.get_data(_LOGIN_STATE_KEY) != state:
                    logger.warning("NodeSeek 登录校验状态保存失败")
        except Exception:
            logger.warning("NodeSeek 登录校验状态保存失败")

    def _clear_login(self) -> None:
        if self.get_data(_LOGIN_STATE_KEY) is not None:
            self.del_data(_LOGIN_STATE_KEY)

    def _persist_config(self, config: dict[str, Any]) -> None:
        try:
            if self.update_config(config) is not False:
                return
        except Exception:
            pass
        logger.error("NodeSeek 无法保存修正后的签到开关")

    def _login_source(self, config: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        if self._use_cloud(config):
            status, source = self._cloud_cookie()
            if not source:
                return ("cloud_unavailable" if status == "unavailable" else "cookie_missing"), []
        else:
            source = str(config.get("cookie") or "").strip()
            if not source:
                return "manual_missing", []
            if len(source) > 16384 or "\r" in source or "\n" in source:
                return "manual_invalid", []
        cookies = self._cookies(source)
        missing = "cookie_missing" if self._use_cloud(config) else "manual_invalid"
        return ("found", cookies) if any(item["value"] for item in cookies) else (missing, [])

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

    @classmethod
    def _verification_message(cls, status: str, config: dict[str, Any]) -> str:
        if not cls._use_cloud(config):
            if status == "signed_out":
                return "NodeSeek 返回未登录。请在浏览器重新登录，然后更新 NodeSeek Cookie。"
            if status == "unknown":
                return "无法确认 NodeSeek 登录状态。请检查浏览器登录并重新复制 Cookie。"
        return _VERIFICATION_MESSAGES[status]

    @classmethod
    def _verification_result(cls, status: str, config: dict[str, Any]) -> CookieCloudVerificationResult:
        """记录固定状态，避免把站点响应或浏览器异常写进日志。"""
        source = "CookieCloud" if cls._use_cloud(config) else "手动 Cookie"
        logger.info(f"NodeSeek {source} 验证结果：{status}")
        return {
            "success": status == "authenticated",
            "status": status,
            "message": cls._verification_message(status, config),
        }

    def verify_cookiecloud_login(
        self, options: CookieCloudVerificationOptions | None = None
    ) -> CookieCloudVerificationResult:
        """使用本次表单网络设置检查 CookieCloud 登录状态。"""
        network = options.model_dump(exclude_none=True) if options is not None else {}
        config = {**self._config, **network, "cookiecloud": True}
        return self._verification_result(self._check_login(config), config)

    def verify_manual_login(self, options: ManualVerificationOptions) -> CookieCloudVerificationResult:
        """使用草稿中的手动 Cookie 和网络设置，只读检查登录。"""
        config = {**self._config, **options.model_dump(exclude_none=True), "cookiecloud": False}
        return self._verification_result(self._check_login(config), config)

    def _checked_recently(self, binding: str) -> bool:
        return self._recent_login_checks.get(binding, 0) > monotonic()

    def check_config_login(self, options: ConfigLoginOptions) -> CookieCloudVerificationResult:
        """在宿主写入设置前验证草稿。成功结果仅在本进程内短期有效。"""
        draft = options.model_dump(exclude_none=True, exclude={"force"})
        config = {**self._config, **draft}
        binding = self._login_binding(config)
        with self._config_lock:
            reusable = not options.force and (
                self._checked_recently(binding) or self._saved_login(binding).get("status") == "authenticated"
            )
        if reusable:
            return {"success": True, "status": "authenticated", "message": _VERIFICATION_MESSAGES["authenticated"]}

        status = self._check_login(config)
        with self._config_lock:
            self._recent_login_checks.pop(binding, None)
            if status == "authenticated":
                now = monotonic()
                self._recent_login_checks = {
                    key: expires for key, expires in self._recent_login_checks.items() if expires > now
                }
                if len(self._recent_login_checks) >= 16:
                    del self._recent_login_checks[next(iter(self._recent_login_checks))]
                self._recent_login_checks[binding] = now + _RECENT_LOGIN_SECONDS
        return self._verification_result(status, config)

    def _check_login(self, config: dict[str, Any]) -> str:
        status, cookies = self._login_source(config)
        if not cookies:
            return status
        user_agent = str(config.get("user_agent") or "").strip()
        browser_options: dict[str, Any] = {"headless": True, "service_workers": "block"}
        if user_agent:
            browser_options["user_agent"] = user_agent
        if config.get("use_proxy") is True:
            proxy = getattr(settings, "PROXY_SERVER", None)
            if not proxy:
                return "proxy_missing"
            browser_options["proxy"] = proxy

        try:
            context = launch_browser_context(**browser_options)
        except Exception:
            return "browser_error"
        try:
            context.add_cookies(cookies)
            context.route("**/*", self._route_verification_request)
            page = context.new_page()
            response = page.goto(_BASE_URL + "/", wait_until="domcontentloaded", timeout=15000)
            return self._verification_status(page, response)
        except Exception:
            return "network_error"
        finally:
            try:
                context.close()
            except Exception:
                logger.warning("NodeSeek 浏览器上下文关闭失败")

    def _schedule_trigger(self, *, register: bool = False) -> SignupCronTrigger | RandomWindowTrigger:
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
            return SignupCronTrigger.from_crontab(expression, timezone=_BEIJING)
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

    def _run_window_sign(self, trigger: RandomWindowTrigger, config: dict[str, Any]) -> None:
        """限制随机任务执行时间，手动发起的签到仍使用 sign。"""
        with self._window_attempt_lock:
            if config is not self._config or not self.get_state():
                return
            now = datetime.now(_BEIJING)
            if not trigger.is_due(now):
                logger.info("NodeSeek 随机签到已错过计划时段，本次跳过")
                return
            if self._window_attempt_date() == now.date():
                return
            self.save_data(_WINDOW_ATTEMPT_KEY, now.date().isoformat())
        self.sign(config)

    def _scheduled_sign(self, config: dict[str, Any]) -> None:
        if config is self._config and self.get_state():
            self.sign(config)

    def get_service(self) -> list[dict[str, Any]]:
        """按配置注册签到及独立的 CookieCloud 同步任务。"""
        services = []
        config = self._config
        self._registered_trigger = None
        if self._use_cloud(config):
            interval = getattr(settings, "COOKIECLOUD_INTERVAL", None)
            try:
                minutes = max(1, int(interval)) if interval else 1440
            except (TypeError, ValueError):
                minutes = 1440
            services.append({"id": "NodeSeekV3CookieCloud", "name": "NodeSeek CookieCloud 同步",
                             "trigger": IntervalTrigger(minutes=minutes, timezone=_BEIJING),
                             "func": self.sync_cookiecloud, "kwargs": {}})
        if not self.get_state():
            return services
        try:
            trigger = self._schedule_trigger(register=True)
        except ValueError:
            logger.error("NodeSeek 签到计划无效，请检查 Cron 或随机时段设置")
            return services
        self._registered_trigger = trigger
        action = (partial(self._run_window_sign, trigger, config) if isinstance(trigger, RandomWindowTrigger)
                  else partial(self._scheduled_sign, config))
        services.insert(0, {"id": _SIGN_SERVICE_ID, "name": "NodeSeek 论坛签到",
                            "trigger": trigger, "func": action, "kwargs": {}})
        return services

    def sync_cookiecloud(self) -> None:
        """定时刷新云端快照，不覆盖用户正在编辑的整份插件配置。"""
        if not self._use_cloud(self._config):
            return
        status, source = self._cloud_cookie()
        if status != "found" or source is None or not self._use_cloud(self._config):
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
            output.update(message="站点拒绝访问，请检查 Cookie 或站点验证", login_expired=True)
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
            if auth_error:
                output.update(message="站点未识别登录状态，请检查 Cookie 是否有效", login_expired=True)
            elif (result.get("ok") and (result.get("success") is True or already or explicit_gain)) or result.get("confirmed"):
                output.update(success=True, message="今日已签到" if already else "签到成功", gain=gain)
            else:
                suffix = f"（HTTP {status}）" if isinstance(status, int) else ""
                output["message"] = "签到未确认" + suffix
        return output

    def _request_sign(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一次浏览器签到，不向日志传递远端正文或凭据。"""
        config = self._config if config is None else config
        status, cookies = self._login_source(config)
        if not cookies:
            return {"success": False, "message": self._verification_message(status, config), "gain": None}
        options: dict[str, Any] = {"headless": True}
        user_agent = str(config.get("user_agent") or "").strip()
        if user_agent:
            options["user_agent"] = user_agent
        if config.get("use_proxy") is True:
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
                return {"success": False, "message": diagnostic, "gain": None,
                        "login_expired": getattr(response, "status", None) == 401 and self._verification_status(page, response) == "signed_out"}
            stage = "请求签到接口"
            result = page.evaluate(_SCRIPT, bool(config.get("random")))
        except Exception:
            return {"success": False, "message": f"{stage}失败，请检查浏览器环境、网络或站点验证", "gain": None}
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.warning("NodeSeek 浏览器上下文关闭失败")
        return self._interpret_result(result)

    def _pause_for_expired_login(self, config: dict[str, Any]) -> None:
        with self._config_lock:
            if config is not self._config:
                return
            was_enabled = self.get_state()
            binding = self._login_binding(config)
            self._recent_login_checks.pop(binding, None)
            self._save_login(binding, "signed_out", was_enabled)
            self._login_failure, self._login_blocks_schedule = "signed_out", was_enabled
            # 这里仅记录失效状态，避免覆盖管理员正在保存的完整配置。
            self._config = {**config, "enabled": False}
        if was_enabled:
            logger.warning("NodeSeek 登录已失效，定时签到已暂停")
            try:
                update_plugin_job(self.__class__.__name__)
            except Exception:
                logger.warning("NodeSeek 签到已暂停，调度器刷新失败")

    def sign(self, config: dict[str, Any] | None = None) -> None:
        """串行执行签到，仅在成功确认后记录北京时间日期。"""
        if not self._lock.acquire(blocking=False):
            logger.warning("NodeSeek 签到正在运行，本次跳过")
            return
        try:
            if config is not None and config is not self._config:
                return
            config = self._config if config is None else config
            sign_day = datetime.now(_BEIJING).date()
            with self._history_lock:
                _, _, state = self._read_sign_state(sign_day)
            if state["confirmed_day"] == sign_day.isoformat():
                logger.info("NodeSeek 今日已确认签到，跳过重复执行")
                return
            self._running_day = sign_day
            result = self._request_sign(config)
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
            if result.get("login_expired") is True:
                self._pause_for_expired_login(config)
            if config.get("notify"):
                self.post_message(mtype=MessageType.SiteMessage, title="NodeSeek 论坛签到", text=text)
        finally:
            self._running_day = None
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
        if record["success"] is None or record["message"].startswith("签到未确认") or (
            record["message"] == "站点返回了无法识别的签到结果"
        ):
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

        error = {"component": "p", "show": "windowError", "bind": {"textContent": "windowError"},
                 "props": {"class": "text-caption text-error mt-2 mb-0", "role": "alert"}}
        cron_content = [self._col(
            "VCronField", "cron", "Cron 表达式", 12, placeholder="0 8 * * *",
            hint="北京时间 · 星期可填写 mon（周一）或 sun（周日）。",
            **{"persistent-hint": True},
        )]
        legacy_days = self._legacy_cron_weekdays()
        if legacy_days:
            cron_content.append({"component": "VCol", "show": "legacyCronDays", "props": {
                "cols": 12,
                "class": "text-caption text-medium-emphasis pt-0",
            }, "content": [{"component": "p", "text": f"旧规则当前按{legacy_days}执行。修改星期时，0 表示周日。"}]})
        return self._card("签到计划", "mdi-clock-outline", [
            {"component": "VBtnToggle", "props": {
                "model": "schedule_mode", "mandatory": True, "color": "primary", "variant": "outlined",
                "rounded": "lg", "class": "w-100 h-auto mb-4", "aria-label": "计划模式",
            }, "content": [
                {"component": "VBtn", "bind": {"aria-pressed": f"{mode}Mode"}, "props": {
                    "value": mode, "height": 44,
                    "class": "flex-grow-1 text-none text-body-2 font-weight-medium",
                    "type": "button",
                }, "text": label}
                for mode, label in (("window", "随机时段"), ("cron", "Cron 定时"))
            ]},
            {"component": "VRow", "show": "cronMode", "content": cron_content},
            {"component": "div", "show": "windowMode", "content": [
                {"component": "VRow", "props": {"dense": True}, "content": [
                    time_group("window_start", "开始时间"), time_group("window_end", "结束时间", error),
                ]},
                {"component": "p", "show": "windowSummary", "bind": {"textContent": "windowSummary"},
                 "props": {"class": "text-caption text-medium-emphasis mt-3 mb-0"}},
            ]},
        ])

    def _account_form(self) -> dict[str, Any]:
        """呈现登录方式、验证结果和代理设置。"""
        return self._card("登录与网络", "mdi-shield-account-outline", [
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-medium mb-2"}, "text": "登录方式"},
            {"component": "VBtnToggle", "props": {
                "model": "cookiecloud", "mandatory": True, "color": "primary", "variant": "outlined",
                "density": "comfortable", "rounded": "lg", "class": "w-100 h-auto flex-column flex-sm-row mb-4",
                "aria-label": "登录方式",
            }, "content": [
                {"component": "VBtn", "bind": {"aria-pressed": active}, "props": {
                    "value": value, "prepend-icon": icon, "height": 44,
                    "class": "flex-grow-1 text-none text-body-2 font-weight-medium", "type": "button",
                }, "text": label}
                for value, icon, active, label in (
                    (True, "mdi-cloud-outline", "useCloud", "CookieCloud"),
                    (False, "mdi-key-outline", "useManual", "手动 Cookie"),
                )
            ]},
            {"component": "div", "show": "useManual", "content": [
                {"component": "VTextField", "props": {
                    "model": "cookie", "label": "NodeSeek Cookie", "type": "password", "autocomplete": "off",
                    "hide-details": True, "variant": "outlined",
                }},
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                 "text": "粘贴已登录浏览器的 NodeSeek Cookie。Cookie 是登录凭据，请妥善保管。"},
            ]},
            {"component": "p", "show": "useCloud", "props": {"class": "text-body-2 text-medium-emphasis mb-0"},
             "text": "使用 MoviePilot 的 CookieCloud。"},
            {"component": "VBtn", "action": "verifyLogin",
             "bind": {"text": "verifyLabel", "loading": "verifying", "disabled": "busy"},
             "props": {"color": "primary", "variant": "tonal", "class": "text-none mt-3", "type": "button",
                       "prepend-icon": "mdi-shield-check-outline"}},
            {"component": "div", "props": {"role": "status", "aria-live": "polite", "data-login-feedback": "",
                                          "tabindex": -1, "class": "mb-4"}, "content": [
                {"component": "VAlert", "show": "loginMessage", "bind": {"type": "loginAlertType", "text": "loginMessage"},
                 "props": {"variant": "tonal", "density": "compact", "class": "mt-3"}},
            ]},
            {"component": "VExpansionPanels", "props": {"variant": "accordion"}, "content": [
                {"component": "VExpansionPanel", "props": {"elevation": 0, "class": "border rounded-lg"}, "content": [
                    {"component": "VExpansionPanelTitle", "props": {"class": "px-4"}, "content": [
                        {"component": "VIcon", "props": {"size": "small", "class": "mr-2"},
                         "text": "mdi-lan-connect"},
                        {"component": "span", "props": {"class": "font-weight-medium"}, "text": "代理与 User-Agent"},
                        {"component": "VChip", "bind": {"text": "proxyLabel"},
                         "props": {"size": "small", "variant": "tonal", "class": "ml-3"}},
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
        """返回配置页布局、初始登录状态与兼容既有设置的默认值。"""
        col = self._col
        basic = self._card("签到设置", "mdi-calendar-check", [
            {"component": "VRow", "content": [
                col("VSwitch", "enabled", "启用定时签到", 6, color="primary"),
                col("VSwitch", "onlyonce", "保存后立即签到一次", 6, color="success"),
                col("VSwitch", "random", "随机奖励（关闭为固定奖励）", 6, color="primary"),
                col("VSwitch", "notify", "发送签到通知", 6, color="primary"),
            ]},
            {"component": "p", "show": "needsLogin", "props": {"class": "text-caption text-medium-emphasis mb-0"},
             "text": "保存时会验证当前登录方式。"},
        ])
        defaults = dict(self._defaults)
        for key in ("window_start", "window_end"):
            value = self._window_value(self._config, key)
            if isinstance(value, str) and re.fullmatch(_TIME_PATTERN, value):
                defaults[f"{key}_hour"], defaults[f"{key}_minute"] = value.split(":")
            else:
                defaults[f"{key}_hour"] = None
                defaults[f"{key}_minute"] = None
        notice = self._verification_message(self._login_failure, self._config) if self._login_failure else ""
        return [{
            "component": "VForm",
            "enabled": self.get_state(),
            "login_notice": notice,
            "content": [basic, self._schedule_form(), self._account_form()],
        }], defaults

    def _schedule_overview(self, now: datetime) -> ScheduleOverview:
        """读取实际登记的签到任务，避免页面访问影响随机计划。"""
        if self._login_blocks_schedule:
            expired = self._login_failure == "signed_out"
            label = "登录已失效" if expired else "待验证登录"
            return ScheduleOverview("blocked", label, self._verification_message(self._login_failure, self._config))
        if not self.get_state():
            return ScheduleOverview("off", "自动签到已关闭")
        try:
            plan = self._schedule_trigger()
        except ValueError:
            return ScheduleOverview("blocked", "计划无效", "请检查签到计划")
        job_id = f"{self.__class__.__name__}_{_SIGN_SERVICE_ID}"
        try:
            job = next((item for item in list_scheduler_jobs() if item.id == job_id), None)
        except Exception:
            logger.warning("NodeSeek 无法读取已登记的签到任务")
            return ScheduleOverview("unavailable", "暂时无法读取")
        if job is None:
            return ScheduleOverview("blocked", "暂无执行计划")

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if isinstance(plan, SignupCronTrigger):
            first = plan.get_next_fire_time(None, day_start)
            has_plan = first is not None and first.date() == now.date()
        else:
            has_plan = True
        registered = self._registered_trigger
        next_time = registered.next_time if registered is not None else None
        if next_time is None or next_time < now:
            if job.next_run and next_time is None:
                return ScheduleOverview("scheduled", str(job.next_run))
            return ScheduleOverview("scheduled", "暂无执行时间", has_plan_today=has_plan if not has_plan else None)
        next_time = next_time.astimezone(_BEIJING)
        days = (next_time.date() - now.date()).days
        prefix = ("今天", "明天")[days] if days in (0, 1) else next_time.strftime("%m-%d")
        return ScheduleOverview("scheduled", f"{prefix} {next_time:%H:%M}", next_time=next_time, has_plan_today=has_plan)

    @staticmethod
    def _overview_cell(title: str, value: str, icon: str, color: str, *details: str) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": [
            {"component": "VCard", "props": {"variant": "tonal", "color": color, "class": "pa-3 h-100"}, "content": [
                {"component": "div", "props": {"class": "d-flex align-center text-caption"},
                 "content": [{"component": "VIcon", "props": {"size": "small", "class": "mr-2"}, "text": icon},
                             {"component": "span", "text": title}]},
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold mt-2"}, "text": value},
                *[
                    {"component": "div", "props": {"class": "text-caption mt-1"}, "text": detail}
                    for detail in details if detail
                ],
            ]},
        ]}

    def _history_table(self, records: list[SignRecord]) -> dict:
        rows = []
        for record in records:
            rows.append({"component": "tr", "content": [
                {"component": "td", "props": {"style": "white-space: nowrap;"}, "text": self._format_time(record["time"])},
                {"component": "td", "props": {"style": {"overflow-wrap": "anywhere"}}, "text": record["message"]},
                {"component": "td", "text": self._reward_text(record)},
            ]})
        return {"component": "VTable", "props": {"density": "comfortable", "fixed-header": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": "时间（北京时间）"},
                {"component": "th", "text": "结果"},
                {"component": "th", "text": "奖励"},
            ]}]},
            {"component": "tbody", "content": rows},
        ]}

    def _history_page(self, records: list[SignRecord]) -> dict:
        if not records:
            content = [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "text": "暂无签到记录",
            }}]
        elif len(records) <= _HISTORY_PAGE_SIZE:
            content = [self._history_table(records)]
        else:
            pages, buttons = [], []
            for start in range(0, len(records), _HISTORY_PAGE_SIZE):
                page = start // _HISTORY_PAGE_SIZE + 1
                pages.append({"component": "VStepperWindowItem", "props": {
                    "value": page, "transition": False, "reverse-transition": False,
                }, "content": [self._history_table(records[start:start + _HISTORY_PAGE_SIZE])]})
                buttons.append({"component": "VStepperItem", "props": {
                    "value": page, "editable": True, "edit-icon": "", "class": "pa-2 flex-grow-0",
                    "aria-label": f"第 {page} 页", "type": "button",
                }})
            # PageRender 不传递插槽状态。页码和表格共用 Vuetify 的当前步骤。
            content = [{"component": "VStepper", "props": {"flat": True, "modelValue": 1}, "content": [
                {"component": "VStepperWindow", "props": {"class": "ma-0"}, "content": pages},
                {"component": "nav", "props": {"class": "d-flex flex-wrap justify-center mt-2", "aria-label": "签到历史分页"},
                 "content": buttons},
            ]}]
        return self._card("签到历史", "mdi-history", content)

    def _today_overview(
        self, now: datetime, confirmed: bool, latest: SignRecord | None, schedule: ScheduleOverview
    ) -> tuple[str, str]:
        if confirmed:
            return "已签到", "success"
        if self._running_day == now.date():
            return "签到中", "info"
        if latest:
            return ("签到失败", "error") if self._record_status(latest) == "失败" else ("结果未确认", "warning")
        if schedule.next_time and schedule.next_time.date() == now.date():
            return "等待签到", "info"
        if schedule.has_plan_today:
            return "今日未执行", "warning"
        if schedule.state in ("blocked", "off") or schedule.has_plan_today is False:
            return "今日无计划", "info"
        return "暂无记录", "info"

    def get_page(self) -> list[dict]:
        """显示签到快照和实际登记的调度任务，页面查询只读。"""
        now = datetime.now(_BEIJING)
        with self._history_lock:
            _, _, history_state = self._read_sign_state(now.date())
        records = history_state["records"]
        today = now.date().isoformat()
        today_records = [record for record in records if record["day"] == today]
        confirmed = history_state["confirmed_day"] == today
        today_reward = next(
            (record["gain"] for record in today_records if record["success"] is True and record["gain"] is not None),
            None,
        )
        reward = f"{today_reward} 个鸡腿" if today_reward is not None else ("未知" if confirmed else "—")
        schedule = self._schedule_overview(now)
        today_status, today_color = self._today_overview(
            now, confirmed, today_records[0] if today_records else None, schedule,
        )
        latest = records[0] if records else None
        latest_time = datetime.fromisoformat(latest["time"]).strftime("%m-%d %H:%M") if latest else "暂无记录"
        latest_status = {"失败": "签到失败", "未确认": "结果未确认"}
        result = self._record_status(latest) if latest else ""
        overview = self._card("签到概览", "mdi-chart-box-outline", [
            {"component": "VRow", "content": [
                self._overview_cell("今日签到", today_status, "mdi-check-circle-outline", today_color),
                self._overview_cell("签到奖励", reward, "mdi-food-drumstick-outline", "warning"),
                self._overview_cell("最近签到", latest_time, "mdi-clock-outline", "primary", latest_status.get(result, result)),
                self._overview_cell(
                    "下次执行", schedule.label, "mdi-calendar-clock",
                    "warning" if schedule.state in ("blocked", "unavailable") else "primary", schedule.detail,
                ),
            ]},
        ])
        return [overview, self._history_page(records)]

    def stop_service(self) -> None:
        """取消历史清理和尚未执行的一次性签到。"""
        with self._config_lock:
            self._config = self._config.copy()
            self._registered_trigger = None
        with self._history_lock:
            self._history_session = None
            if self._history_timer is not None:
                self._history_timer.cancel()
                self._history_timer = None
        try:
            remove_plugin_once_job(self.__class__.__name__, "once")
        except RuntimeError:
            pass
