# ResolveAI — 智能客户运营平台

[English](README.md) · **简体中文**

这是一个处理合成 SaaS 账单工单的本地工作台。它调查工单、检索政策依据、提出退款，并在另一位审批人确认之后，才在业务模拟器中执行并起草回复。

客户、发票、付款和退款都是合成数据。这不是生产环境的支付或客户消息服务。默认进程使用假模型和零预算。实时模型是单独的进程，除非项目所有者打开，否则保持关闭。

当前版本处理一个租户的重复付款退款。订阅、第二个租户，以及进程崩溃后的恢复，都不在这个版本里。

参见[产品范围](docs/PRODUCT_SCOPE.md)、[架构](docs/ARCHITECTURE.md)和[决策记录](docs/adr/README.md)。这些技术文档目前是英文。

## 开发环境

使用 Python 3.12、uv、Node 24 和 pnpm 11.25.0。Python 依赖锁定在 `uv.lock`，工作台依赖锁定在 `pnpm-lock.yaml`。依赖解析本身不是应用测试。

```sh
make env
make check-setup
make db-up
make migrate
make seed
```

在三个终端分别启动：

```sh
make simulator
make api
make web
```

打开 http://127.0.0.1:5173。登录页列出合成的客服和审批人账号。登录后，页眉有 English 与简体中文的语言菜单。新产生的调查记录和回复草稿使用创建当时所选的语言。客户工单正文保持夹具原来的语言。API 位于 http://127.0.0.1:8000，模拟器位于 http://127.0.0.1:8001。两者都只绑定本机。工作台把 `/api` 代理到 API。

数据库专供 ResolveAI 使用，并绑定到本机端口 55432。`compose.yaml` 和 `.env.example` 中的开发凭据是合成占位符。迁移会创建 `app` 和 `simulator` 模式、vector 扩展，以及受限的运行时角色。初始化角色只用于迁移和灌入数据。

运行 `make db-up` 时 Docker Engine 必须已经启动。`make check-setup` 只检查配置和导入，不会启动容器。换一台机器时先检查工具版本；不要重置任何无关的 PostgreSQL 实例。

把 `.env.example` 复制为 `.env` 可以覆盖文档中的默认值。提供方默认为假模型，实时模型预算为零。非假模型、模型名称、API 密钥或非零预算都会阻止启动。不要把真实 API 密钥写入被跟踪的文件。

```sh
make test
corepack pnpm install --frozen-lockfile
corepack pnpm --dir apps/web test:e2e
```

`make test` 运行 Ruff 和 pytest。Playwright 在浏览器里走通一笔批准的退款和一笔被拒绝的提案，并在 `resolveai_pytest` 不存在时自行创建它。两套测试都使用这个本机数据库。Playwright 的 API 端口是 18080，模拟器端口是 18081，工作台端口是 15173。一次只运行其中一套。`make seed` 写入的是 `resolveai_demo`。种子之前要先运行 `make migrate`，这样政策表才会存在。

退款金额来自政策和付款记录。客户原文不能决定金额，客服也不能批准自己提出的退款。退款响应丢失时，按原来的操作标识查询，不会另换一个标识再提交。回复是模拟回执。发送回复不会关闭工单。

尚未选择许可证。仓库公开本身不构成开源许可。
