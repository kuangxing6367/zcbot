"""顶层参数解析与子命令相关的小工具：版本、帮助、语言参数。"""
import os

# framework/bootstrap/cli_dispatch.py 上溯三级即项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_version():
    try:
        with open(os.path.join(_PROJECT_ROOT, 'VERSION'), 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'


def print_usage():
    print("""ZCBOT 用法：
  python main.py [config.yaml]   启动 bot（缺省用 config.yaml；文件不存在则自动生成默认配置）
  python main.py code            打开 AI 智能体界面（不启动 bot，需要 textual）
  python main.py attach          接入运行中的 bot 调试控制台（无需 TTY / 无需重启）
  python main.py -a              同上（attach 的简写）
  python main.py -a --agent       智能体对话模式（纯文本通道里跟 AI 对话）
  python main.py code            全屏 AI 界面（需要真正的终端 / TTY）
  python main.py --language LANG 提示文案语言：zh（默认）/ ciallo（活泼版）/ en
  python main.py -l LANG         同上（简写）。也可用环境变量 ZCBOT_LANG=ciallo
  python main.py config          配置控制台：显示当前配置摘要
  python main.py config get K    查看配置项（如 language）
  python main.py config set K V  修改配置项（写回 config.yaml，重启生效）
  python main.py --iipc          走 IIPC 内核链路（Go 内核 + Rust 协议端，插件独立进程）
  python main.py --help          显示本帮助

attach 常用参数：
  --config <path>   指定 config.yaml（据此定位数据库里的控制台凭证）
  --db <path>       直接指定 SQLite 库路径
  --port/--token    直接指定控制台端口与 token（跳过读库）
  --agent           每行都交给 AI（正常模式下也可用行首 `!` 单条交给 AI）

--iipc 常用参数（其余见 framework/iipc/README.md；端口等也可在 config.yaml 的 iipc: 段配置）：
  --ipc-addr ADDR      内核 IPC 监听 [ip:]port（默认 127.0.0.1:18730）
  --plugins-dir DIR    插件目录（默认 framework/iipc/plugins）
  --admin IDS          管理员 user_id（逗号分隔，供 require_admin 鉴权）
  --onebot-url URL     正向 WS：适配器主动连 ws://host:port
  --onebot-listen ADDR 反向 WS：监听 [ip:]port 等 OneBot 实现连入（默认 127.0.0.1:6701）
  --no-rust            不启动 Rust 协议端（send 类动作不可用，事件注入仍可）""")


def pop_language_arg(argv):
    """从 argv 中摘出语言选项：--language LANG / -l LANG / --language=LANG。

    摘掉后剩余参数继续走原有的「子命令 / 配置文件路径」解析，避免被后面的
    「未知选项」检查误杀。返回 (新 argv, 语言代码或 None)。
    """
    out = []
    lang = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--language='):
            lang = a.split('=', 1)[1].strip()
        elif a in ('--language', '-l'):
            if i + 1 < len(argv):
                lang = argv[i + 1].strip()
                i += 1
        else:
            out.append(a)
        i += 1
    return out, (lang or None)
