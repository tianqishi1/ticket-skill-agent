# KB-001：下单接口 5xx 率飙升 - 支付网关超时（2025-12-15）
- **现象**：高峰期 POST /api/order/create 5xx 率 2.1%，错误码 502，监控显示 PaymentService.prePay P99 > 5000ms。
- **根因**：第三方支付网关 DNS 解析偶发超时，重试策略未生效（spring-retry @Retryable 未作用于 public 方法外层代理）。
- **证据**：支付服务日志 `UnknownHostException api.pay.example.com`，13:15 开始集中；NACOS 配置 retry.maxAttempts=3 但 AOP 未拦截到。
- **处理**：手动切换支付 DNS 解析为内网缓存 + @Retryable 移到 Service public 方法；高峰期无再次出现。
- **预防**：支付侧 5xx 监控告警 + 域名级健康检查。
- **关键字**：支付、502、支付超时、DNS、@Retryable

# KB-002：商品详情页偶现 500 - NPE（2026-01-08）
- **现象**：特定 SKU（已下架的预售品）商品详情偶现 500，报错 `NullPointerException at ProductService.java:225`。
- **根因**：预售商品下架后 skuExtend 对象为 null，代码未做判空直接取 getPreSaleConfig()。
- **证据**：17 条相同堆栈，SKU 前缀均为 PS-2025-xxx，对应商品表 status=OFF_SHELF 且 extend 列为 NULL。
- **处理**：补判空 + 默认值兜底，灰度上线验证 OK。
- **关键字**：NPE、预售、下架、判空、NullPointerException

# KB-003：晚高峰下单 504 — 优惠券缓存集中失效击穿（2026-03-21）
- **现象**：每天 20:00 前后 3-5 分钟，部分大租户下单 504，5xx 率 2-3%，P99 打到 3000ms（网关超时），其他接口正常。
- **根因（与本次样例相同模式）**：
  1. CouponService.getAvailableCoupons 缓存 key 的 TTL 写成「当天 23:59:59 - 当前时间戳」，结果是所有 key 在 00:00 新的一天会被设置为相同 TTL（86399s）→ 每天 23:59:59 同时过期；但线上实际触发窗口偏移到了 20:00（因缓存重建后 TTL 计算代码在白天发布导致）。
  2. 缓存 miss 没有加互斥锁（单飞/setnx），同一个 tenant 的并发请求全部击穿到 DB。
  3. 大租户（tenantId=888 等）优惠券记录 3-5 万条，单条查询耗时 500ms-2s，打满 Hikari 连接池（50 个），排队的请求 30s 后报 Hikari connection timeout，再叠加网关 3s 超时返回 504 给前端。
- **证据链**：
  - 日志：20:01 连续 3 次 cache MISS + HikariPool Connection is not available（order-service.log）。
  - 监控：Hikari Active=50/Total=50、Waiting>280；getAvailableCoupons P99=29.8s；Redis coupons:available:* 命中率从 98% 掉到 83%。
  - 代码：CouponService.java TTL 计算行 + setIfAbsent 未加锁。
- **处理（⚠️ 需人工执行）**：
  1. TTL 改为固定时长（例如 2h）+ 随机抖动 ± 5 分钟，避免同一租户/所有租户同刻过期。
  2. 缓存 miss 加互斥锁（Redisson getLock / Redis SETNX）保证单飞。
  3. Hikari 连接池上限从 50 评估调到 80-100（先压测）。
  4. 大租户优惠券查询加分页/按券类型分批，或前置聚合 ES。
- **关键字**：504、优惠券、缓存击穿、TTL 写死、Hikari 连接池打满、gateway timeout、部分租户、晚高峰
- **匹配建议**：本次工单与 KB-003 重合关键词：504、下单、高峰期、部分用户/租户 → 建议优先按 KB-003 证据链验证。

# KB-004：登录接口 500 - JWT 密钥被刷新（2026-05-02）
- **现象**：发布后登录全部 500，报错 `JWT signature does not match locally computed signature`。
- **根因**：发布时 secret-key 配置被重置为随机值，老 token 无法验证。
- **处理**：回滚配置 + 双密钥兼容老 token 30 天。
- **关键字**：登录、JWT、signature、配置发布

# KB-005：导出接口 OOM - 大数据量无分页（2026-07-10）
- **现象**：运营导出 30 万订单 xlsx，order-service Pod OOMKilled，Heap dump 显示 XSSFRow 占 6.2G。
- **根因**：导出代码一次拉取全量订单到内存。
- **处理**：分页流式查询 + SXSSF 流式写。
- **关键字**：OOM、导出、Excel、XSSF、内存泄漏、分页
