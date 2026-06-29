# SpecModule

可审计、可调试、可监控的完全控制的LLM使用。

## 一个module基本结构与要素：
* spec ”想要什么“——内容约束，主要是描述目标是什么，适当进行过程部分spec

* tasklist 流程控制——“如何去做”，注意：tasklist是spec在执行层面上的细粒度翻译，而不是简单的task拼接排布

* submodule 与 harness 为执行元件，实现手段
  * submodule就是一个tasklist固定、spec强模板化的一个module
  * submodule和harness本质就是一个特定输入得到特定输出的元件

* harness 由prompt、outputformat组成

* script供给module内部处理，部分scrip实际上是起到通常Agent中tool的作用

## spec与tasklist

spec和tasklist都是被支持的输入，两种输入模式——可以输入spec+tasklist，也可以只输入spec。

* 输入spec+tasklist，内部一致性审核通过后，spec作为目标约束，用于判断执行是否偏移目标，spec此时可以简略
* 输入spec，module内部翻译为tasklist，然后执行；要求spec要足够详细

每n个tick后使用LLM对当前状态（主要是history中的输出内容）与spec进行对齐判断，如果判定为偏离则截断提醒。

tasklist的执行引擎已开发，即tickflow

tasklist写法参考

```json
{
  Tasks: {
    A : {......},
    B : {......},
    ......
  }
  Flow: {
    """
    A --> B
    B --> C
    ......
    """,
  }
}
```

总之方便人和agent写以及tickflow翻译。

## submodule

submodule是tasklist固定、spec强模板化的一个module，其spec和tasklist是固定的，不能修改。且取消快照状态等开销，形成一个特定输入得到特定输出的固定的“箱子”。


## 状态记录与控制——快照与回滚

一个module的运行就是一个进程，整个进程进行过程记录。

状态记录以最终实现前端可视化展示与监控为准。

快照和回滚以tick为粒度，恢复到某个tick的全局字典和布尔值表状态。

** 回滚时可以调整spec和tasklist中未执行的部分 **


## harness

LLM+prompt，需要指定LLM的供应商、模型名和模型相关设置（温度、是否开启think等）

兼顾“专一方向”的定制化特用性，和这个专一方向上应对各种情况的灵活性。

三层prompt：

* 必要能力提示词、是什么、要做什么(只含必要的)————写死的，只有部分关键词可以替换，大方向定调用
* 动态注入prompt（一是许多skil中会写多种情况下应该应该怎么样，这里的prompt就是针对这部分的，不是都写出来一起注入，而是选择性注入
* 人工注入prompt，除开前二者关键词替换之外的，人工注入的部分。

```json
Task : {
  "promptmode": "xxx", # 动态prompt，在提供的选项中选择
  "prompt": "xxx", #人工注入的部分
  "input": {"xxx": "xxx",...}, # 用于替换提示词中的关键字，作为输入；内容在tickflow全局记录的内容字典里 
  "outputformat": "xxx", # 输出格式
  "notdo": ["xxx",...] # 进一步约束，同样注入提示词
}
```

仅作参考，起名和格式都不规范，总之要兼顾可读性可写性，以及框架翻译为tickflow的一个body的便利性。
  

flow 层事件（on_fire / on_tick_start / on_tick_end）：
  - node_status_changed(node, status: idle|fireable|firing|ok|failed|aborted)
  - tick_started(tick, fireable_nodes[])
  - tick_completed(tick, firings[])

harness/script 层事件（EventBus，body 内部）：
  - llm_token(node, chunk)        # 流式，高频
  - script_output

其中部分内容实际上就是在全局字典里的，或许可以直接记录字典键值……关键在于可视化的方便性。


一般认为现在的agent还需要为LLM配置tool，但在moduleharness中这部分被放到script中，单独一个node。目的：一个node是任务的一个最小单元，不需要LLM进行调用tool的循环；减轻LLM的上下文压力，无toollist和tool介绍的注入；奉行模型能力无关（尽量），使一些没有为工具调用优化过的老模型也能用（说老其实也就是一年多以前的模型，发展还是太快了）。

## 使用方式

### 通过agent调用

agent与人交互，形成一个完整的spec，或者spec+tasklist，然后agent调用moduleharness。

1. 外部agent（claude、claw、Hermes等）调用，方式MCP
2. 自建agent调用，方式为斜杠指令激活加指令文本提取

期望的场景：多个model并行，多进程或者多线程。agent与后台跑的model通信，后台跑model的时候agent可以做别的事，可以创建别的module进程或线程，可以与人交互，总之就是不被module阻塞。agent只对module具备有限的调用能力（吊起、状态查询、终止、回滚）

为实现，需要以下能力

* module独立进程
* mcp接的不是module内部，而是modle写出的状态。需要及时返回agent并断开连接，以防阻塞agent和module
* module状态持久化到json或者直接用sqlite

### 嵌入式

submodule和其他嵌入式开发场景，状态与快照只起到调试作用，完成后封装成一个接受特定格式输入，输出特定格式“箱子”。

## 开发时始终遵循的————为了后续的可拓展性

* 可视化，响应的接口、json形式需要考虑可视化的方便性
* 自建agent，需要方便指令控制、独立进程不相互阻塞

为此，可以将一些接口封装成SDK，统一被mcp、自建agent、可视化fastapi等调用。


