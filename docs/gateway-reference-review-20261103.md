# deepseek-gateway 参考核查（2026-11-03）

## 一、实际查阅

同名项目有多个。本次选取与“虚拟 Key、余额计费、并发限制”描述相符的 [TesterNaN/deepseek-gateway](https://github.com/TesterNaN/deepseek-gateway)，静态阅读版本为 `0db3001be5869d09ead6c5c47684d7545fb32132`。它不是 DeepSeek 官方项目，本项目也未与其建立合作或取得安全背书。只读取公开 README 与源码，没有安装依赖、启动服务、导入真实 Key 或调用其测试脚本。

| 模块 | 在该版本中观察到的职责 |
| --- | --- |
| [`app/dependencies.py`](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/dependencies.py) | 读取客户端 Bearer 虚拟 Key，查 SQLite 记录；余额不大于 0 时在转发前返回 402 |
| [`app/api/proxy.py`](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/api/proxy.py) | 用服务器持有的真实上游 Key 替换 Authorization；转发请求；普通响应或流式响应结束后提取 usage 并调用扣费 |
| [`app/billing.py`](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/billing.py) | 按模型、缓存命中/未命中输入和输出 token 计算金额；以条件 UPDATE 扣减虚拟余额 |
| [`app/utils.py`](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/utils.py) | 单进程内记录每个虚拟 Key 的在途请求数，申请和归还并发槽位 |
| [`app/database.py`](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/database.py) | 保存虚拟 Key、租户、余额和上游配置；管理接口修改这些记录 |

## 二、可以学习的理由

凭据集中保管与客户端凭据分离，能减少原始上游 Key 出现在业务客户端的需要；鉴权、并发、计费、转发分别成模块，便于单独验证；普通与流式响应分别收尾，也有助于明确连接和并发槽位的清理责任。

这些是架构参考。当前额度管理器依旧按官方平台返回的实际费用做本地核算，并在达额后提交撤销，不接管模型请求。没有把虚拟令牌、Redis、请求代理等新系统塞进本项目。现有按人额度、导出、日志和撤销记录继续保留。

## 三、已做的核查与边界

以下是该固定版本源码的静态观察及由此作出的工程推论，不是对远程部署的测试或完整安全审计：

- **有请求前余额检查，但没有看到对应请求的预算预留。** 扣费位于响应后；条件 UPDATE 在余额小于本次费用时返回失败，调用方没有据此追记负余额。由此推论，余额仍为正但不足以覆盖一次请求，或多个在途请求同时产生费用时，不能靠这套检查保证零超支。见 [鉴权](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/dependencies.py)与[扣费实现](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/billing.py)。
- **流式用量缺失有固定金额兜底，未知模型计费返回 0。** 这些值不等于官方真实账单；推论是采用前需要处理流式中断、模型价格更新、函数类独立费用与失败对账，不能直接作为财务准确性保证。见 [响应收尾](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/api/proxy.py)与[价格计算](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/billing.py)。
- **凭据和日志需要额外审查。** 该版本在 SQLite 中直接使用虚拟 Key 文本，并保存上游配置；部分管理/扣费日志包含完整虚拟 Key。虚拟令牌本身也能调用网关，不能写成“泄漏无风险”。本项目保留不记录原始凭据的策略。见 [数据库](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/database.py)、[管理接口](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/api/admin.py)及[计费日志](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/billing.py)。
- **并发计数位于进程内存。** 该实现不能自动扩展成多进程/多机器共享限额；本次没有发现把成员身份强制写入 DeepSeek `user_id` 的转发逻辑。不要把此前模型给出的网关设计草稿当成此仓库已实现的功能。见 [槽位计数](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/utils.py)与[转发](https://github.com/TesterNaN/deepseek-gateway/blob/0db3001be5869d09ead6c5c47684d7545fb32132/app/api/proxy.py)。

## 四、仍未验证与使用建议

未验证部署安全、管理员会话、权限隔离、并发压测、完整费用对账、平台条款适用性或长期维护状况，也没有把第三方 README 的安全描述当作事实背书。网关本地检查可拒绝后续新请求，仍需自行设计并验证在途费用预留、结算、异常恢复及日志保护。

多人业务、需要虚拟令牌或请求前本地配额时，可先学习并评估这一类方案；普通单账号本人使用不必迁移。任何方案均应遵守平台规则，开源代码的存在不等于平台允许共享、转售或任意自动化操作。项目选择和实际使用风险须由使用者结合自身授权及部署条件评估。
