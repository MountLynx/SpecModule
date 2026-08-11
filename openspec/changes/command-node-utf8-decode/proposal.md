## Why

`Command` 节点 `subprocess.run(..., text=True)` 未指定 `encoding`，靠 locale 默认编码解码
子进程 stdout/stderr。在非 UTF-8 控制台（如中文 Windows，GBK/cp936）上，`cmd`/`ping` 输出
GBK 字节，而 anaconda Python 3.13 启用 UTF-8 模式 → 后台 reader 线程 `UnicodeDecodeError`，
subprocess 内部线程异常，pytest 报 `PytestUnhandledThreadExceptionWarning`，stdout 捕获失败。

## What Changes

- **command.py 子进程调用显式指定编码**：`subprocess.run(..., text=True, encoding="utf-8",
  errors="replace")`——解码永不抛异常，非法字节替换为 `\ufffd`，stdout/stderr 稳定捕获。
- 不改变超时/失败/退出码语义；仅修复文本解码的健壮性。

## Capabilities

### New Capabilities
- `command-node`: command 节点子进程执行契约——文本输出稳定解码（UTF-8 + replace），
  中文/非 UTF-8 控制台不炸 reader 线程。

### Modified Capabilities
无（specs 目录当前为空）。