"""python -m mikasa 入口：等价于 mikasa 命令（含 .env 装载与日志初始化）。

真实教训（limitations-and-failures.md §四）：曾直调 app() 绕过 main() 的
load_dotenv_file()/setup_logging()，导致 python -m 与 mikasa 不等价——
api profile 下报"密钥缺失"假阴性。两条入口的初始化必须走同一函数。
"""

from mikasa.cli import main

if __name__ == "__main__":
    main()
