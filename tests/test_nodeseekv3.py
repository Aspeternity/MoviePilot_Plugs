"""NodeSeek 插件离线行为测试，不访问网络或真实配置。"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def plugin_module(monkeypatch):
    """隔离宿主依赖并加载待测插件。"""
    class FakeBase:
        """替代持久化基类，避免读取真实 MoviePilot 数据库。"""

        def __init__(self):
            """建立内存数据和通知列表。"""
            self.data = {}
            self.notifications = []

        def get_data(self, key):
            """读取测试数据。"""
            return self.data.get(key)

        def save_data(self, key, value):
            """保存测试数据。"""
            self.data[key] = value

        def update_config(self, config):
            """记录测试配置。"""
            self.saved_config = config.copy()

        def post_message(self, **kwargs):
            """记录测试通知。"""
            self.notifications.append(kwargs)

    class FakeLogger:
        """记录脱敏后的日志。"""

        def __init__(self):
            """初始化日志内容。"""
            self.messages = []

        def info(self, *args):
            """记录普通日志。"""
            self.messages.append(args)

        def warning(self, *args):
            """记录警告日志。"""
            self.messages.append(args)

        def error(self, *args):
            """记录错误日志。"""
            self.messages.append(args)

    def module(name, **exports):
        """插入隔离的 SDK 模块。"""
        value = types.ModuleType(name)
        value.__dict__.update(exports)
        monkeypatch.setitem(sys.modules, name, value)

    fake_logger = FakeLogger()
    module("app", __path__=[])
    module("app.sdk", __path__=[])
    module("app.sdk.browser", launch_browser_context=lambda **kwargs: None)
    module("app.sdk.config", settings=types.SimpleNamespace(PROXY_SERVER=None))
    module("app.sdk.logging", logger=fake_logger)
    module("app.sdk.plugin", _PluginBase=FakeBase)
    module("app.sdk.scheduler", add_plugin_once_job=lambda *args, **kwargs: True,
           remove_plugin_once_job=lambda *args: None)
    module("app.sdk.security", verify_token=lambda: None)
    module("app.schemas", TokenPayload=types.SimpleNamespace)
    module("app.schemas.types", MessageType=types.SimpleNamespace(SiteMessage="site"))
    spec = importlib.util.spec_from_file_location("test_nodeseek_plugin", ROOT / "plugins.v3/nodeseekv3/__init__.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class FakePage:
    """模拟浏览器页面及指定站点返回。"""

    def __init__(self, result, url="https://www.nodeseek.com/", status=200, headers=None):
        """配置页面结果和地址。"""
        self.result = result
        self.url = url
        self.evaluated = False
        self.response = types.SimpleNamespace(ok=200 <= status < 300, status=status, headers=headers or {})

    def set_default_timeout(self, timeout):
        """记录页面超时。"""
        self.timeout = timeout

    def goto(self, url, **kwargs):
        """模拟访问首页。"""
        return self.response

    def title(self):
        """返回正常页面标题。"""
        return "NodeSeek"

    def evaluate(self, script, random=None):
        """返回模拟签到结果。"""
        self.evaluated = True
        self.random = random
        return self.result


class FakeContext:
    """模拟可关闭的浏览器上下文。"""

    def __init__(self, page):
        """建立关联页面。"""
        self.page = page
        self.closed = False
        self.cookies = []

    def add_cookies(self, cookies):
        """记录限定域名的 Cookie。"""
        self.cookies = cookies

    def route(self, pattern, handler):
        """记录上下文的只读请求过滤器。"""
        self.route_handler = handler

    def new_page(self):
        """返回模拟页面。"""
        return self.page

    def close(self):
        """记录上下文已关闭。"""
        self.closed = True


def test_metadata_form_and_schedule(plugin_module):
    """核对 V3 元数据、表单模型和北京时间调度。"""
    plugin = plugin_module.NodeSeekV3()
    metadata = json.loads((ROOT / "package.v3.json").read_text())["NodeSeekV3"]
    assert metadata["version"] == plugin.plugin_version
    assert metadata["system_version"] == ">=3.2.8"
    form, defaults = plugin.get_form()
    assert form[0]["component"] == "VForm"
    assert defaults["cookie"] == ""
    assert not plugin.get_service()
    plugin._config.update(enabled=True, cron="0 8 * * *", cookie="session=example")
    trigger = plugin.get_service()[0]["trigger"]
    assert trigger.timezone.utcoffset(None) == timedelta(hours=8)
    plugin._config["cron"] = "invalid"
    assert plugin.get_service() == []


@pytest.mark.parametrize("result, expected", [
    ({"kind": "already", "gain": 5}, (True, "今日已签到", 5)),
    ({"kind": "already"}, (True, "今日已签到", None)),
    ({"kind": "result", "ok": True, "success": True, "gain": 5}, (True, "签到成功", 5)),
    ({"kind": "result", "ok": True, "message": "今天的签到收益是5个鸡腿"}, (True, "签到成功", 5)),
    ({"kind": "result", "ok": False, "confirmed": True}, (True, "签到成功", None)),
    ({"kind": "auth", "status": 401}, (False, "站点拒绝访问，请检查 Cookie 或站点验证", None)),
    ({"kind": "result", "ok": True, "success": False, "status": 200}, (False, "签到未确认（HTTP 200）", None)),
    (None, (False, "站点返回了无法识别的签到结果", None)),
])
def test_browser_results_and_cleanup(plugin_module, monkeypatch, result, expected):
    """覆盖成功、已有签到、认证失败、未确认和资源关闭。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config.update(cookie="session=mock-secret; other=example", random=False)
    page = FakePage(result)
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    outcome = plugin._request_sign()
    assert (outcome["success"], outcome["message"], outcome["gain"]) == expected
    assert context.closed
    assert page.evaluated and page.random is False
    assert all(cookie["url"] == "https://www.nodeseek.com" for cookie in context.cookies)
    assert "mock-secret" not in repr(plugin_module.logger.messages)


def test_redirect_and_exception_never_leak_cookie(plugin_module, monkeypatch):
    """拒绝跳转并确保异常详情及凭据不进入输出。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config["cookie"] = "session=mock-secret"
    page = FakePage({}, url="https://evil.example/")
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    assert plugin._request_sign()["success"] is False
    assert context.closed and not page.evaluated

    def fail(**kwargs):
        """模拟含敏感内容的启动异常。"""
        raise RuntimeError("mock-secret")

    monkeypatch.setattr(plugin_module, "launch_browser_context", fail)
    outcome = plugin._request_sign()
    assert not outcome["success"] and "mock-secret" not in outcome["message"]


def test_cookiecloud_uses_exact_group_per_sign_without_overwriting_manual(plugin_module, monkeypatch):
    """每次签到获取目标域分组，不注入相似域，也不覆盖手动配置。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"cookiecloud": True, "cookie": "session=manual-secret", "custom": "keep"})
    downloads = []
    def download():
        """返回递增的 NodeSeek 登录值和无关站点。"""
        downloads.append(1)
        return {"nodeseek.com": f"session=cloud-{len(downloads)}; cf_clearance=challenge",
                "nodeseek.com.evil.example": "session=bad-secret",
                "other.example": "session=other-secret"}, ""
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", download)
    contexts = []
    def launch(**kwargs):
        """每次创建独立的测试浏览器。"""
        context = FakeContext(FakePage({"kind": "already"}))
        contexts.append(context)
        return context
    monkeypatch.setattr(plugin_module, "launch_browser_context", launch)
    assert plugin._request_sign()["success"] and plugin._request_sign()["success"]
    assert [context.cookies[0]["value"] for context in contexts] == ["cloud-1", "cloud-2"]
    assert all(item["url"] == "https://www.nodeseek.com" for context in contexts for item in context.cookies)
    assert all("bad-secret" not in repr(context.cookies) and "other-secret" not in repr(context.cookies)
               for context in contexts)
    assert plugin._config["cookie"] == "session=manual-secret" and plugin._config["custom"] == "keep"
    assert not hasattr(plugin, "saved_config")
    assert "cloud-1" not in repr(plugin_module.logger.messages)


@pytest.mark.parametrize("response", [
    (None, "configured secret detail"),
    ({}, ""),
    ({"nodeseek.com.evil.example": "session=wrong-secret"}, ""),
    ({"www.nodeseek.com": "session=unaggregated-secret"}, ""),
    ({"nodeseek.com": ""}, ""),
    ({"nodeseek.com": "invalid cookie text"}, ""),
    ({"nodeseek.com": "session=cloud-secret"}, "configured secret detail"),
])
def test_cookiecloud_failure_does_not_fall_back_or_leak(plugin_module, monkeypatch, response):
    """异常、空值、相似域和不可解析内容都不得回退手动值。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config.update(cookiecloud=True, cookie="session=manual-secret")
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: response)
    assert plugin._sign_cookies() == []
    assert plugin._config["cookie"] == "session=manual-secret"
    assert "secret" not in repr(plugin_module.logger.messages)


def test_cookiecloud_exception_no_manual_and_disabled_legacy(plugin_module, monkeypatch):
    """同步异常不泄密；无备用时明确失败，旧配置不会调用云端。"""
    plugin = plugin_module.NodeSeekV3()
    def fail():
        """模拟带秘密的同步异常。"""
        raise RuntimeError("cloud-secret")
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", fail)
    plugin.init_plugin({"cookie": "session=manual-secret"})
    assert not plugin._config["cookiecloud"]
    assert plugin._sign_cookies()[0]["value"] == "manual-secret"
    assert not plugin_module.logger.messages
    plugin._config.update(cookiecloud=True, cookie="")
    result = plugin._request_sign()
    assert not result["success"] and "CookieCloud" in result["message"]
    assert "cloud-secret" not in repr(result) + repr(plugin_module.logger.messages)


def test_cookiecloud_host_facade_contract(plugin_module, monkeypatch):
    """调用宿主兼容入口并保留其返回结构，不接触真实配置。"""
    module = types.ModuleType("app.helper.cookiecloud")
    class Cloud:
        """模拟兼容入口提供的无参客户端。"""
        def download(self):
            """返回模拟的按域分组数据。"""
            return {"nodeseek.com": "session=example"}, ""
    module.CookieCloudHelper = Cloud
    monkeypatch.setitem(sys.modules, "app.helper.cookiecloud", module)
    assert plugin_module._download_cookiecloud() == ({"nodeseek.com": "session=example"}, "")


@pytest.mark.parametrize("observed,expected", [
    ("authenticated", "authenticated"), ("signed_out", "signed_out"),
    ("unknown", "unknown"), (None, "unknown"), (True, "unknown"),
])
def test_verify_login_requires_explicit_evidence_and_is_read_only(plugin_module, monkeypatch, observed, expected):
    """只有明确登录证据才成功，验证不执行签到或更改配置。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"cookie": "session=manual-secret", "cookiecloud": False, "enabled": True})
    before = plugin._config.copy()
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({"nodeseek.com": "session=cloud-secret"}, ""))
    page = FakePage(observed)
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    monkeypatch.setattr(plugin, "sign", lambda: pytest.fail("不得签到"))
    result = plugin.verify_cookiecloud_login()
    assert result["status"] == expected
    assert result["success"] is (expected == "authenticated") and context.closed and page.evaluated
    assert plugin._config == before and not plugin.data and not hasattr(plugin, "saved_config")
    assert "secret" not in repr(result) + repr(plugin_module.logger.messages)
    assert context.route_handler == plugin._route_verification_request


@pytest.mark.parametrize("method,url,resource_type,expected", [
    ("GET", "https://www.nodeseek.com/", "document", "allow"),
    ("GET", "https://www.nodeseek.com/assets/app.js", "script", "allow"),
    ("POST", "https://www.nodeseek.com/api/attendance", "fetch", "abort"),
    ("GET", "https://www.nodeseek.com/api/attendance", "fetch", "abort"),
    ("GET", "https://www.nodeseek.com/api/attendance", "script", "abort"),
    ("GET", "https://www.nodeseek.com/cdn-cgi/challenge-platform/script.js", "script", "abort"),
    ("GET", "https://www.nodeseek.com.example/assets/app.js", "script", "abort"),
])
def test_verification_routes_are_limited_to_reading_the_page(plugin_module, method, url, resource_type, expected):
    calls = []
    route = types.SimpleNamespace(
        request=types.SimpleNamespace(
            url=url, method=method, resource_type=resource_type,
            is_navigation_request=lambda: resource_type == "document",
        ),
        continue_=lambda: calls.append("allow"),
        abort=lambda: calls.append("abort"),
    )
    plugin_module.NodeSeekV3._route_verification_request(route)
    assert calls == [expected]


@pytest.mark.parametrize("response,expected", [
    ((None, "secret"), "cloud_unavailable"),
    (({}, ""), "cookie_missing"),
    (({"other.example": "session=secret"}, ""), "cookie_missing"),
    (({"nodeseek.com": "invalid cookie text"}, ""), "cookie_missing"),
])
def test_verify_cloud_failure_no_browser_or_leak(plugin_module, monkeypatch, response, expected):
    """云端读取失败不会启动浏览器，错误详情不会泄露。"""
    plugin = plugin_module.NodeSeekV3()
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: response)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: pytest.fail("不得启动浏览器"))
    result = plugin.verify_cookiecloud_login()
    assert result["status"] == expected
    assert not result["success"] and "secret" not in repr(result)


def test_verify_challenge_redirect_and_exception(plugin_module, monkeypatch):
    """验证页、重定向与异常不误判成功，且资源关闭。"""
    plugin = plugin_module.NodeSeekV3()
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({"nodeseek.com": "session=secret"}, ""))
    page = FakePage("authenticated", url="https://evil.example/")
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    assert plugin.verify_cookiecloud_login()["status"] == "redirected" and context.closed and not page.evaluated
    page = FakePage("authenticated")
    page.title = lambda: "Just a moment..."
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    assert plugin.verify_cookiecloud_login()["status"] == "blocked" and context.closed and not page.evaluated
    def fail(**kwargs):
        """模拟泄露敏感内容的浏览器异常。"""
        raise RuntimeError("secret")
    monkeypatch.setattr(plugin_module, "launch_browser_context", fail)
    result = plugin.verify_cookiecloud_login()
    assert result["status"] == "browser_error" and "secret" not in repr(result)


@pytest.mark.parametrize("status,headers,expected", [
    (401, {}, "signed_out"),
    (403, {}, "blocked"),
    (200, {"cf-mitigated": "challenge"}, "blocked"),
    (502, {}, "network_error"),
])
def test_verify_http_errors_do_not_read_page_login_marker(plugin_module, monkeypatch, status, headers, expected):
    plugin = plugin_module.NodeSeekV3()
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({"nodeseek.com": "session=example"}, ""))
    page = FakePage("authenticated", status=status, headers=headers)
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)

    result = plugin.verify_cookiecloud_login()

    assert result["status"] == expected and not result["success"]
    assert context.closed and not page.evaluated


def test_verify_uses_current_network_options_without_saving(plugin_module, monkeypatch):
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"use_proxy": True, "user_agent": "saved-agent"})
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({"nodeseek.com": "session=example"}, ""))
    calls = []

    def launch(**options):
        calls.append(options)
        return FakeContext(FakePage("authenticated"))

    monkeypatch.setattr(plugin_module, "launch_browser_context", launch)
    assert plugin.verify_cookiecloud_login()["status"] == "proxy_missing"
    assert not calls
    network = plugin_module.CookieCloudVerificationOptions(use_proxy=False, user_agent=" draft-agent ")
    assert plugin.verify_cookiecloud_login(network)["success"]
    assert calls[-1] == {"headless": True, "service_workers": "block", "user_agent": "draft-agent"}

    monkeypatch.setattr(plugin_module.settings, "PROXY_SERVER", "http://proxy.example:8080")
    network = plugin_module.CookieCloudVerificationOptions(user_agent="")
    assert plugin.verify_cookiecloud_login(network)["success"]
    assert calls[-1] == {"headless": True, "service_workers": "block", "proxy": "http://proxy.example:8080"}
    assert plugin._config["use_proxy"] is True and plugin._config["user_agent"] == "saved-agent"
    assert not hasattr(plugin, "saved_config")


def test_cookiecloud_fallback_does_not_mask_signin_result(plugin_module, monkeypatch):
    """同步失败时直接失败，不启动浏览器，也不使用或改写手动 Cookie。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config.update(cookiecloud=True, cookie="session=manual-secret")
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({}, ""))
    context = FakeContext(FakePage({"kind": "auth", "status": 401}))
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    outcome = plugin._request_sign()
    assert not outcome["success"] and "CookieCloud" in outcome["message"]
    assert not context.cookies and not context.page.evaluated
    assert plugin._config["cookie"] == "session=manual-secret"


def test_cookiecloud_verification_requires_admin(plugin_module):
    """验证接口要求管理员，且不再暴露获取 Cookie 接口。"""
    plugin = plugin_module.NodeSeekV3()
    route = next(r for r in plugin.get_api() if r["path"] == "/cookiecloud_verify")
    assert route["auth"] == "bear" and route["methods"] == ["POST"]
    assert route["dependencies"][0].dependency == plugin_module._require_administrator
    plugin_module._require_administrator(types.SimpleNamespace(super_user=True))
    with pytest.raises(plugin_module.HTTPException) as exc:
        plugin_module._require_administrator(types.SimpleNamespace(super_user=False))
    assert exc.value.status_code == 403


def test_cookiecloud_source_config_ui(plugin_module):
    """布尔来源模型兼容旧配置，页面按选择显示输入框或验证按钮。"""
    plugin = plugin_module.NodeSeekV3()
    form, defaults = plugin.get_form()
    cards = form[0]["content"]
    assert cards[1]["content"][0]["content"][1]["text"] == "签到计划"
    account = cards[2]["content"][2]["content"]
    toggle = next(node for node in account if node["component"] == "VBtnToggle")
    assert toggle["props"]["model"] == "cookiecloud"
    assert {button["props"]["value"] for button in toggle["content"]} == {False, True}
    manual = next(node for node in account if node["props"].get("show") == "{{!cookiecloud}}")
    assert manual["content"][0]["props"]["type"] == "password"
    cloud = next(node for node in account if node["props"].get("show") == "{{cookiecloud}}")
    button = next(node for node in cloud["content"] if node["component"] == "VBtn")
    assert button["text"] == "验证 CookieCloud 登录" and "cookiecloud_verify" in button["props"]["onclick"]
    assert any(node["component"] == "VExpansionPanels" for node in account)
    assert "alert(" not in button["props"]["onclick"] and "_nodeseek_cloud_verifying" not in defaults
    assert "CookieCloud 检测" not in json.dumps(plugin.get_page(), ensure_ascii=False)
    for value in (False, True):
        plugin.init_plugin({"cookiecloud": value, "cookie": "session=manual", "custom": "keep"})
        assert plugin._config["cookiecloud"] is value and plugin._config["custom"] == "keep"


def test_periodic_cloud_sync_updates_form_snapshot_without_overwriting_config(plugin_module, monkeypatch):
    """云端同步更新独立快照，手动配置不被覆盖，失败不替换快照。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"cookiecloud": True, "cookie": "session=manual-secret", "enabled": False})
    assert "cookiecloud_synced_cookie" not in plugin.data
    assert not hasattr(plugin, "saved_config")
    jobs = plugin.get_service()
    assert len(jobs) == 1 and jobs[0]["trigger"].__class__.__name__ == "IntervalTrigger"
    assert jobs[0]["trigger"].interval.total_seconds() == 86400
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: ({"nodeseek.com": "session=new-secret"}, ""))
    plugin.sync_cookiecloud()
    assert plugin.data["cookiecloud_synced_cookie"] == "session=new-secret"
    assert plugin.data["cookiecloud_synced_cookie"] == "session=new-secret"
    assert plugin._config["cookie"] == "session=manual-secret" and not hasattr(plugin, "saved_config")
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: (None, "secret-error"))
    plugin.sync_cookiecloud()
    assert plugin.data["cookiecloud_synced_cookie"] == "session=new-secret"
    assert "secret-error" not in repr(plugin_module.logger.messages)
    plugin._config["cookiecloud"] = False
    assert plugin.get_service() == []


def test_manual_mode_requires_cookie_and_ignores_cloud(plugin_module, monkeypatch):
    """手动模式不调用云端，缺失 Cookie 时不会启动浏览器。"""
    plugin = plugin_module.NodeSeekV3()
    monkeypatch.setattr(plugin_module, "_download_cookiecloud", lambda: pytest.fail("手动模式不得访问云端"))
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: pytest.fail("缺少 Cookie 不得启动浏览器"))
    plugin._config.update(cookiecloud=False, cookie="")
    assert plugin._request_sign()["message"] == "未配置有效的 Cookie"


def test_sign_idempotence_and_failed_retry(plugin_module, monkeypatch):
    """仅成功时记录日期，失败后允许再次尝试。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config["notify"] = True
    calls = []

    def request():
        """依次模拟失败和成功。"""
        calls.append(1)
        return {"success": False, "message": "失败", "gain": None} if len(calls) == 1 else \
            {"success": True, "message": "成功", "gain": 7}

    monkeypatch.setattr(plugin, "_request_sign", request)
    plugin.sign()
    assert "last_sign_date" not in plugin.data
    plugin.sign()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert plugin.data["last_sign_date"] == today
    plugin.sign()
    assert len(calls) == 2
    assert len(plugin.notifications) == 2
    assert "无法确认" in plugin.notifications[0]["text"]
    assert "7 个鸡腿" in plugin.notifications[1]["text"]
    assert plugin.data["last_result"]["gain"] == 7


def test_once_and_overlap(plugin_module, monkeypatch):
    """确认一次性开关重置且并发执行不会重复签到。"""
    jobs = []
    monkeypatch.setattr(plugin_module, "add_plugin_once_job", lambda *args, **kwargs: jobs.append((args, kwargs)) or True)
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"onlyonce": True, "cookie": "session=example"})
    assert plugin.saved_config["onlyonce"] is False
    assert len(jobs) == 1
    plugin._lock = threading.Lock()
    plugin._lock.acquire()
    plugin.sign()
    assert not plugin.data
    plugin._lock.release()


def test_window_trigger_boundaries_and_restarts(plugin_module, monkeypatch):
    """验证时段边界、稳定抽样、日切换及重启时剩余窗口。"""
    monkeypatch.setattr(plugin_module.random, "uniform", lambda low, high: high / 2)
    trigger = plugin_module.RandomWindowTrigger("08:00", "10:00")
    tz = timezone(timedelta(hours=8))
    before = datetime(2026, 11, 18, 7, 0, tzinfo=tz)
    first = trigger.get_next_fire_time(None, before)
    assert first == datetime(2026, 11, 18, 9, 0, tzinfo=tz)
    assert trigger.get_next_fire_time(None, before) == first
    assert trigger.get_next_fire_time(first, first) == datetime(2026, 11, 19, 9, 0, tzinfo=tz)
    mid = datetime(2026, 11, 18, 9, 0, tzinfo=tz)
    assert plugin_module.RandomWindowTrigger("08:00", "10:00").get_next_fire_time(None, mid) == \
        datetime(2026, 11, 18, 9, 30, tzinfo=tz)
    late = datetime(2026, 11, 18, 10, 1, tzinfo=tz)
    assert plugin_module.RandomWindowTrigger("08:00", "10:00").get_next_fire_time(None, late) == \
        datetime(2026, 11, 19, 9, 0, tzinfo=tz)


def test_window_trigger_real_apscheduler_processing(plugin_module, monkeypatch):
    """模拟 APScheduler 的到期扫描和二次 next-fire 计算，确保不会跳过一天。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(plugin_module.random, "uniform", lambda low, high: high / 2)
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 11, 18, 7, 0, tzinfo=tz)
    trigger = plugin_module.RandomWindowTrigger("08:00", "10:00")
    first = trigger.get_next_fire_time(None, start)
    scheduler = BackgroundScheduler(timezone=tz)
    job = scheduler.add_job(lambda: None, trigger=trigger, next_run_time=first)
    assert job._get_run_times(first) == [first]
    assert trigger.get_next_fire_time(first, first) == datetime(2026, 11, 19, 9, 0, tzinfo=tz)


def test_window_pending_time_stays_in_window_when_late(plugin_module, monkeypatch):
    """调度器延迟时保持已抽取时间，交由宿主判定失火策略。"""
    monkeypatch.setattr(plugin_module.random, "uniform", lambda low, high: high / 2)
    tz = timezone(timedelta(hours=8))
    trigger = plugin_module.RandomWindowTrigger("08:00", "10:00")
    first = trigger.get_next_fire_time(None, datetime(2026, 11, 18, 7, 0, tzinfo=tz))
    assert trigger.get_next_fire_time(None, datetime(2026, 11, 18, 11, 0, tzinfo=tz)) == first


@pytest.mark.parametrize("start,end", [("8:00", "10:00"), ("12:60", "13:00"),
                                       ("10:00", "10:00"), ("23:00", "02:00")])
def test_invalid_random_windows(plugin_module, start, end):
    """拒绝无效时间、零长度和跨午夜窗口。"""
    with pytest.raises(ValueError):
        plugin_module.RandomWindowTrigger(start, end)
    plugin = plugin_module.NodeSeekV3()
    plugin._config.update(enabled=True, schedule_mode="window", window_start=start, window_end=end)
    assert plugin.get_service() == []


def test_legacy_config_and_form_fields(plugin_module):
    """旧配置继续使用 Cron，未知键和全部原有表单字段保留。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"enabled": True, "cron": "30 7 * * *", "cookie": "session=example",
                        "random": False, "custom": "keep"})
    assert plugin._config["schedule_mode"] == "cron"
    assert plugin._config["custom"] == "keep"
    assert plugin.get_service()[0]["trigger"].__class__.__name__ == "CronTrigger"
    form, defaults = plugin.get_form()
    encoded = json.dumps(form, ensure_ascii=False)
    for field in ("enabled", "cookie", "cookiecloud", "cron", "random", "notify", "use_proxy",
                  "user_agent", "onlyonce", "schedule_mode", "window_start_hour", "window_start_minute",
                  "window_end_hour", "window_end_minute"):
        assert f'"model": "{field}"' in encoded
    assert defaults["schedule_mode"] == "cron"
    plugin._config.update(schedule_mode="window", window_start="08:00", window_end="10:00")
    assert isinstance(plugin.get_service()[0]["trigger"], plugin_module.RandomWindowTrigger)


def test_site_challenge_and_stage_diagnostics(plugin_module, monkeypatch):
    """验证站点验证提示、阶段诊断和异常脱敏。"""
    plugin = plugin_module.NodeSeekV3()
    plugin._config["cookie"] = "session=mock-secret"
    page = FakePage({})
    page.title = lambda: "Just a moment..."
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    outcome = plugin._request_sign()
    assert not outcome["success"] and "验证页面" in outcome["message"] and not page.evaluated and context.closed
    page = FakePage({"kind": "blocked", "status": 403})
    context = FakeContext(page)
    monkeypatch.setattr(plugin_module, "launch_browser_context", lambda **kwargs: context)
    outcome = plugin._request_sign()
    assert not outcome["success"] and "403" in outcome["message"] and "mock-secret" not in outcome["message"]


def test_form_conditional_time_selection(plugin_module):
    """计划字段按模式显示，小时与分钟分别提供完整选项。"""
    plugin = plugin_module.NodeSeekV3()
    form, _ = plugin.get_form()
    schedule = form[0]["content"][1]["content"][2]["content"]
    assert schedule[0]["content"][0]["content"][0]["props"]["items"][0]["title"] == "Cron 定时签到"
    assert schedule[1]["props"]["show"] == "{{schedule_mode == 'cron'}}"
    assert schedule[2]["props"]["show"] == "{{schedule_mode == 'window'}}"
    assert schedule[3]["props"]["class"] == "mt-3"
    assert schedule[3]["props"]["show"] == "{{schedule_mode == 'window'}}"
    for index, col in enumerate(schedule[2]["content"]):
        field = col["content"][0]
        assert field["component"] == "VSelect"
        assert len(field["props"]["items"]) == (24 if index % 2 == 0 else 60)
        assert field["props"]["items"][0] == "00"
        assert field["props"]["items"][-1] == ("23" if index % 2 == 0 else "59")


@pytest.mark.parametrize("value,message,expected", [(0, "", 0), ("7", "", 7),
    (None, "今天的签到收益是5个鸡腿", 5), (None, "账号 42，签到失败", None),
    (True, "", None), (-1, "", None), (1.5, "", None)])
def test_gain_parse(plugin_module, value, message, expected):
    """只提取明确鸡腿数，未知与零值严格区分。"""
    assert plugin_module.NodeSeekV3._gain(value, message) == expected


def test_overview_legacy_and_known_gain(plugin_module):
    """概览兼容旧记录，并展示新记录的奖励数。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.data["last_result"] = {"success": True, "message": "今日已签到", "time": "2026-11-18"}
    assert "未知（站点未返回）" in json.dumps(plugin.get_page(), ensure_ascii=False)
    plugin.data["last_result"]["gain"] = 0
    assert "0 个鸡腿" in json.dumps(plugin.get_page(), ensure_ascii=False)


def test_already_without_gain_preserves_today_reward(plugin_module, monkeypatch):
    """站点未给重复签到奖励时，保留同一日已记录的奖励。"""
    plugin = plugin_module.NodeSeekV3()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    plugin.data["last_result"] = {"time": today + "T00:30:00+08:00", "success": True, "gain": 4}
    monkeypatch.setattr(plugin, "_request_sign", lambda: {"success": True, "message": "今日已签到", "gain": None})
    plugin.sign()
    assert plugin.data["last_result"]["gain"] == 4
    assert "4 个鸡腿" in plugin.notifications[0]["text"]


def test_no_false_success_for_failed_or_unrelated_message(plugin_module):
    """非签到消息、未登录及没有成功标志的鸡腿余额均不得判成功。"""
    interpret = plugin_module.NodeSeekV3._interpret_result
    assert not interpret({"kind": "result", "ok": True, "message": "当前余额5个鸡腿"})["success"]
    assert not interpret({"kind": "result", "ok": True, "success": True,
                          "message": "请先登录，签到奖励5个鸡腿"})["success"]
    assert interpret({"kind": "result", "ok": True, "message": "今日已完成签到"})["success"]


def test_split_time_preserves_legacy_saved_config(plugin_module):
    """旧时间在表单中拆分，保存时不被默认值覆盖；新选择能正确合并。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"enabled": True, "cookie": "session=example", "schedule_mode": "window", "window_start": "07:23",
                        "window_end": "11:42", "custom": "keep"})
    _, defaults = plugin.get_form()
    merged = {**defaults, "enabled": True, "cookie": "session=example", "schedule_mode": "window", "window_start": "07:23",
              "window_end": "11:42", "custom": "keep"}
    assert merged["window_start_hour"] == "07" and merged["window_start_minute"] == "23"
    assert merged["window_end_hour"] == "11" and merged["window_end_minute"] == "42"
    plugin.init_plugin(merged)
    trigger = plugin.get_service()[0]["trigger"]
    assert (trigger.start, trigger.end) == ((7 * 60 + 23) * 60, (11 * 60 + 42) * 60)
    merged.update(window_start_hour="06", window_start_minute="09", window_end_hour="12", window_end_minute="59")
    plugin.init_plugin(merged)
    assert plugin._config["window_start"] == "06:09"
    assert plugin._config["window_end"] == "12:59"
    assert plugin._config["custom"] == "keep"


def test_split_time_edits_remain_valid_after_reopen(plugin_module):
    """已保存的分离字段变更后，表单、概览内部值和调度一致。"""
    plugin = plugin_module.NodeSeekV3()
    plugin.init_plugin({"enabled": True, "cookie": "session=example", "schedule_mode": "window", "window_start": "07:23",
                        "window_end": "11:42", "window_start_hour": "06", "window_start_minute": "09",
                        "window_end_hour": "12", "window_end_minute": "59"})
    _, model = plugin.get_form()
    assert model["window_start_hour"] == "06" and model["window_start_minute"] == "09"
    assert model["window_end_hour"] == "12" and model["window_end_minute"] == "59"
    assert plugin._config["window_start"] == "06:09"
    trigger = plugin.get_service()[0]["trigger"]
    assert trigger.start == (6 * 60 + 9) * 60


def test_split_time_invalid_never_silently_resets(plugin_module):
    """无效的旧时间不会因为打开并保存表单被静默替换为默认时间。"""
    plugin = plugin_module.NodeSeekV3()
    old = {"enabled": True, "schedule_mode": "window", "window_start": "bad",
           "window_end": "11:42"}
    plugin.init_plugin(old)
    _, defaults = plugin.get_form()
    assert defaults["window_start_hour"] is None and defaults["window_start_minute"] is None
    plugin.init_plugin({**defaults, **old})
    assert plugin._config["window_start"] == "bad"
    assert plugin.get_service() == []


def test_history_keeps_legacy_and_records_success_failure(plugin_module, monkeypatch):
    """保留旧版最近结果并累计后续失败和成功，不覆盖已有历史。"""
    plugin = plugin_module.NodeSeekV3()
    old = {"time": "2026-01-02T03:04:05+08:00", "message": "旧记录", "success": True, "gain": 3}
    plugin.data["last_result"] = old.copy()
    sequence = iter([{"success": False, "message": "网络错误", "gain": None},
                     {"success": True, "message": "签到成功", "gain": 8}])
    monkeypatch.setattr(plugin, "_request_sign", lambda: next(sequence))
    plugin.sign()
    plugin.sign()
    history = plugin.data["sign_history"]
    assert [record["message"] for record in history] == ["签到成功", "网络错误", "旧记录"]
    assert old == history[-1]
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "签到历史" in page and "旧记录" in page and "8 个鸡腿" in page
    assert "2026-01-02 03:04:05" in page and "2026-01-02T03:04:05" not in page
    assert "Cron：" not in page


def test_time_format_and_empty_history(plugin_module):
    """格式化带时区时间及空历史，异常旧值不触发崩溃。"""
    plugin = plugin_module.NodeSeekV3()
    assert plugin._format_time("2026-11-18T01:27:13+00:00") == "2026-11-18 09:27:13"
    assert plugin._format_time("2026-11-18T09:27:13+08:00") == "2026-11-18 09:27:13"
    assert plugin._format_time("非标准时间") == "非标准时间"
    assert "暂无签到记录" in json.dumps(plugin.get_page(), ensure_ascii=False)
