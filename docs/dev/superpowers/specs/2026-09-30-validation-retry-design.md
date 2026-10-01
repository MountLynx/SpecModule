# 校验失败带反馈重试设计 — HarnessConfig.validate_retries

> 日期：2026-09-30
> 状态：设计已确认（brainstorm 两案：宿主=build_body 单配方 / 终态=失败收据不中止），
> 待实施
> 关联：`module_harness/core/config.py`（HarnessConfig）/ `core/harness.py`
> （build_body）/ `core/call.py`（call_harness）/ `model/spec.py`
> （TaskDefinition）/ `orchestrate/graph_builder.py`（task 级覆盖）；
> `docs/guides/config-guide.md`（字段文档同步）
> 背景：失败语义缺口讨论（裸 harness LLMError→infrastructure ABORT、校验失败
> Failure(type="llm") 下游跳过——见本文 §0）；姊妹 spec：
> `2026-09-30-tickflow-failed-terminal-design.md`（引擎饿死终态，独立发版线）

## 0. 缘起与边界

LLM 输出校验失败是**随机性**失败——同一 prompt 重问常可自愈——但 harness
执行配方现状是一次调用定生死：`validator.validate` 失败即返回
`Failure(type="llm")`（`outputfmt.py`），下游跳过。复杂图（ppt_master）为
此被迫用 script 包 `call_harness` 在 body 内自行接住失败塑形收据；简单图
（academic_writer）则直接吃跳过语义。重试属于这一层：**带校验错误反馈的
重问**（instructor 模式），有信息增益，预算有限。

事实约束（决定设计形状）：

1. **一份执行配方**。节点路径（`registry.harness()` → `Harness.build_body`，
   registry.py:63-68）与 task 路径（`call_harness` → 同一 `build_body`，
   call.py:74）汇聚于同一处。重试加在 `build_body` 内，两条路同时受益，
   配方不裂（call.py docstring 契约："零新执行语义……仅一份执行配方"）。
2. **传输层重试已存在且各归其位**。`LLMConfig.max_retries`（缺省 3）进
   SDK 构造器，SDK 对连接错误/408/429/5xx 指数退避。本设计**不**对
   `LLMError` 重试——SDK 3 次耗尽即系统性故障，fail-fast（infrastructure
   → ABORT）是正确语义，body 内再包只是推迟死刑。
3. **无隐式行为**。缺省预算 0 = 行为与现状逐字节一致；所有现有消费方
   （academic_writer / ppt_master / 一致性审核 / 翻译通道）零变化，开启
   是显式选择。

非目标（本设计不做）：
- 传输层失败重试（SDK 已管，见约束 2）；
- image 模式（无文本输出格式可校验，显式拒绝，见 §1）；
- 重试耗尽后中止 run（维持 `Failure(type="llm")` 语义，收据/修复环由图
  决定——中止只属于 infrastructure 与修复环预算）;
- 包装调用的 LLM 层审计归属（`__call__` 事件归属、raw/usage 进节点
  record——已知独立缺口，另案）；
- 降温/换模型重试（YAGNI）。
- translator.py prompt_core 覆盖分支的既有字段丢弃维持现状（该分支本就不携带
  mode/image_*；修时与 validate_retries 四字段一起补）

## 1. 配置面 — core/config.py

### 1.1 HarnessConfig.validate_retries

```python
validate_retries: int = 0
"""输出校验失败时的额外重试次数（带校验错误反馈重问）。

0（缺省）= 不重试，行为与无此字段时逐字节一致。
N > 0 = 校验失败后最多再问 N 次，每次 prompt 追加校验错误反馈段；
预算耗尽返回最后一次的 Failure(type="llm")。
仅作用于输出校验失败；LLMError（传输层）不重试、不消耗预算。
"""

def __post_init__(self) -> None:
    ...
    if self.validate_retries < 0:
        raise ValueError(f"validate_retries 须 >= 0，得到 {self.validate_retries!r}")
    if self.validate_retries > 0 and self.mode == "image":
        raise ValueError("mode='image' 无文本输出格式可校验，validate_retries 须为 0")
```

`to_dict` / `from_dict` 经 `dataclasses.asdict` / `cls(**data)` 自动兼容，
无手工改动。

### 1.2 TaskDefinition 与 spec 校验 — model/spec.py

`TaskDefinition` 新增可选字段 `validate_retries: int | None = None`
（None = 不覆盖，沿用注册 config）。`TasklistValidator` 校验：缺省 None；
显式值须为 `int >= 0`。

## 2. 重试语义 — core/harness.py build_body

body 内 LLM 调用成功后的校验段改造：

```python
budget = config.validate_retries
attempt_prompt = rendered                      # 首次 = 原渲染 prompt
while True:
    response = await llm.complete(prompt=attempt_prompt, ...)
    # LLMError 路径原样：infrastructure Failure，不消耗预算
    if validator is None:
        return response.content
    result = validator.validate(response.content)
    if not isinstance(result, Failure):
        return result                          # 通过，照旧
    if budget <= 0:
        return result                          # 耗尽：最后一次 Failure(type="llm")
    budget -= 1
    attempt_prompt = rendered + _VALIDATION_FEEDBACK.format(error=result.error)
```

- **反馈段**为框架契约文案（模块常量 `_VALIDATION_FEEDBACK`），形如：

  ```
  上一次输出未通过校验：{error}
  请修正该问题后重新输出完整结果，不要复述错误内容，不要解释修改过程。
  ```

- LLM 参数（model/temperature/think/notdo/api_params）重试时原样保留，
  不降温不换模型。
- image 模式路径（`_run_image`）不进此循环（§1.1 已在配置层拒绝）。

## 3. 审计与用量

- **事件**：每次尝试发完整事件链（PromptRendered / LlmCallStarted /
  LlmToken / OutputValidated…）——重试在审计里自然表现为多次调用，节点
  state 另记 `_validation_attempts`（实际 LLM 调用次数）与
  `_validation_retry_errors`（历次校验错误列表），进 `NodeState.
  mutable_state` 审计链。
- **HarnessCallResult**（call.py）：`usage` 改为**累计**（各次 attempt 之
  和），`raw` 为**最后一次**尝试的原始输出；docstring 写明。图内节点路径
  的 `_usage` state 键同步累计语义。

## 4. task 级覆盖 — orchestrate/graph_builder.py

`_register_harness` 的 override 链新增一项：`task.validate_retries` 非
None 时覆盖 `existing.validate_retries` 构造新 `HarnessConfig`（对称于
model/temperature/mode 等既有覆盖，含同样的"漏掉即静默沿用注册值"注释
义务）。tasklist 文本即可按任务调预算，无需重注册。

## 5. 测试 — module_harness/tests/

1. `test_harness.py`（或就近新建）：
   - 首败次过：mock llm 第一次返坏 JSON 第二次返好的，`validate_retries=1`
     → 断言 2 次 complete 调用、第二次 prompt 含反馈文本（含首错 error）、
     返回校验后值；
   - 预算耗尽：始终坏输出、`=1` → 2 次调用后返回 `Failure(type="llm")`；
   - `=0`（缺省）：1 次调用即返 Failure，与现状逐字节一致；
   - LLMError 不消耗预算：第 1 次坏输出、第 2 次 LLMError、`=2` → 结果为
     infrastructure Failure 且 complete 恰 2 次；
   - image + `validate_retries>0` → ValueError（`__post_init__`）；
   - 事件序列含两次 `LlmCallStarted`；state 含 `_validation_attempts=2`。
2. `test_call.py`：预算耗尽 → `HarnessCallError`；`usage` 累计断言；
   `raw` = 最后一次输出。
3. `test_graph_builder.py`（或 spec/tasklist 侧）：task `validate_retries`
   override 传递进注册 config；validator 对非法值（负数/非 int）报错。
4. 全量回归：`python -m pytest module_harness/tests/ -q` 绿（缺省 0 保证
   零行为变化）。

## 6. 文档同步

- `docs/guides/config-guide.md`：HarnessConfig / tasklist task 键清单加
  `validate_retries`（语义、缺省 0、image 互斥）。
- `docs/api.md`：如列 HarnessConfig 字段契约则同步。
