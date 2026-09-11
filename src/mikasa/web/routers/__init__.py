"""Web 路由包：health / qa / documents / eval 各自一个 APIRouter。

约定：端点一律同步 def（FastAPI 线程池执行，AskService / LLM / SQLite
全是阻塞调用）；错误响应统一 {"error": {"type", "message"}}（中文），
由 app.py 的异常处理器统一翻译，路由内不散落 try/except。
"""
