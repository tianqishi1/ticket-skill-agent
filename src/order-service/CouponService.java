package com.example.order.coupon;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * 优惠券服务
 *
 * ============== 【本文件是本次故障根因代码位置】 ==============
 * 已知缺陷：
 *  1) TTL 写死到当日 23:59:59（见 buildTtlToEndOfDay() 第 105-112 行）
 *     → 所有 "coupons:available:{tenantId}" 在同一时刻过期，重建期一致 MISS
 *     → 叠加版本发布导致偏移，会在晚高峰 20:00 前后一起过期
 *  2) 缓存 MISS 时没有加互斥锁（无 single-flight / SETNX / Redisson 分布式锁）
 *     → 同一个 tenant 的并发请求全部击穿到 DB
 *  3) queryCouponsFromDB() 对大租户一次查全量券 (L118: select * from coupon where tenant_id=?)
 *     → 大租户 (888) 有 5 万条记录，单查 500ms - 2s
 *     → DB 连接池 Hikari 被打满，其他连接排队 30s
 */
@Slf4j
@Service
public class CouponService {

    private static final String COUPON_KEY_PREFIX = "coupons:available:";

    @Autowired
    private StringRedisTemplate redisTemplate;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /**
     * 查询某租户可用优惠券列表（订单创建流程 STEP 1 调用）
     */
    public List<Coupon> getAvailableCoupons(Long tenantId) {
        String key = COUPON_KEY_PREFIX + tenantId;

        // 先查缓存
        String cached = redisTemplate.opsForValue().get(key);
        if (cached != null) {
            // cache HIT —— 正常
            log.debug("getAvailableCoupons cache HIT, tenantId={}", tenantId);
            return deserialize(cached);
        }

        // ========== 缺陷 2 起点：缓存 MISS 没有加互斥锁 ==========
        log.info("getAvailableCoupons cache MISS, tenantId={}, key={}", tenantId, key);
        // 缺少：RLock lock = redisson.getLock("lock:" + key); lock.lock();
        // 结果：高峰期同一个 tenant 的上百条并发请求全部穿透到 DB

        List<Coupon> coupons;
        try {
            // ========== 缺陷 3：大租户查全量 ==========
            coupons = queryCouponsFromDB(tenantId);
        } catch (Exception e) {
            log.error("queryCouponsFromDB error, tenantId={}", tenantId, e);
            // 缺陷：DB 失败时未返回空兜底，异常继续向上抛
            throw e;
        }

        // ========== 缺陷 1：TTL 写死到当天 23:59:59 ==========
        long ttlSeconds = buildTtlToEndOfDay();
        try {
            redisTemplate.opsForValue().set(
                    key,
                    serialize(coupons),
                    ttlSeconds,
                    TimeUnit.SECONDS
            );
        } catch (Exception e) {
            // 缺陷：缓存写失败被吞
            log.warn("write coupon cache failed, tenantId={}", tenantId, e);
        }

        // 缺少：lock.unlock();（因为前面没加锁）
        return coupons;
    }

    /**
     * 缺陷 1：TTL 计算 —— 返回「今日 23:59:59 减去当前时间」的秒数
     * 结果：所有租户 key 的过期时刻集中在 23:59:59 附近，再叠加版本发布时间偏移，
     *       会在同一时间窗口（例如 20:01）大批量同时过期。
     *
     * 正确做法：固定 TTL（比如 7200s）+ 随机抖动 ±300s，避免雪崩。
     */
    private long buildTtlToEndOfDay() {
        LocalDateTime now = LocalDateTime.now();
        LocalDateTime endOfDay = LocalDateTime.of(now.toLocalDate(), LocalTime.of(23, 59, 59));
        long seconds = Duration.between(now, endOfDay).getSeconds();
        // 边界：如果已经过了 23:59:59，给一个小的默认值
        return Math.max(seconds, 60L);
    }

    /**
     * 缺陷 3：对大租户直接 select *，无分页、无字段裁剪。
     * 对 tenantId=888 大租户，该 SQL 扫描行 ~5 万，耗时 500ms~2s。
     */
    private List<Coupon> queryCouponsFromDB(Long tenantId) {
        String sql = "SELECT id, tenant_id, name, type, discount, min_amount, start_time, end_time, status " +
                     "FROM coupon WHERE tenant_id = ? AND status = 1 AND end_time > NOW()";
        try {
            return jdbcTemplate.query(sql, (rs, row) -> {
                Coupon c = new Coupon();
                c.setId(rs.getLong("id"));
                c.setTenantId(rs.getLong("tenant_id"));
                c.setName(rs.getString("name"));
                return c;
            }, tenantId);
        } catch (Exception e) {
            log.error("queryCouponsFromDB exception, tenantId={}", tenantId, e);
            throw e;
        }
    }

    public void validateCoupon(Long tenantId, Long userId, Long couponId, List<Coupon> available) {
        // 校验 couponId 是否在可用列表中、是否属于该用户
        // 省略：不是本次瓶颈
    }

    // ============ 序列化工具（省略真实实现，仅示意） ================
    private String serialize(List<Coupon> coupons) { return "[" + coupons.size() + " coupons]"; }
    private List<Coupon> deserialize(String cached) { return Collections.emptyList(); }
}
