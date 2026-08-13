# Palintropos

[English](README.md)

**可逆副作用与响应式依赖的微型可执行模型。**

Palintropos（希腊语词源，意为“转身返回”）把 DeepSeek Harness 与 Cordis 背后的设计压缩成三个原子：

```text
effect(do -> undo)
provide / get
plugin(requires -> activate / deactivate)
```

这是一个零依赖、单核心文件、面向 Python 3.11+ 的教学项目。全新 clone 后可直接运行：

```bash
python3 palintropos.py
```

Palintropos 是独立教学实现，**不是** DeepSeek 官方项目，不复制上游源代码，也不追求与 Cordis API 兼容。

## 名称含义

这个运行时中的每个动作都有返回路径。插件可以在激活期间改变共享状态，但记录下来的逆操作会把状态送回起点。“Palintropos”指的正是这次转身返回。

## 三个原子

### 1. `effect(do -> undo)`

`Context.effect(apply)` 会立刻执行 `apply()`；`apply()` 必须返回撤销函数。运行时把撤销函数包装成幂等操作，并按 LIFO 顺序执行全部撤销。

```python
def start(ctx):
    def apply():
        resource = acquire()
        return lambda: release(resource)

    ctx.effect(apply)
```

即使激活在后续步骤失败，之前成功的 effect 仍会回滚。某个撤销失败不会阻止其余清理；所有清理错误最终通过 `ExceptionGroup` 一并报告。

### 2. `provide / get`

`Context.provide(key, value)` 本身也是 effect：它发布能力，同时记录撤回能力的逆操作。

消费者不导入具体 provider，而是在 `requires` 中声明 key，并且只能通过 `Context.get(key)` 读取这些 key。每次激活会得到不可变的依赖快照。即使 provider 已从全局停止发布能力，这份快照在 teardown 期间仍然可读。

`Harness.get(key)` 含义不同：它读取实时的全局 service store，只有 active provider 存在时才成功。

### 3. `plugin(requires -> activate / deactivate)`

```python
harness = Harness()

def start_consumer(ctx):
    begin(ctx.get("clock"))

def start_clock(ctx):
    ctx.provide("clock", monotonic)

unmount_consumer = harness.mount(
    "consumer",
    requires=("clock",),
    start=start_consumer,
)

unmount_provider = harness.mount(
    "clock-provider",
    requires=(),
    start=start_clock,
)
```

`Harness.mount(name, requires, start)` 返回幂等的 unmount 函数。`start(ctx)` 可以返回一个清理函数，也可以返回 `None`。插件只有四种可见状态：

- `waiting`：至少一个依赖缺失。
- `active`：激活完成，插件提供的 service 已提交。
- `stopping`：正在撤回消费者和副作用。
- `failed`：激活或 teardown 失败，详情可从 `status()` 获取。

## 完整生命周期轨迹

默认 demo 会先挂载 consumer，再挂载 provider；随后替换 provider，让 consumer 基于新快照自动重启，最后关闭运行时：

```text
consumer: waiting for greeter
provider-v1: active -> consumer: active
withdraw provider-v1
  从实时 store 隐藏 greeter
  consumer: stopping -> waiting
  provider-v1: undo second, undo first
provider-v2: active -> consumer: active again
close
  consumer 先停止，provider-v2 再清理
  所有 effect 按 LIFO 回滚
status: {}
```

这个顺序是刻意设计的。卸载 provider 时，Palintropos 先让新 consumer 无法看到其 service，再递归停止已有 consumer；consumer 此时仍可读取已提交快照。最后才执行 provider 自己的撤销栈。

显式运行同一轨迹：

```bash
python3 palintropos.py demo
```

## 公开核心 API

刻意保持极小的公开接口如下：

```python
Harness.mount(name, requires, start) -> unmount
Harness.get(key)
Harness.status()
Harness.close()

Context.effect(apply) -> undo
Context.provide(key, value) -> undo
Context.get(key)
```

激活错误不会从 `mount()` 直接逃逸，而是保留为 `failed` 状态，调用方因此可以检查完整组合。清理错误会等所有相关清理执行完后，才从 `unmount()` 或 `close()` 以 `ExceptionGroup` 抛出。

Service key 必须唯一。第二个 provider 会激活失败，但不会替换第一个 provider。插件若用 `Context.get()` 读取未声明的 key，也会激活失败。

## 可选真实 tool-call 闭环

`ask` 子命令用同样三个原子组合三个插件：

1. Anthropic-compatible Messages API 客户端，提供 `llm`；
2. 纯函数 `reverse`，提供 `reverse`；
3. 同时依赖这两个能力、步数有上限的单用户轮次 Agent。

密钥通过当前进程环境中的 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 提供，同时设置 `ANTHROPIC_BASE_URL`。GLM-5.2 的官方兼容地址为：

```bash
export ANTHROPIC_BASE_URL='https://open.bigmodel.cn/api/anthropic'
python3 palintropos.py ask --model glm-5.2 'Call reverse on stressed'
```

预期输出形态：

```text
tool_use: reverse('stressed')
tool_result: 'desserts'
assistant: ...desserts...
```

客户端只使用 `urllib`。它不接受 API-key 命令行参数，不读取 Claude 配置文件，不打印请求头，也不写请求日志。工具执行范围只有纯 `reverse` 函数，循环步数有硬上限。

GLM 兼容性见智谱官方的 [Claude API 兼容文档](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)。

## 与 DeepSeek Harness / Cordis 的关系

本项目只借鉴思想、不复制实现，设计依据固定在以下公开快照：

- [DeepSeek Harness `47f9438`](https://github.com/deepseek-ai/deepseek-harness/tree/47f943859bef60e4160492346772ded9b24f765a)
- [《A Programming Paradigm for Spatiotemporal Composability》`948a07b`](https://github.com/cordiverse/paper/tree/948a07b369c62adb3b12e102458be5c18dfb69b9)
- [Cordis `8cc9e33`](https://github.com/cordiverse/cordis/tree/8cc9e33fab69e2d0476d126baaf2acb24e6a6ab4)

| Palintropos 原子 | 保留的上游思想 | 删去的生产级机制 |
| --- | --- | --- |
| `effect(do -> undo)` | Cordis 可逆 effect 与插件自有清理 | 异步 effect、嵌套 fiber、事件、诊断 |
| `provide / get` | 稳定 key service 与响应式 coeffect | 类型/反射、隔离、拦截、可选注入 |
| `mount(requires, start)` | 依赖驱动的插件激活与重启 | YAML loader、配置协调、HMR、包图 |
| `ask` 组合 | DeepSeek Harness 的 “everything is a plugin” 架构 | 完整 agent loop、持久化、工具注册表、UI 与策略层 |

论文把时间可组合性（撤销组件 effect）和空间可组合性（声明并响应式解析依赖）分开定义。Palintropos 只把两者最小的可执行交集具体化。

## 信任边界

`mount`、`unmount` 和 `status` 通过人类可调用的 Python API 展示 creator-style 运行时修改；它们不会作为模型工具暴露。Palintropos 不执行模型生成的 Python，不运行 shell，也不声称提供沙箱。

这项删减是刻意的：DeepSeek Harness 自身的动态包文档明确说明其 VM 不是安全边界，并要求把模型生成组件视同 shell 权限（[固定快照中的信任边界](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/extensions/cordis-host-runner/README.md#trust-stance)）。

## 明确不做的能力

v1 不提供 TUI/Web UI、shell/editor、持久化、异步生命周期、HMR、沙箱、多 Agent、插件发现、包安装器、模型生成运行时代码、SDK、构建系统、PyPI 包或 GitHub Release。

这个微型运行时也不承诺任意交错全局修改都相互独立。它提供的是结构化保证：一次激活拥有的 effect 按 LIFO 撤销；已声明的依赖边在 teardown 时保证 consumer 先于 provider。

## 测试

CI 全部离线，只使用标准库：

```bash
python3 -m py_compile palintropos.py test_palintropos.py
python3 -m unittest -v
python3 palintropos.py demo
```

测试覆盖 effect 回滚与幂等、缺失依赖等待、consumer-before-provider teardown、teardown 快照、provider 替换、失败激活的部分回滚、重复 service、未声明访问、清理错误聚合、上下文精确恢复，以及假 LLM 驱动的 tool-call 循环。

## 许可证

[MIT](LICENSE)
