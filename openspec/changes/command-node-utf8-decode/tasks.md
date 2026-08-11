## 1. 修复

- [x] 1.1 `command.py:subprocess.run` 加 `encoding="utf-8", errors="replace"`。

## 2. 验证

- [x] 2.1 `python -m pytest module_harness/tests/test_command.py -q
      -W error::pytest.PytestUnhandledThreadExceptionWarning` 全绿（无 reader 线程异常）。
- [x] 2.2 `python -m pytest module_harness/tests/ -q` 全绿（无跨测试回归）。