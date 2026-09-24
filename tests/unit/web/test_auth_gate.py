"""访问口令（ADR-0033）：口令存取、会话签名、以及"非回环绑定才生效"的门。

三组：
  1. **口令与会话本身**（纯函数，零 HTTP）：加盐哈希、错口令、过期、篡改、
     换口令让旧会话失效；
  2. **门的行为**：同一个 app，用 `TestClient(client=…)` 冒充不同来源
     ——回环免口令、局域网页面 302 去登录、局域网接口 401、登录后放行；
  3. **失败闭合**：绑了非回环却**没设口令**时，局域网来源一律拒绝（正常路径上
     serve 已经在启动前拦掉，这里是第二层）。

为什么这一层要测得这么细：它是"开给局域网"与"我的资料谁都看得见"之间**唯一**
的一道门（用户 2026-09-21 明确要求"每个人的数据独立、别人看不到我的资料"）。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from mikasa.config.settings import load_settings
from mikasa.web import auth
from mikasa.web.app import create_app

PASSWORD = "test-password-1234"


# ---------------------------------------------------------------------------
# 口令与会话
# ---------------------------------------------------------------------------


def test_password_roundtrip_and_rejection(tmp_path):
    """设了口令就能验过；错口令、没设口令一律 False（后者 = 谁都进不去）。"""
    assert auth.verify_password(tmp_path, PASSWORD) is False  # 还没设
    auth.set_password(tmp_path, PASSWORD)
    assert auth.verify_password(tmp_path, PASSWORD) is True
    assert auth.verify_password(tmp_path, PASSWORD + "x") is False
    assert auth.verify_password(tmp_path, "") is False


def test_password_file_never_stores_plaintext(tmp_path):
    """文件里不该出现明文口令（哈希 + 盐 + 会话密钥三样）。"""
    auth.set_password(tmp_path, PASSWORD)
    raw = auth.auth_file(tmp_path).read_text(encoding="utf-8")
    assert PASSWORD not in raw
    assert "hash" in raw and "salt" in raw and "secret" in raw


def test_token_roundtrip_expiry_and_tamper(tmp_path):
    """会话：签发能验、过期不算、改一个字符也不算。"""
    auth.set_password(tmp_path, PASSWORD)
    token = auth.make_token(tmp_path, ttl=60, now=1000.0)
    assert auth.token_ok(tmp_path, token, now=1030.0) is True
    assert auth.token_ok(tmp_path, token, now=1061.0) is False  # 过期
    assert auth.token_ok(tmp_path, token + "x", now=1030.0) is False  # 篡改签名
    assert auth.token_ok(tmp_path, token.split(".")[0] + ".deadbeef", now=1030.0) is False
    assert auth.token_ok(tmp_path, "不是个令牌", now=1030.0) is False


@pytest.mark.parametrize(
    "token",
    [
        "１２３.abc",  # 全角数字：int() 认（== 123），但 .encode("ascii") 抛
        "٣.abc",  # 阿拉伯-印度数字：同上
        "1_0.abc",  # int() 认下划线分隔符
        "-1.abc",
        "0x10.abc",
    ],
)
def test_token_with_non_ascii_digits_is_false_not_500(tmp_path, token):
    """畸形 Cookie 只能得到 False（→401），绝不能抛。

    2026-09-24 修：`int()` 认全角/阿拉伯-印度数字甚至 `1_0`，但随后的
    `.encode("ascii")` 会抛 UnicodeEncodeError——而这道门在 `--host 0.0.0.0`
    下是**未鉴权就能到达**的：同网段任何人发一个
    `Cookie: mikasa_session=１２３.x` 就能稳定打出 500 + 服务端堆栈日志，
    本该一律 401。同类问题本仓库早有既定做法（app.py 的 _drop_surrogates）。
    """
    auth.set_password(tmp_path, PASSWORD)
    assert auth.token_ok(tmp_path, token, now=1000.0) is False


def test_changing_password_invalidates_old_sessions(tmp_path):
    """换口令 = 换会话密钥 → 旧会话立刻失效（"改密码"该有的语义）。"""
    auth.set_password(tmp_path, PASSWORD)
    old = auth.make_token(tmp_path, ttl=3600, now=1000.0)
    assert auth.token_ok(tmp_path, old, now=1001.0) is True
    auth.set_password(tmp_path, "another-password")
    assert auth.token_ok(tmp_path, old, now=1001.0) is False


def test_loopback_host_detection():
    """回环判定：本机地址算，暴露地址不算，认不出来的**按不安全处理**。"""
    for host in ("127.0.0.1", "::1", "localhost", ""):
        assert auth.is_loopback_host(host) is True, host
    for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.8", "example.com"):
        assert auth.is_loopback_host(host) is False, host


def test_clear_password(tmp_path):
    auth.set_password(tmp_path, PASSWORD)
    assert auth.clear_password(tmp_path) is True
    assert auth.is_configured(tmp_path) is False
    assert auth.clear_password(tmp_path) is False  # 再删一次：没有可删的


# ---------------------------------------------------------------------------
# 门：不同来源走同一个 app
# ---------------------------------------------------------------------------


def _app(host: str):
    """按指定监听地址建 app（host 决定门开不开）。

    Settings/WebConfig 都是 frozen 的 → 用 model_copy 造一份改过 host 的副本
    （与"serve 读 web.host"走的是同一个字段，测的就是那条路径）。
    """
    settings = load_settings("offline")
    return create_app(
        settings.model_copy(update={"web": settings.web.model_copy(update={"host": host})})
    )


def _client(app, *, host: str):
    """冒充指定来源地址的测试客户端。

    `TestClient(client=(host, port))` 就是改 ASGI scope 里的 client——
    门的判据正是它（回环放行 / 其余要会话），所以这一层能精确测到。
    """
    return TestClient(app, client=(host, 51234), follow_redirects=False)


def test_loopback_needs_no_password_even_when_lan_bound(tmp_path):
    """绑了 0.0.0.0，但**从本机访问**（桌面端/本地浏览器）→ 照旧免口令。"""
    app = _app("0.0.0.0")
    with _client(app, host="127.0.0.1") as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/").status_code == 200


def test_lan_client_is_gated_without_session(tmp_path):
    """局域网来源：页面 302 去登录页、接口 401 JSON。"""
    app = _app("0.0.0.0")
    with _client(app, host="192.168.1.9") as c:
        page = c.get("/", follow_redirects=False)
        assert page.status_code == 302 and page.headers["location"] == "/login"

        api = c.get("/api/health")
        assert api.status_code == 401
        assert api.json()["error"]["type"] == "unauthorized"

        # 深链接会把目标带进 next（登录后送回去）
        deep = c.get("/documents", follow_redirects=False)
        assert deep.status_code == 302
        assert deep.headers["location"] == "/login?next=%2Fdocuments"


def test_lan_login_flow_grants_access(tmp_path):
    """输对口令 → 拿到会话 Cookie → 后续请求放行；错口令 401 且不发 Cookie。"""
    settings = load_settings("offline")
    # 口令写在**这个 app 用的数据目录**里
    auth_payload_dir = settings.data_dir
    auth.set_password(auth_payload_dir, PASSWORD)

    app = _app("0.0.0.0")
    with _client(app, host="192.168.1.9") as c:
        # 登录页本身免口令
        assert c.get("/login").status_code == 200

        bad = c.post("/api/auth/login", data={"password": "wrong", "next": "/"})
        assert bad.status_code == 401 and "口令不对" in bad.text
        assert auth.COOKIE_NAME not in c.cookies

        ok = c.post("/api/auth/login", data={"password": PASSWORD, "next": "/documents"})
        assert ok.status_code == 303 and ok.headers["location"] == "/documents"
        assert auth.COOKIE_NAME in c.cookies

        assert c.get("/api/health").status_code == 200  # 带会话 → 放行

        # 已登录再开登录页 → 直接送回目标页（不再看"你已登录"）
        again = c.get("/login", follow_redirects=False)
        assert again.status_code == 303


def test_lan_client_is_refused_when_no_password_configured(tmp_path):
    """**失败闭合**：绑了非回环却没设口令 → 一律拒绝（serve 会在启动前拦，
    这里是第二层，防的是"有人绕过 CLI 直接 create_app"）。"""
    app = _app("0.0.0.0")
    with _client(app, host="192.168.1.9") as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 302


def test_logout_clears_the_session(tmp_path):
    """登出后立刻回到"没有会话"的状态。"""
    settings = load_settings("offline")
    auth.set_password(settings.data_dir, PASSWORD)
    app = _app("0.0.0.0")
    with _client(app, host="192.168.1.9") as c:
        c.post("/api/auth/login", data={"password": PASSWORD, "next": "/"})
        assert c.get("/api/health").status_code == 200
        out = c.post("/api/auth/logout", follow_redirects=False)
        assert out.status_code == 303 and out.headers["location"] == "/login"
        assert c.get("/api/health").status_code == 401


def test_lan_bound_gate_is_not_installed_for_a_default_local_run():
    """默认（127.0.0.1）绑定不带门：局域网来源根本连不上，本地照旧——本机自用
    与全部既有测试的行为一个字不变（这一条是"零回归"的显式锁）。"""
    app = _app("127.0.0.1")
    with _client(app, host="192.168.1.9") as c:
        assert c.get("/api/health").status_code == 200  # 门没装，直通


def test_open_redirect_is_refused(tmp_path):
    """登录后的跳转只认站内路径（`next=//evil.com` 会被打回 `/`）。"""
    settings = load_settings("offline")
    auth.set_password(settings.data_dir, PASSWORD)
    app = _app("0.0.0.0")
    with _client(app, host="192.168.1.9") as c:
        resp = c.post(
            "/api/auth/login",
            data={"password": PASSWORD, "next": "//evil.example.com/x"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_wildcard_bindings_are_gated(host: str):
    """通配绑定（0.0.0.0 / ::）都算"开给网络"，都要门。"""
    assert auth.is_loopback_host(host) is False
