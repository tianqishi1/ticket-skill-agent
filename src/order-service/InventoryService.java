package com.example.order.inventory;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 库存服务（本次不是瓶颈，仅为示意）
 * 正常 P99 < 30ms，在 2026-08-20 高峰监控中无异常。
 */
@Slf4j
@Service
public class InventoryService {

    public void checkAndLock(Long tenantId, Object items) {
        // 调用库存中心 RPC 或者 Redis 扣减
        // 本次案例中不是瓶颈，跳过细节
    }
}
