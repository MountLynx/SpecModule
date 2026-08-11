## Purpose

command 节点子进程的文本输出稳定解码：显式 UTF-8 + `errors="replace"`，非 UTF-8 控制台
（中文 Windows GBK）不再导致子进程读取线程解码崩溃。

## ADDED Requirements

### Requirement: command 子进程输出显式 UTF-8 解码

`Command` body 执行 `subprocess.run` 时显式指定 `encoding="utf-8", errors="replace"`，
不依赖 locale 默认编码。解码失败不得抛异常（替换为 `\ufffd`）。

#### Scenario: 非 UTF-8 控制台输出
- **WHEN** 子进程（如 `cmd`/`ping`）在中文 Windows 上输出 GBK 编码报错信息
- **THEN** stdout/stderr 稳定捕获（非法字节替换为 `\ufffd`），无 reader 线程
  `UnicodeDecodeError`，`CommandCompleted`/`CommandFailed` 正常发出

#### Scenario: UTF-8 控制台输出不回归
- **WHEN** 子进程输出合法 UTF-8 文本
- **THEN** stdout/stderr 原样捕获，字符无损