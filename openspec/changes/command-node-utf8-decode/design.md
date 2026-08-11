## Context

`module_harness/command.py` 的 `Command.build_body` 用 `subprocess.run(..., text=True)` 捕获
stdout/stderr。`text=True` 未指定 `encoding` → 用 `locale.getpreferredencoding()`。anaconda
Python 3.13 下 UTF-8 模式使该值为 UTF-8，而子进程（中文 Windows 的 `cmd`/`ping`）按控制台
代码页（GBK/cp936）输出 → reader 线程解码 `UnicodeDecodeError`，subprocess 内部线程异常，
pytest 报 `PytestUnhandledThreadExceptionWarning`。`test_command.py` 的失败命令/错误信息测试
在本机触发（英文/UTF-8 控制台不触发，故为环境相关缺陷）。

## Goals / Non-Goals

**Goals:**
- command 节点子进程文本解码健壮，永不因编码抛异常。
- UTF-8 输出无损。

**Non-Goals:**
- 不改超时/失败/退出码语义。
- 不引入 `bytes` 模式（文本语义保留，`CommandCompleted.stdout` 仍为 str）。
- 不改 tickflow。

## Decisions

### D1: 显式 `encoding="utf-8", errors="replace"`
`subprocess.run(..., text=True)` 改为 `encoding="utf-8", errors="replace"`。用 replace 而非
strict：子进程输出非关键文本，损坏字节替换为 `\ufffd` 比抛异常更符合"捕获到结果"的意图。
明文选 UTF-8 是合理默认（SpecModule 全项目 UTF-8 约定）。

### D2: 不引入 bytes/解码配置
`CommandConfig` 不加 `encoding` 字段——当前无确认的第二个消费者会自定义编码，YAGNI；
硬编码 UTF-8+replace 满足现状。若未来出现需求再加字段。

## Risks / Trade-offs

- `errors="replace"` 会丢弃损坏字节的原始信息（变 `\ufffd`）。可接受：文本输出本就非关键，
  且 SDK 不依赖精确字节。
- UTF-8 显式编码在纯 ASCII 控制台无影响，在正确 UTF-8 控制台无损，仅修非 UTF-8 环境。