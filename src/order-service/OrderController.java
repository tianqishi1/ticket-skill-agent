package com.example.order;

import com.example.order.coupon.CouponService;
import com.example.order.dto.CreateOrderRequest;
import com.example.order.dto.CreateOrderResponse;
import com.example.order.inventory.InventoryService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

/**
 * 订单服务入口
 * 链路：createOrder -> 1) 查可用券 -> 2) 锁库存 -> 3) 写订单主单 -> 4) 预下单
 */
@Slf4j
@RestController
@RequestMapping("/api/order")
public class OrderController {

    @Autowired
    private CouponService couponService;

    @Autowired
    private InventoryService inventoryService;

    @PostMapping("/create")
    public CreateOrderResponse createOrder(@RequestBody CreateOrderRequest req,
                                           @RequestHeader("X-Tenant-Id") Long tenantId,
                                           @RequestHeader("X-User-Id") Long userId) {
        log.info("createOrder start, userId={}, skuCount={}", userId, req.getItems().size());
        try {
            // STEP 1: 查可用优惠券（问题点定位：CouponService.getAvailableCoupons）
            var coupons = couponService.getAvailableCoupons(tenantId);
            if (req.getCouponId() != null) {
                couponService.validateCoupon(tenantId, userId, req.getCouponId(), coupons);
            }

            // STEP 2: 锁库存
            inventoryService.checkAndLock(tenantId, req.getItems());

            // STEP 3: 写订单
            String orderNo = saveOrder(tenantId, userId, req, coupons);

            log.info("createOrder success, orderNo={}", orderNo);
            return CreateOrderResponse.ok(orderNo);

        } catch (Exception e) {
            // 问题：此处仅打一条 error 日志，堆栈未进一步上抛到 gateway 统一
            // 但本次问题是 Hikari 超时，堆栈里可以看到 CannotGetJdbcConnectionException
            log.error("createOrder failed", e);
            throw e;
        }
    }

    private String saveOrder(Long tenantId, Long userId, CreateOrderRequest req, Object coupons) {
        // 省略：写订单主/子表、写订单事件到 MQ
        // 本次样例中 saveOrder 未执行到（异常发生在查券阶段）
        return "SO" + System.currentTimeMillis();
    }
}
