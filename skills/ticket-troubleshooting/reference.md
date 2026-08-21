# ticket-troubleshooting skill — 参考速查手册（reference.md）

> 本文件是 `SKILL.md` 主 prompt 的**辅助静态参考资料**，由智能体在执行 5 阶段诊断流程时按需查阅。
> ⚠️ 这里所有「命令 / SQL / 操作」仅为人工排查时的思路参考，智能体不得直接执行；任何落地动作必须前置【需人工执行】标识。

---

## 一、HTTP 网关错误码速查（对应前端样例 → 快速锁定层面）

| 状态码 | 名称 | 典型含义 | 第一排查方向 |
|---|---|---|---|
| **400** | Bad Request | 入参校验失败 | 前端传参 schema、字段缺失、类型错误 |
| **401** / **403** | Unauthorized / Forbidden | 鉴权失败、权限不足 | token 是否过期、租户/角色权限配置、网关鉴权插件 |
| **404** | Not Found | 路由或资源不存在 | 网关路由配置、服务名、URL 路径拼写、服务是否注册 |
| **408** | Request Timeout | 客户端发包慢（较少见） | 网络、客户端/CDN、大文件上传 |
| **429** | Too Many Requests | 被限流 | 网关限流规则、租户级/接口级流控阈值 |
| **500** | Internal Server Error | 业务服务抛了未捕获异常 | 直接看后端 ERROR 堆栈 |
| **502** | Bad Gateway | 网关拿到上游**无效响应**（连接被重置/空响应/upstream prematurely closed） | 上游服务 Pod OOMKill、优雅下线没做好、反向代理配置 |
| **503** | Service Unavailable | 上游实例不可用/全部被摘除 | 实例存活/就绪探针、注册中心实例列表、熔断开启 |
| **504** | Gateway Timeout | **网关在超时阈值内未等到上游响应**（本次样例场景） | 链路 P99 是否超网关阈值 → 方法级耗时拆分 → DB/缓存/下游/MQ |

> 经验：500 先看后端堆栈；502 先看 Pod 重启；503 先看实例和熔断；**504 直接跳到「链路耗时拆分 + 连接池/线程池水位」**。

---

## 二、Java 常见异常 → 根因方向速查表

| 异常关键词 | 置信度 | 典型根因方向 |
|---|---|---|
| `OutOfMemoryError: Java heap space` | 高 | 大集合、内存泄漏；抓堆 dump 看 dominator tree |
| `OutOfMemoryError: Metaspace` | 高 | 动态代理/CGLIB/反射不停生成类；类加载器泄漏 |
| `OutOfMemoryError: Direct buffer memory` | 高 | Netty/Apache HttpClient 堆外缓冲未释放 |
| `GC overhead limit exceeded` | 高 | Full GC 频繁但回收不到 → 90% 是堆泄漏 |
| `unable to create new native Thread` | 高 | 线程数爆了；`ulimit -u`、线程池无界队列 |
| `StackOverflowError` | 高 | 递归无终止、JSON 序列化循环引用 |
| `NoSuchMethodError` / `NoSuchFieldError` | 高 | Jar 冲突/编译运行版本不一致；查依赖树 |
| `ClassNotFoundException` / `NoClassDefFoundError` | 中 | 包没打进去/类加载器差异；核对 fat jar BOOT-INF/lib |
| `LinkageError` / `ClassCastException` (同类名但不同 loader) | 中 | 类加载器隔离问题（多包同名类、spring-devtools） |
| `BeanCreationException` / `BeanCurrentlyInCreationException` | 高 | Spring 启动期循环依赖/注入的 Bean 出错；看 Caused by 最底层 |
| `NullPointerException` (NPE) | 高 | 返回值未判空；看堆栈最底层用户代码行 |
| `IndexOutOfBoundsException` | 中 | 空数组取第 0 个、分页错位、substring 越界 |
| `ConcurrentModificationException` | 高 | for-each 遍历同时修改集合（非并发容器）|
| **`SQLTransientConnectionException: HikariPool-1 - Connection is not available`** | **高** | **DB 连接池打满 → 慢 SQL / 长事务持有连接不释放 / 池上限过小**（本次样例场景） |
| `CannotGetJdbcConnectionException` (嵌套上面那条) | 高 | 同上 |
| `QueryTimeoutException` | 中 | 单 SQL 超 `@Transactional(timeout=)` 或 DB statementTimeout |
| `Deadlock found when trying to get lock` | 高 | DB 死锁；反向分析 SQL 加锁顺序 |
| `RedisCommandTimeoutException` / `RedisConnectionFailureException` | 高 | Redis 实例问题 / 连接池耗尽 / 网络抖动 |
| `RequestRejectedException` | 中 | 线程池/信号量熔断；看 hystrix/sentinel/circuitbreaker 配置 |
| `InterruptedException` | 中 | 线程被中断；检查 shutdown 路径、线程池是否被过早 close |

---

## 三、Python 常见异常 → 根因方向速查表

| 异常关键词 | 置信度 | 典型根因方向 |
|---|---|---|
| `ModuleNotFoundError` / `ImportError` | 高 | 解释器/venv/路径不对；核对 `sys.executable`、装包环境、`PYTHONPATH` |
| `AttributeError: 'NoneType' object has no attribute ...` | 高 | 上游返回 None 未判空；加 if / 默认值 |
| `KeyError` | 高 | dict 缺字段；用 `.get(key, default)` 或 try/except |
| `IndexError: list index out of range` | 高 | 空列表取 [0] / 分页错位 |
| `TypeError: ... not ...` | 中 | 类型不对（把 dict 当 list、把 int 当字符串拼接）|
| `ValueError` | 中 | int('abc')、json.loads 非 JSON |
| `RecursionError` | 高 | 递归无终止；序列化循环引用；改用迭代或 `sys.setrecursionlimit` |
| `MemoryError` | 高 | 大对象/泄漏；用 `tracemalloc` 抓增长点 |
| `RuntimeError: This event loop is already running` | 高 | asyncio 事件循环嵌套（在已跑 loop 里又 `run_until_complete`）|
| `asyncio.TimeoutError` | 高 | await 超时；核对 aiohttp/httpx timeout 配置 |
| `ConnectionError` / `requests.ConnectionError` | 高 | 目标服务不可达；核对 endpoint、DNS、防火墙 |
| `ReadTimeout` / `ConnectTimeout` | 高 | HTTP 客户端超时阈值过短或下游慢 |
| `SSLError: CERTIFICATE_VERIFY_FAILED` | 中 | 证书过期/自签/中间人；核对 CA 包 |
| `json.JSONDecodeError` | 中 | 上游响应非 JSON（网关/nginx 返回 HTML 错误页）|
| `pickle.PickleError` | 低 | 跨版本序列化不一致；建议改用 JSON/Protobuf |
| `django.db.utils.OperationalError` (timeout / connection) | 高 | DB 不可达 / 连接池耗尽 / 锁 |
| `pika.exceptions.ChannelClosed` | 中 | RabbitMQ channel 错误；消息大小/ack 模式 |

---

## 四、证据收集 Checklist（人工版）

> 智能体在阶段 3 可参考此清单，逐项核对是否收集齐全。缺哪项就提示用户补充。

**□ 基础信息**
- [ ] 工单发生时间（精确到分钟，与时区）
- [ ] 影响范围：用户量 / 租户量 / 是否单地域
- [ ] 是否必现 / 偶现 / 高峰期出现 / 特定租户出现
- [ ] 首次出现时间 & 最近一次变更（代码发布 / 配置 / 依赖 / 扩容）

**□ 前端侧（504/500/4xx 通用）**
- [ ] traceId（响应头 `x-trace-id` / `x-request-id`）
- [ ] HTTP 状态码 & Response Body（是 nginx HTML 还是业务 JSON）
- [ ] 请求 URL、Method、请求体、X-Tenant-Id、X-User-Id
- [ ] 浏览器 Console JS 报错截图文字版

**□ 后端日志侧**
- [ ] 网关 access 日志（同 traceId，看耗时、状态码、upstream）
- [ ] 业务服务 ERROR 日志（完整堆栈，包含 Caused by 链）
- [ ] 对应时间窗的 WARN/INFO 是否有早期信号（缓存 MISS 潮、重试开始）
- [ ] 上下游服务同 traceId 串联，定位最后一条日志卡在哪一步

**□ 监控侧**
- [ ] 接口 QPS / P50 / P99 / P999 / 5xx 率
- [ ] JVM：堆使用率、Full GC 次数&耗时、元空间
- [ ] 线程池：Tomcat/Undertow 活跃线程、队列长度
- [ ] 连接池：Hikari/Redis/HTTP active vs max、等待线程数
- [ ] 缓存：命中率、热点 key、Redis 实例 CPU & 内存 & 慢命令
- [ ] DB：活跃连接、TPS、慢查询 TOP10、锁等待
- [ ] MQ：积压量、消费 TPS、失败重试次数
- [ ] Pod/容器：重启次数、OOMKill、CPU 限流、内存 limits

**□ 代码侧**
- [ ] 堆栈最底部用户代码行 → 对应方法逻辑
- [ ] 异常捕获是否吞异常（catch Exception 后只 log.warn 再返回空）
- [ ] 缓存使用：TTL 策略、击穿/穿透/雪崩防护
- [ ] 事务：边界是否过大、是否有事务里 RPC/缓存调用
- [ ] MQ：幂等键、重试策略、死信处理
- [ ] 限流/熔断：阈值是否合理、触发是否有监控埋点
- [ ] 多租户：tenant_id 是否全程透传、是否存在跨租户泄漏风险

**□ 知识库侧**
- [ ] 是否有相同错误码的历史案例
- [ ] 是否有相同模块的「已复现 + 已修复」Runbook
- [ ] 上次修复是否有未落地的预防项（例如"加监控"但没加）

---

## 五、只读排查命令速查（⚠️ 仅人工参考，智能体不得自动执行）

### A. Java / JVM 侧（仅人工在排查机执行）
```bash
# 看进程 & 基本参数
jps -lvm
# 线程快照（死锁/阻塞）
jstack <pid>
# 类直方图（看什么对象最多）
jmap -histo:live <pid> | head -50
# GC 趋势（每秒一行，20 行）
jstat -gcutil <pid> 1000 20
# JVM 实际生效参数
jcmd <pid> VM.flags
```

### B. Python 侧
```bash
# 确认解释器 & 环境
python -c "import sys; print(sys.version, sys.executable)"
pip list | grep -i <pkg>
# 性能
python -m cProfile -s cumulative main.py
```

### C. 日志排查（grep 思路，仅人工）
```bash
# 按 traceId 关联
grep -E 'abc123def456|def789ghi012' app.log gateway.log
# 看某 1 分钟 ERROR 数
grep '2026-08-20 20:01.*ERROR' app.log | wc -l
# 找异常栈连续行（ERROR 到下一个日志时间戳）
```

### D. DB 只读查询思路（仅人工，禁止直接执行 DML）
```sql
-- 1) 看慢查询 TOP（思路，具体字段按实际 schema）
SELECT * FROM slow_query_log
WHERE start_time BETWEEN '2026-08-20 20:00:00' AND '20:05:00'
ORDER BY query_time DESC LIMIT 10;

-- 2) 大租户券量（对应本次 CouponService.queryCouponsFromDB 的风险）
SELECT tenant_id, COUNT(*) cnt
FROM coupon
WHERE status = 1
GROUP BY tenant_id
ORDER BY cnt DESC LIMIT 20;
```

---

## 六、证据记录模板（在对话/报告中粘贴时用）

```
【证据片段】
来源文件：<绝对路径>:L<起行>-L<止行>
关键内容：
> 关键行 1
> 关键行 2
> ...
说明：为什么这段是证据（例如「traceId=abc123def456 关联到 504，且 Hikari 等待 287」）
```

---

## 七、置信度判定准则（供智能体自检）

- **高置信度**：至少 3 类独立证据源互相印证（例如：日志堆栈 + 监控异常指标 + 代码缺陷 + 知识库匹配），且没有反证。
- **中置信度**：2 类证据印证，或 1 类强证据 + 1 类弱证据，但缺少某一环（例如日志+监控对得上，但未读源码/知识库）。
- **低置信度**：仅 1 类证据或全是语义推理（例如「504=网关超时」纯文字），或存在多个无法排除的竞争假设。
- 输出**高置信度**前，自检：能否按 traceId 从端到端串起一条完整链路？如果不能，考虑降为「中」。
